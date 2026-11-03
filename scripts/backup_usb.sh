#!/usr/bin/env bash
# 把完整冷备包（PG dump + 配置 + 人格 + 运行态）写到 U 盘，每周由 systemd timer 调用。
#
# 边界：
# - 只写 $USB_ROOT/agent-demo-YYYY-MM-DD/，只清理同格式的旧目录，U 盘上其他文件一概不碰；
# - U 盘未挂载时跳过（exit 0）并在 journal 留一条记录，这是“没插盘”的正常情况。
#   **挂载点目录在拔盘后依然存在**，所以只查 -d/-w 会把 9GB 冷备写进根盘并假成功
#   （REVIEW-1b16fa7..9c2d3c1 M10）——必须 mountpoint -q 判定真实挂载；
# - 写前校验本地最新 dump 不超过 MAX_AGE_H，避免把过期数据当成功；
# - 先写临时目录、校验通过后再替换当天目录，中途失败不会毁掉上一份好的备份；
# - 写完做两道校验：MANIFEST 全量 + dump 与本地 sidecar 比对。MANIFEST 之前对
#   必含清单（dump/配置源/config.yaml）做存在性硬断言——MANIFEST 只校验“已写的
#   文件”，缺文件时双道校验全绿（M15）。配置源按 .env 或 config/agent.json 二选一；
# - 轮转按 **mtime** 保留最近 KEEP 份（按目录名排序在时钟回拨时误删当天份，L12）。
set -euo pipefail

USB_ROOT="${USB_ROOT:-/mnt/usb-backup/backup}"
SRC="${SRC:-/home/xiaji/code/agent-demo}"
KEEP="${KEEP:-8}"
MAX_AGE_H="${MAX_AGE_H:-30}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

# I3（REVIEW 2026-11-02）：KEEP=0 必须拒。旧校验只拒非数字，KEEP=0 时
# `tail -n +1` 列出全部目录（含刚 mv 上去的当天份）⇒ 轮转把当天备份连同
# 历史一起删掉，然后照常 log done 并 exit 0——假成功。agentcore/backup/
# db_backup.py:861 的 keep = max(1, int(keep)) 早就夹紧了，这里补齐。
# 与 backup_mirror.sh 的 M11 同款校验：KEEP 会拼进 `tail -n +$((KEEP + 1))`
# 算术上下文，MAX_AGE_H 拼进 `-gt` 比较——坏值不该等于任意命令展开。
case "$KEEP" in
  '' | *[!0-9]* | 0)
    log "ERROR: KEEP 必须是正整数（当前：$KEEP）"
    exit 2
    ;;
esac
case "$MAX_AGE_H" in
  '' | *[!0-9]*)
    log "ERROR: MAX_AGE_H 必须是非负整数（当前：$MAX_AGE_H）"
    exit 2
    ;;
esac

# M10：mountpoint -q 只对真实挂载点为真。挂载点目录常驻（mkdir -p 惯例），
# 拔盘后 -d/-w 仍全真——旧检查把完整冷备（含全部凭据）写进根盘、KEEP 份
# 轮转累积可写满根盘致 bot/DB 故障，journal 还记 done 假成功。
if ! mountpoint -q "$USB_ROOT"; then
  log "U 盘未挂载（$USB_ROOT 不是挂载点），跳过本次"
  exit 0
fi

DUMP="$(ls -t "$SRC"/data/backups/*.sql.gz "$SRC"/data/backups/*.sql.gz.age \
  2>/dev/null | head -1 || true)"
[ -n "$DUMP" ] || { log "ERROR: 没有可用的 .sql.gz/.sql.gz.age"; exit 1; }
# 加密产物原样保留后缀（.age 不能当明文 dump 用，别改名成 db.sql.gz）
DUMP_NAME="db.sql.gz"
case "$DUMP" in
  *.age) DUMP_NAME="db.sql.gz.age" ;;
esac
age_h=$(( ($(date +%s) - $(stat -c %Y "$DUMP")) / 3600 ))
if [ "$age_h" -gt "$MAX_AGE_H" ]; then
  log "ERROR: 最新备份已 ${age_h}h（>${MAX_AGE_H}h），拒绝把过期备份写到 U 盘"
  exit 1
fi

DEST="$USB_ROOT/agent-demo-$(date +%F)"
TMP="$USB_ROOT/.agent-demo-tmp-$$"
trap 'rm -rf -- "$TMP"' EXIT

