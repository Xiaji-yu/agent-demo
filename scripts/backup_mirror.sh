#!/usr/bin/env bash
# 把数据库备份镜像到远端（NAS / VPS），由 systemd timer 或手动调用。
#
# 设计要点：
# - 目标通用：MIRROR_HOST / MIRROR_ROOT（兼容旧的 NAS_HOST / NAS_ROOT）。
# - 只同步、不删除远端历史：本机 keep 轮转不应连带删掉异地副本；
#   远端按 KEEP_DAYS 独立清理，且清理放在同步**之前**——避免“先传后删”
#   在 VPS 10GB 配额上短暂超限。
# - MAX_GB > 0 时做目录级硬闸门（du），超限直接失败，绝不塞满远端。
# - 远端校验最新一份 dump 的 SHA256（sidecar 只有 hash，不能 sha256sum -c）。
# - 任一环节失败退出非 0，交给 systemd 记录/告警。
# - PITR 产物（pgwal/pgbase）目录不存在时自动跳过。
set -euo pipefail

MIRROR_HOST="${MIRROR_HOST:-${NAS_HOST:-nas-backup}}"
MIRROR_ROOT="${MIRROR_ROOT:-${NAS_ROOT:-/vol1/1000/backup/agent-demo}}"
SRC="${SRC:-/home/xiaji/code/agent-demo/data}"
KEEP_DAYS="${KEEP_DAYS:-30}"
MAX_GB="${MAX_GB:-0}"
WAL_ONLY="${WAL_ONLY:-0}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

case "$MAX_GB" in
  '' | *[!0-9]*)
    log "ERROR: MAX_GB 必须是非负整数（当前：$MAX_GB）"
    exit 2
    ;;
esac

sync_wal() {
  if [ -d "$SRC/pgwal" ]; then
    log "sync pgwal -> $MIRROR_HOST:$MIRROR_ROOT/pgwal"
    rsync -a --partial "$SRC/pgwal/" "$MIRROR_HOST:$MIRROR_ROOT/pgwal/"
  fi
}

if [ "$WAL_ONLY" = "1" ]; then
  ssh "$MIRROR_HOST" "mkdir -p '$MIRROR_ROOT/backups'"
  sync_wal
  log "done (wal-only)"
  exit 0
fi

# 1) 远端目录 + 先清理过期文件（避免同步时短暂超过配额）
ssh "$MIRROR_HOST" "mkdir -p '$MIRROR_ROOT/backups'"
log "prune $MIRROR_HOST:$MIRROR_ROOT/backups older than ${KEEP_DAYS}d"
ssh "$MIRROR_HOST" "find '$MIRROR_ROOT/backups' -maxdepth 1 -type f \
  \( -name '*.sql.gz' -o -name '*.jsonl.gz' -o -name '*.sha256' \) \
  -mtime +$KEEP_DAYS -delete"

# 2) 容量闸门（目录级；0 = 关闭）
if [ "$MAX_GB" -gt 0 ]; then
  used_kb="$(ssh "$MIRROR_HOST" "du -sk '$MIRROR_ROOT' | cut -f1")"
  limit_kb=$((MAX_GB * 1024 * 1024))
  if [ "$used_kb" -gt "$limit_kb" ]; then
    log "ERROR: $MIRROR_HOST:$MIRROR_ROOT 已用 ${used_kb}KB，超过 ${MAX_GB}GB 上限"
    exit 1
  fi
  log "remote usage: $((used_kb / 1024))MB / ${MAX_GB}GB"
fi

# 3) 每日逻辑备份
if [ -d "$SRC/backups" ]; then
  # 新鲜度闸门：bot 停机时内置备份不会产出，这里必须失败而不是“成功镜像了旧文件”。
  newest="$(ls -t "$SRC/backups"/*.sql.gz 2>/dev/null | head -1 || true)"
  if [ -n "$newest" ]; then
    age_h=$(( ($(date +%s) - $(stat -c %Y "$newest")) / 3600 ))
    if [ "$age_h" -gt 30 ]; then
      log "ERROR: 最新备份已 ${age_h}h（>30h），bot 可能停机，拒绝把过期备份当成功"
      exit 1
    fi
  else
    log "ERROR: $SRC/backups 里没有 .sql.gz，拒绝空镜像"
    exit 1
  fi
  log "sync backups -> $MIRROR_HOST:$MIRROR_ROOT/backups"
  rsync -a --partial --stats \
    "$SRC/backups/" "$MIRROR_HOST:$MIRROR_ROOT/backups/"
else
  log "skip backups: $SRC/backups 不存在"
fi

# 4) PITR 产物（未启用 WAL 归档时自动跳过）
sync_wal
if [ -d "$SRC/pgbase" ]; then
  log "sync pgbase -> $MIRROR_HOST:$MIRROR_ROOT/pgbase"
  rsync -a --partial "$SRC/pgbase/" "$MIRROR_HOST:$MIRROR_ROOT/pgbase/"
fi

# 5) 远端校验最新一份逻辑备份
log "verify latest dump on $MIRROR_HOST"
ssh "$MIRROR_HOST" "cd '$MIRROR_ROOT/backups' && \
  f=\$(ls -t *.sql.gz 2>/dev/null | head -1) && \
  [ -n \"\$f\" ] && \
  sum=\$(sha256sum \"\$f\" | cut -d' ' -f1) && \
  want=\$(cat \"\$f.sha256\") && \
  [ \"\$sum\" = \"\$want\" ] && echo \"ok: \$f\""

log "done"
