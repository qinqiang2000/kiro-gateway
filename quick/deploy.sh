#!/usr/bin/env bash
# quick-gateway 部署脚本（幂等）：从本机把代码（可选 + 某个账号的凭证）同步到远程主机并（重）部署。
#
# 用法:
#   quick/deploy.sh                       # 只同步代码 + 重建，【不动任何凭证】
#   quick/deploy.sh --creds b             # 代码 + 上传账号 b 的凭证
#   quick/deploy.sh --creds default,b     # 代码 + 上传多个账号的凭证
#   quick/deploy.sh --creds-only b        # 只上传账号 b 的凭证并重启
#   HOST=1.2.3.4 PEM=~/k.pem quick/deploy.sh
#
# 为什么默认不传凭证：Keycloak 每次刷新都会轮换 refresh_token，线上容器一直在轮换并回写
# 自己那份文件。把 Mac 上那份（早已被轮换作废的）副本盖上去，等于亲手弄死这个账号。
# 所以上传凭证必须显式点名账号，且只覆盖那一个文件。
#
# 账号名 ↔ 文件名: default -> gateway-creds.json, b -> gateway-creds-b.json
#
# 可用环境变量（均有默认值）:
#   HOST         远程主机 IP           (默认 43.160.157.90)
#   PEM          SSH 私钥路径          (默认 ~/tools/pem/rocky_test.pem)
#   SSH_USER     远程用户              (默认 root)
#   REMOTE_DIR   远程部署目录          (默认 /opt/quick-gateway)
#   PROJECT      docker compose 项目名 (默认 quick-gateway)
#   CREDS_DIR    本机凭证目录          (默认 ~/.quickwork)
#   KIRO_UID     容器内 kiro 用户 uid  (默认 999)
set -euo pipefail

HOST="${HOST:-43.160.157.90}"
PEM="${PEM:-$HOME/tools/pem/rocky_test.pem}"
SSH_USER="${SSH_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/opt/quick-gateway}"
PROJECT="${PROJECT:-quick-gateway}"
CREDS_DIR="${CREDS_DIR:-$HOME/.quickwork}"
KIRO_UID="${KIRO_UID:-999}"

SYNC_CODE=1
ACCOUNTS=""
case "${1:-}" in
  --creds)      ACCOUNTS="${2:-}"; [[ -n "$ACCOUNTS" ]] || { echo "--creds 需要账号名，如 --creds b" >&2; exit 2; } ;;
  --creds-only) SYNC_CODE=0; ACCOUNTS="${2:-}"; [[ -n "$ACCOUNTS" ]] || { echo "--creds-only 需要账号名，如 --creds-only b" >&2; exit 2; } ;;
  --no-creds)   ;;   # 与默认一致，保留兼容
  "" )          ;;
  * ) echo "未知参数: $1（用法见脚本头部注释）" >&2; exit 2 ;;
esac

# 账号名 -> 本机凭证文件名
creds_file_for() {
  if [[ "$1" == "default" ]]; then echo "gateway-creds.json"; else echo "gateway-creds-$1.json"; fi
}

# 仓库根目录（本脚本在 quick/ 下）
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_OPTS=(-i "$PEM" -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new)
REMOTE="${SSH_USER}@${HOST}"

say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

say "目标: ${REMOTE}:${REMOTE_DIR}  (project=${PROJECT})"
ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p '${REMOTE_DIR}/quickwork'"