log "dump: $DUMP (${age_h}h) -> $DEST"
# vfat 没有权限位：不保留属主/权限，时间戳容差 2s（FAT 只存偶数秒）。
# 权限由 systemd 的 UMask=0077 兜底（ext4 上落 0600，见 usb-backup.service）。
RSYNC=(rsync -rt --modify-window=2 --no-perms --no-owner --no-group)
rm -rf -- "$TMP"
mkdir -p "$TMP/personas" "$TMP/data"

"${RSYNC[@]}" "$DUMP" "$TMP/$DUMP_NAME"
# 配置源二选一（2026-10-28 JSON 化）：新部署在 config/agent.json，老部署在 .env。
# 两边都没有就放弃本次——冷备的全部意义就是「配置改坏了能救回来」。
CONFIG_SRC=""
if [ -f "$SRC/config/agent.json" ]; then
  mkdir -p "$TMP/config"
  "${RSYNC[@]}" "$SRC/config/agent.json" "$TMP/config/"
  CONFIG_SRC="config/agent.json"
  # web 写面落点：管理员在设置页改过的键在里面，丢了等于丢那些设置
  if [ -f "$SRC/config/agent.env" ]; then
    "${RSYNC[@]}" "$SRC/config/agent.env" "$TMP/config/"
  fi
elif [ -f "$SRC/.env" ]; then
  "${RSYNC[@]}" "$SRC/.env" "$TMP/"
  CONFIG_SRC=".env"
else
  log "ERROR: 既没有 .env 也没有 config/agent.json，拒绝打一份没有配置的冷备"
  exit 1
fi
# I1（REVIEW 2026-11-02）：config.yaml 的位置也是二选一——宿主机部署在仓库根，
# compose init 部署在 config/config.yaml。写死仓库根时 compose 项目目录里 rsync 源
# 不存在，退 23 后被 set -euo pipefail 直接带走，且发生在下面的硬断言之前，连
# 「冷备必含文件缺失」的自解释告警都没有。
CONFIG_YAML_SRC=""
for p in "$SRC/config/config.yaml" "$SRC/config.yaml"; do
  if [ -f "$p" ]; then
    CONFIG_YAML_SRC="$p"
    break
  fi
done
if [ -z "$CONFIG_YAML_SRC" ]; then
  log "ERROR: 既没有 config/config.yaml 也没有 config.yaml，拒绝打一份没有引擎配置的冷备"
  exit 1
fi
"${RSYNC[@]}" "$CONFIG_YAML_SRC" "$TMP/"
# schema 版本 sidecar（M2）：加密备份的规范名已去掉 .age（<file>.sql.gz.schema.json），
# 解密成 .sql.gz 后才能被 restore 的版本守卫读到。旧命名 <file>.age.schema.json
# 也接受；两者都缺时冷备仍可做，但要在 journal 里留"无版本守卫"的痕。
SCHEMA_SRC="${DUMP%.age}.schema.json"
[ -f "$SCHEMA_SRC" ] || SCHEMA_SRC="$DUMP.schema.json"
SCHEMA_NAME="${DUMP_NAME%.age}.schema.json"
# 必含清单：dump + 上面选中的那个配置源 + config.yaml（落点名固定为仓库根名，
# 与 I1 的位置探测无关——冷备恢复时按宿主机布局解开）。早先这里写死 .env，
# JSON 部署下必然缺一项（rsync 也先报错），整个 NAS 形态的冷备不可用。
REQUIRED_FILES=("$DUMP_NAME" "$CONFIG_SRC" config.yaml)
# cron 任务配置：存在则必须带走（投递目标只在里面）；state sidecar 不必。
# H1（REVIEW-63f9444..858afcc）：判定与复制都必须用 $SRC 前缀——旧实现只把它
# 加进必含清单、没有任何复制语句：CWD=$SRC（出货 unit 的 WorkingDirectory）时
# 每周冷备必败（必含断言 exit 1），CWD≠$SRC 时静默缺件（探测不到就不进清单）。
if [ -f "$SRC/data/cron/jobs.json" ]; then
  mkdir -p "$TMP/data/cron"
  "${RSYNC[@]}" "$SRC/data/cron/jobs.json" "$TMP/data/cron/jobs.json"
  REQUIRED_FILES+=(data/cron/jobs.json)
fi
if [ -f "$SCHEMA_SRC" ]; then
  "${RSYNC[@]}" "$SCHEMA_SRC" "$TMP/$SCHEMA_NAME"
  REQUIRED_FILES+=("$SCHEMA_NAME")
