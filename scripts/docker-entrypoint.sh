#!/bin/sh
# 入口脚本：root 启动时修挂载点属权，再降权到 app 运行。
#
# 为什么需要：compose 的 bind mount 指向不存在的目录时，docker 以 root:root
# 创建它，降权用户写不进去。而本项目的日志/归档/账本初始化都是**容错降级**的
# （logging_setup：目录不可写只降级为「仅控制台」；archive append 失败只 ERROR
# 不崩；budget 记账失败只 WARNING）——不修属权的话故障表现是「日志莫名只有
# 控制台输出、账本数字不动、归档文件不生成」，极难排查。
#
# 只改挂载点顶层属主，不递归：递归 chown 在 archive/backups 攒了几百 MB 后
# 会让每次启动都慢一截。宿主机 uid 非 1000 时，compose 里加 user: "UID:GID"
# 直接以该身份启动（不走这个分支）——但那只在宿主目录**已存在且属主正确**时
# 成立；目录不存在时 docker 会以 root:root 创建它，此时同样写不进去
# （M6，REVIEW-4d5c1b8..63f9444），所以部署时应先 mkdir -p 并 chown 到该 uid。
#
# 早年以 root 跑过、或从宿主拷入 root 属主的**文件**不在顶层 chown 的覆盖范围内
# （L11，REVIEW-4d5c1b8..63f9444）：.env 与 config.yaml 是单文件挂载（web 受控
# 写面的落点），宿主 uid 非 1000 时同样写不进，一并修属权（只改属主不动内容）。
# config/ 是 2026-10-28 JSON 化后的**目录**挂载（agent.json 主配置 + agent.env
# web 写面落点）：init 容器以 root 建宿主 ./config、Docker 对不存在的 bind 源也
# 以 root:root 补建，漏了它 web 写面就 500（P1-2 同款），所以它进 TOPDIRS。
# 因此降权后以 app 身份再自检一次可写性，不可写就喊出来——把静默降级变成响故障。

set -eu

# init 子命令：compose 项目目录的一键引导（生成 config/agent.json / config.yaml /
# data/）。
# 与下面的运行期路径完全无关，放在最前面尽早分流——引导是部署者手动跑一次的
# 动作，不该带上属权修复/降权那套逻辑。
#   docker run --rm -v "$PWD:/out" ghcr.io/<owner>/agent-demo:latest init /out
if [ "${1:-}" = "init" ]; then
    shift
    exec python /app/scripts/compose_init.py "$@"
fi

MOUNTS="logs archive backups budget workspace cache sessions privacy skills kb_samples state mirror ssh"
TOPDIRS="/app/config"
FILES="/app/.env /app/config.yaml /app/config/agent.env"

# 以指定身份复检挂载点可写性。root 的 [ -w ] 恒真，必须**降权后**测才准。
# 只打 ERROR 不退出：不可写是降级而非致命，退出会让容器整个起不来。
check_writable() {
    for d in $MOUNTS; do
        p="/app/data/$d"
        if [ -d "$p" ] && [ ! -w "$p" ]; then
            echo "ERROR: $p 不可写（属主 $(stat -c '%u:%g' "$p")，当前 uid $(id -u)）；" \
                 "日志/归档/账本将静默降级。请把该目录 chown 到运行身份，" \
                 "或改以 root 启动（本脚本会修属权后再降权）。" >&2
        fi
    done
    for d in $TOPDIRS; do
        if [ -d "$d" ] && [ ! -w "$d" ]; then
            echo "ERROR: $d 不可写（属主 $(stat -c '%u:%g' "$d")，当前 uid $(id -u)）；" \
                 "JSON 主配置与 web 受控写面都落在里面，写不进去设置页会整体 500。" \
                 "请把该目录 chown 到运行身份，或改以 root 启动（本脚本会修属权后再降权）。" >&2
        fi
    done
    for f in $FILES; do
        if [ -f "$f" ] && [ ! -w "$f" ]; then
            echo "ERROR: $f 不可写（属主 $(stat -c '%u:%g' "$f")，当前 uid $(id -u)）；" \
                 "web 受控写面对它的补丁会失败（500）。请把该文件 chown 到运行身份，" \
                 "或改以 root 启动（本脚本会修属权后再降权）。" >&2
        fi
    done
}

if [ "$(id -u)" = "0" ]; then
    for d in $MOUNTS; do
        p="/app/data/$d"
        if [ -d "$p" ] && [ "$(stat -c '%u' "$p")" != "1000" ]; then
            chown 1000:1000 "$p"
        fi
    done
    # 目录挂载（config/）：data/ 之外，逐个修属权
    for d in $TOPDIRS; do
        if [ -d "$d" ] && [ "$(stat -c '%u' "$d")" != "1000" ]; then
            chown 1000:1000 "$d"
        fi
    done
    # 单文件挂载（web 受控写面的落点）：目录 chown 覆盖不到，逐个修属权
    for f in $FILES; do
        if [ -f "$f" ] && [ "$(stat -c '%u' "$f")" != "1000" ]; then
            chown 1000:1000 "$f"
        fi
    done
    # 先以 app 身份自检，再 exec 目标命令。用 sh -c 包一层以便先跑检查；
    # "$@" 经 _ 占位后原样传递，参数中的空格/引号不被二次解释。
    exec gosu app /bin/sh -c 'check_writable() {
    for d in '"$MOUNTS"'; do
        p="/app/data/$d"
        if [ -d "$p" ] && [ ! -w "$p" ]; then
            echo "ERROR: $p 不可写（属主 $(stat -c "%u:%g" "$p")，当前 uid $(id -u)）；" \
                 "日志/归档/账本将静默降级。请把该目录 chown 到运行身份。" >&2
        fi
    done
    for d in '"$TOPDIRS"'; do
        if [ -d "$d" ] && [ ! -w "$d" ]; then
            echo "ERROR: $d 不可写（属主 $(stat -c "%u:%g" "$d")，当前 uid $(id -u)）；" \
                 "JSON 主配置与 web 受控写面都落在里面，写不进去设置页会整体 500。" \
                 "请把该目录 chown 到运行身份。" >&2
        fi
    done
    for f in '"$FILES"'; do
        if [ -f "$f" ] && [ ! -w "$f" ]; then
            echo "ERROR: $f 不可写（属主 $(stat -c "%u:%g" "$f")，当前 uid $(id -u)）；" \
                 "web 受控写面对它的补丁会失败（500）。请把该文件 chown 到运行身份。" >&2
        fi
    done
}
check_writable
exec "$@"' _ "$@"
fi

# 非 root 启动（compose 里配了 user:）：属主修复帮不上忙，只做自检。
check_writable

exec "$@"
