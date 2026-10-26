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
# 直接以该身份启动（不走这个分支），文件属主本就匹配。

set -eu

if [ "$(id -u)" = "0" ]; then
    for d in logs archive backups budget workspace cache sessions privacy skills kb_samples; do
        p="/app/data/$d"
        if [ -d "$p" ] && [ "$(stat -c '%u' "$p")" != "1000" ]; then
            chown 1000:1000 "$p"
        fi
    done
    exec gosu app "$@"
fi

exec "$@"