else
  log "WARN: 本地备份缺少 schema sidecar，U 盘冷备不带版本信息（restore 版本守卫不可用）"
fi
# 人格在 data/personas/（2026-10-29 移出 workspace 沙箱写面）。旧的
# agentcore/personas/*.md 是包内模板，不是运行时人格，别再把空 glob 当内容，
# 也别让 IO 错误被 `2>/dev/null || true` 吞掉（M15）
if [ -d "$SRC/data/personas" ]; then
  "${RSYNC[@]}" "$SRC/data/personas/" "$TMP/personas/"
else
  log "WARN: 未找到 data/personas，冷备不包含人格"
fi
# 状态文件：宿主机部署落在 data/ 根，compose 部署重定向到 data/state/
#（scripts/compose_init.py 的 STATE_REDIRECTS，2026-10-28）。只查 data/ 根会让
# NAS/容器形态的冷备**静默**丢掉 2FA secret、provider key 与搜索源 key——三样都
# 是「配置改坏了能救回来」的核心，而 MANIFEST 只校验已写的文件，缺了照样全绿。
state_file() {
  local f="$1" p
  for p in "$SRC/data/state/$f" "$SRC/data/$f"; do
    if [ -f "$p" ]; then
      printf '%s\n' "$p"
      return 0
    fi
  done
  return 1
}
for f in llm-providers.json search-providers.json 2fa-secret.json; do
  if p="$(state_file "$f")"; then
    "${RSYNC[@]}" "$p" "$TMP/data/"
  else
    log "WARN: 未找到 data/state/$f 或 data/$f，冷备不包含它"
  fi
done
for d in archive workspace budget skills cron; do
  [ -d "$SRC/data/$d" ] && "${RSYNC[@]}" "$SRC/data/$d/" "$TMP/data/$d/"
done

# M15：MANIFEST 只对“实际存在的文件”算 hash，不校验“该有的都在”——冷备可缺
# .env/config.yaml/dump 而双道校验全绿。生成前硬断言必含清单。
for req in "${REQUIRED_FILES[@]}"; do
  if [ ! -f "$TMP/$req" ]; then
    log "ERROR: 冷备必含文件缺失：$req（放弃本次，保留上一份不动）"
    exit 1
  fi
done

(cd "$TMP" && find . -type f ! -name MANIFEST.sha256 -print0 \
  | sort -z | xargs -0 sha256sum > MANIFEST.sha256)
if ! (cd "$TMP" && sha256sum -c --quiet MANIFEST.sha256); then
  log "ERROR: MANIFEST 校验失败，保留上一份备份不动"
  exit 1
fi
want="$(cat "$DUMP.sha256")"
got="$(sha256sum "$TMP/$DUMP_NAME" | cut -d' ' -f1)"
if [ "$want" != "$got" ]; then
  log "ERROR: U 盘 dump SHA256 与本地 sidecar 不符"
  exit 1
fi

# 校验全部通过后才替换当天目录。
rm -rf -- "$DEST"
mv -- "$TMP" "$DEST"
trap - EXIT

# 轮转：只匹配 agent-demo-YYYY-MM-DD，按 mtime 保留最近 KEEP 份（L12：按名字
# sort -r 在系统时钟回拨时把当天目录排到队尾优先被删）。
pruned=0
while IFS= read -r dir; do
  log "prune: $dir"
  rm -rf -- "$dir"
  pruned=$((pruned + 1))
done < <(find "$USB_ROOT" -maxdepth 1 -type d -name 'agent-demo-????-??-??' \
  -printf '%T@ %p\n' | sort -rn | tail -n +$((KEEP + 1)) | cut -d' ' -f2-)
# I4（REVIEW 2026-11-02）：find 失败（U 盘 IO 错误/权限）时管道末端 wc -l
# 恒回 0，pipefail 也取末端退码 ⇒ 失败被完全掩盖，打出「无需清理（0 份）」
# 而实际可能有超期份没删。命令替换的失败要显式接住。
if ! total=$(find "$USB_ROOT" -maxdepth 1 -type d -name 'agent-demo-????-??-??' | wc -l); then
  log "ERROR: 轮换计数失败（find 不可用），请人工检查 U 盘上的旧备份"
  exit 1
fi
[ "$pruned" -gt 0 ] || log "rotate: 无需清理（$total 份 ≤ KEEP=$KEEP）"

sync
log "done: $DEST ($(du -sh "$DEST" | cut -f1))"