if [[ "$SYNC_CODE" == 1 ]]; then
  say "同步代码 (--checksum: 按内容比对，避免时间戳漏更新)"
  # 关键: 绝不带 --delete，且显式排除 quickwork/，永不误删远程凭证。
  rsync -az --checksum \
    -e "ssh ${SSH_OPTS[*]}" \
    --exclude '.git' --exclude '.venv' --exclude 'venv' \
    --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'debug_logs*' --exclude '.pytest_cache' --exclude '.hypothesis' \
    --exclude '.env' --exclude '.env.*' --exclude 'log/' \
    --exclude '*.icns' --exclude '*.ico' --exclude '.DS_Store' \
    --exclude 'quickwork/' \
    "$REPO_DIR/main.py" "$REPO_DIR/quick" "$REPO_DIR/requirements.txt" \
    "$REPO_DIR/Dockerfile" \
    "$REMOTE:${REMOTE_DIR}/"
  # 独立 compose 单独放（仓库里在 deploy/quick/ 下）
  scp "${SSH_OPTS[@]}" "$REPO_DIR/deploy/quick/docker-compose.yml" \
    "$REMOTE:${REMOTE_DIR}/docker-compose.yml"
fi

if [[ -n "$ACCOUNTS" ]]; then
  IFS=',' read -r -a _accts <<< "$ACCOUNTS"
  for acct in "${_accts[@]}"; do
    fname="$(creds_file_for "$acct")"
    src="${CREDS_DIR}/${fname}"
    if [[ ! -f "$src" ]]; then
      echo "找不到账号 '${acct}' 的本机凭证 $src —— 先在已登录该账号的 Mac 上导出（见 quick/RUNBOOK.md §1）。" >&2
      exit 1
    fi
    say "上传账号 '${acct}' 的凭证 → ${fname}（只覆盖这一个文件）"
    scp "${SSH_OPTS[@]}" "$src" "$REMOTE:${REMOTE_DIR}/quickwork/${fname}"
    ssh "${SSH_OPTS[@]}" "$REMOTE" "
      chown ${KIRO_UID}:${KIRO_UID} '${REMOTE_DIR}/quickwork/${fname}' &&
      chmod 600 '${REMOTE_DIR}/quickwork/${fname}' &&
      chown ${KIRO_UID}:${KIRO_UID} '${REMOTE_DIR}/quickwork' &&
      chmod 700 '${REMOTE_DIR}/quickwork'
    "
  done
fi

say "构建并（重）启动容器"
ssh "${SSH_OPTS[@]}" "$REMOTE" "cd '${REMOTE_DIR}' && docker compose -p '${PROJECT}' up -d --build" 2>&1 | tail -4

say "等待健康检查"
ssh "${SSH_OPTS[@]}" "$REMOTE" '
  for i in $(seq 1 20); do
    s=$(docker inspect quick-gateway --format "{{.State.Health.Status}}" 2>/dev/null || echo none)
    [ "$s" = "healthy" ] && { echo "healthy"; break; }
    sleep 2
  done
  docker ps --filter name=quick-gateway --format "{{.Names}} | {{.Ports}} | {{.Status}}"
'

say "账号池状态"
ssh "${SSH_OPTS[@]}" "$REMOTE" '
  curl -sS -m 15 http://localhost:8000/quick/pool \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(\"  账号 %s/%s 可用\" % (d[\"ready\"], d[\"total\"]))
for a in d[\"accounts\"]:
    left = a[\"session_remaining_pct\"]
    print(\"  - %-10s %-9s 会话剩余 %s\" % (a[\"name\"], a[\"status\"], \"?\" if left is None else str(int(left))+\"%\"))
" || echo "  取账号池状态失败，检查 docker logs quick-gateway"
'

say "冒烟测试 (容器内 localhost:8000)"
ssh "${SSH_OPTS[@]}" "$REMOTE" '
  curl -sS -m 60 http://localhost:8000/quick/v1/messages \
    -H "content-type: application/json" \
    -d "{\"model\":\"claude-opus-4-8\",\"max_tokens\":24,\"messages\":[{\"role\":\"user\",\"content\":\"say: deploy ok\"}]}" \
  | python3 -c "import sys,json; print(\"  →\", json.load(sys.stdin)[\"content\"][0][\"text\"])" \
  || echo "  冒烟测试失败，检查 docker logs quick-gateway"
'
say "完成 ✅   状态页: http://${HOST}:9090/quick"
