#!/usr/bin/env bash
# 把数据库备份镜像到远端（NAS / VPS），由 systemd timer 或手动调用。
#
# 设计要点：
# - 目标通用：MIRROR_HOST / MIRROR_ROOT（兼容旧的 NAS_HOST / NAS_ROOT）。
# - 只同步、不删除远端历史：本机 keep 轮转不应连带删掉异地副本；
#   远端按 KEEP_DAYS 独立清理，且清理放在同步**之前**——避免“先传后删”
#   在 VPS 10GB 配额上短暂超限。
# - MAX_GB > 0 时做目录级硬闸门（du），超限直接失败，绝不塞满远端。
# - 远端校验最新一份 dump 的 SHA256（sidecar 只有 hash，不能 sha256sum -c）；
#   缺 sidecar 与校验和不匹配是两种失败，文案区分（告警疲劳）。
# - REQUIRE_PRIVATE_IP=1（VPS 单位置）：解析 MIRROR_HOST 必须落在私网段
#   （WireGuard 内网）——防配置漂移把 9GB 明文 dump 推向公网主机。
# - 任一环节失败退出非 0，交给 systemd 记录/告警。
# - PITR 产物（pgwal/pgbase）目录不存在时自动跳过。
set -euo pipefail

MIRROR_HOST="${MIRROR_HOST:-${NAS_HOST:-nas-backup}}"
MIRROR_ROOT="${MIRROR_ROOT:-${NAS_ROOT:-/vol1/1000/backup/agent-demo}}"
SRC="${SRC:-/home/xiaji/code/agent-demo/data}"
KEEP_DAYS="${KEEP_DAYS:-30}"
MAX_GB="${MAX_GB:-0}"
WAL_ONLY="${WAL_ONLY:-0}"
REQUIRE_PRIVATE_IP="${REQUIRE_PRIVATE_IP:-0}"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

case "$MAX_GB" in
  '' | *[!0-9]*)
    log "ERROR: MAX_GB 必须是非负整数（当前：$MAX_GB）"
    exit 2
    ;;
esac

# M11：KEEP_DAYS 会拼进远端 find 谓词（-mtime +$KEEP_DAYS），未校验数字即可
# 注入额外谓词乃至 `; cmd`——与 MAX_GB 同款 case 校验。
case "$KEEP_DAYS" in
  '' | *[!0-9]*)
    log "ERROR: KEEP_DAYS 必须是非负整数（当前：$KEEP_DAYS）"
    exit 2
    ;;
esac

# M11：MIRROR_ROOT 以单引号形式拼进远端命令串，值含单引号即闭合引号注入
# 远端 shell。信任边界内（能写 env 的人本可执行命令），但配置坏值不该等于
# 对备份服务器的任意命令，且无任何前置报错。
case "$MIRROR_ROOT" in
  *"'"*)
    log "ERROR: MIRROR_ROOT 含单引号，拒绝（远端命令注入面）：$MIRROR_ROOT"
    exit 2
    ;;
esac

# M12：VPS 单位置要求解析到私网段（WG 内网）。主机名解析失败同样拒绝。
if [ "$REQUIRE_PRIVATE_IP" = "1" ]; then
  resolved="$(getent ahostsv4 "$MIRROR_HOST" 2>/dev/null | awk 'NR==1{print $1}')"
  case "$resolved" in
    10.* | 192.168.* | 127.* | 172.1[6-9].* | 172.2?.* | 172.3[01].*) ;;
    *)
      log "ERROR: REQUIRE_PRIVATE_IP=1 但 $MIRROR_HOST 解析到非私网地址（${resolved:-解析失败}）"
      exit 1
      ;;
  esac
  log "private-ip gate ok: $MIRROR_HOST -> $resolved"
fi

sync_wal() {
  if [ -d "$SRC/pgwal" ]; then
    log "sync pgwal -> $MIRROR_HOST:$MIRROR_ROOT/pgwal"
    rsync -a --partial "$SRC/pgwal/" "$MIRROR_HOST:$MIRROR_ROOT/pgwal/"
  fi
}

