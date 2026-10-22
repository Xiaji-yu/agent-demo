#!/usr/bin/env bash
# 把完整冷备包（PG dump + 配置 + 人格 + 运行态）写到 U 盘，每周由 systemd timer 调用。
#
# 边界：
# - 只写 $USB_ROOT/agent-demo-YYYY-MM-DD/，只清理同格式的旧目录，U 盘上其他文件一概不碰；
# - U 盘未挂载时跳过（exit 0）并在 journal 留一条记录，这是“没插盘”的正常情况；
# - 写前校验本地最新 dump 不超过 MAX_AGE_H，避免把过期数据当成功；
# - 先写临时目录、校验通过后再替换当天目录，中途失败不会毁掉上一份好的备份；
# - 写完做两道校验：MANIFEST 全量 + dump 与本地 sidecar 比对。
set -euo pipefail

USB_ROOT="${USB_ROOT:-/mnt/usb-backup/backup}"
SRC="${SRC:-/home/xiaji/code/agent-demo}"
KEEP="${KEEP:-8}"
MAX_AGE_H="${MAX_AGE_H:-30}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

if [ ! -d "$USB_ROOT" ] || [ ! -w "$USB_ROOT" ]; then
  log "U 盘未挂载或不可写（$USB_ROOT），跳过本次"
  exit 0
fi

DUMP="$(ls -t "$SRC"/data/backups/*.sql.gz 2>/dev/null | head -1 || true)"
[ -n "$DUMP" ] || { log "ERROR: 没有可用的 .sql.gz"; exit 1; }
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
RSYNC=(rsync -rt --modify-window=2 --no-perms --no-owner --no-group)
rm -rf -- "$TMP"
mkdir -p "$TMP/personas" "$TMP/data"

"${RSYNC[@]}" "$DUMP" "$TMP/db.sql.gz"
"${RSYNC[@]}" "$SRC/.env" "$SRC/config.yaml" "$TMP/"
"${RSYNC[@]}" "$SRC"/agentcore/personas/*.md "$TMP/personas/" 2>/dev/null || true
for f in llm-providers.json search-providers.json 2fa-secret.json; do
  [ -f "$SRC/data/$f" ] && "${RSYNC[@]}" "$SRC/data/$f" "$TMP/data/"
done
for d in archive workspace budget skills; do
  [ -d "$SRC/data/$d" ] && "${RSYNC[@]}" "$SRC/data/$d/" "$TMP/data/$d/"
done

(cd "$TMP" && find . -type f ! -name MANIFEST.sha256 -print0 \
  | sort -z | xargs -0 sha256sum > MANIFEST.sha256)
if ! (cd "$TMP" && sha256sum -c --quiet MANIFEST.sha256); then
  log "ERROR: MANIFEST 校验失败，保留上一份备份不动"
  exit 1
fi
want="$(cat "$DUMP.sha256")"
got="$(sha256sum "$TMP/db.sql.gz" | cut -d' ' -f1)"
if [ "$want" != "$got" ]; then
  log "ERROR: U 盘 dump SHA256 与本地 sidecar 不符"
  exit 1
fi

# 校验全部通过后才替换当天目录。
rm -rf -- "$DEST"
mv -- "$TMP" "$DEST"
trap - EXIT

# 轮转：只匹配 agent-demo-YYYY-MM-DD，保留最近 KEEP 份。
count=0
while IFS= read -r name; do
  count=$((count + 1))
  if [ "$count" -gt "$KEEP" ]; then
    log "prune: $name"
    rm -rf -- "${USB_ROOT:?}/$name"
  fi
done < <(find "$USB_ROOT" -maxdepth 1 -type d -name 'agent-demo-????-??-??' \
  -printf '%f\n' | sort -r)

sync
log "done: $DEST ($(du -sh "$DEST" | cut -f1))"