if [ "$WAL_ONLY" = "1" ]; then
  ssh -- "$MIRROR_HOST" "mkdir -p '$MIRROR_ROOT/backups'"
  sync_wal
  log "done (wal-only)"
  exit 0
fi

# 1) 远端目录 + 先清理过期文件（避免同步时短暂超过配额）
ssh -- "$MIRROR_HOST" "mkdir -p '$MIRROR_ROOT/backups'"
log "prune $MIRROR_HOST:$MIRROR_ROOT/backups older than ${KEEP_DAYS}d"
ssh -- "$MIRROR_HOST" "find '$MIRROR_ROOT/backups' -maxdepth 1 -type f \
  \( -name '*.sql.gz' -o -name '*.sql.gz.age' -o -name '*.jsonl.gz' \
     -o -name '*.sha256' -o -name '*.schema.json' \) \
  -mtime +$KEEP_DAYS -delete"

# 2) 容量闸门（目录级；0 = 关闭）
if [ "$MAX_GB" -gt 0 ]; then
  used_kb="$(ssh -- "$MIRROR_HOST" "du -sk '$MIRROR_ROOT' | cut -f1")"
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
  # L10：纯 JSONL 部署（DB 不可达时的降级备份）同样是合法产物——glob 收两种后缀。
  # age 加密产物（.sql.gz.age）同理，否则开启加密后新鲜度闸门会把好备份判成过期。
  newest="$(ls -t "$SRC/backups"/*.sql.gz "$SRC/backups"/*.sql.gz.age \
    "$SRC/backups"/*.jsonl.gz 2>/dev/null | head -1 || true)"
  if [ -n "$newest" ]; then
    age_h=$(( ($(date +%s) - $(stat -c %Y "$newest")) / 3600 ))
    if [ "$age_h" -gt 30 ]; then
      log "ERROR: 最新备份已 ${age_h}h（>30h），bot 可能停机，拒绝把过期备份当成功"
      exit 1
    fi
  else
    log "ERROR: $SRC/backups 里没有 .sql.gz/.sql.gz.age/.jsonl.gz，拒绝空镜像"
    exit 1
  fi
  log "sync backups -> $MIRROR_HOST:$MIRROR_ROOT/backups"
  rsync -a --partial --stats \
    "$SRC/backups/" "$MIRROR_HOST:$MIRROR_ROOT/backups/"
else
  # M14：目录缺失与「目录在但无 dump」同为异常——旧实现此处静默 exit 0，
  # 换路径部署后每晚“成功”但从未镜像任何 dump，零异地副本无人察觉。
  log "ERROR: $SRC/backups 不存在——没有可镜像的逻辑备份（检查 SRC/deploy 路径）"
  exit 1
fi

# 4) PITR 产物（未启用 WAL 归档时自动跳过）
sync_wal
if [ -d "$SRC/pgbase" ]; then
  log "sync pgbase -> $MIRROR_HOST:$MIRROR_ROOT/pgbase"
  rsync -a --partial "$SRC/pgbase/" "$MIRROR_HOST:$MIRROR_ROOT/pgbase/"
fi

# 5) 远端校验最新一份逻辑备份（L9：缺 sidecar 与校验和不匹配分开报）
log "verify latest dump on $MIRROR_HOST"
ssh -- "$MIRROR_HOST" "cd '$MIRROR_ROOT/backups' && \
  f=\$(ls -t *.sql.gz *.sql.gz.age *.jsonl.gz 2>/dev/null | head -1) && \
  [ -n \"\$f\" ] && \
  sum=\$(sha256sum \"\$f\" | cut -d' ' -f1) && \
  if [ ! -f \"\$f.sha256\" ]; then \
    echo \"NO-SIDECAR: \$f\"; exit 4; \
  elif [ \"\$sum\" != \"\$(cat \"\$f.sha256\")\" ]; then \
    echo \"CHECKSUM-MISMATCH: \$f\"; exit 3; \
  else echo \"ok: \$f\"; fi"

log "done"
