#!/usr/bin/env bash
# quick-gateway 部署脚本（幂等）：从本机把代码 + 凭证同步到远程主机并（重）部署。
#
# 用法:
#   quick/deploy.sh                       # 用默认变量部署
#   HOST=1.2.3.4 PEM=~/k.pem quick/deploy.sh
#   quick/deploy.sh --no-creds            # 只同步代码+重建，不动远程凭证
#   quick/deploy.sh --creds-only          # 只（重新）上传凭证并重启
#
# 可用环境变量（均有默认值）:
#   HOST         远程主机 IP           (默认 43.160.157.90)
#   PEM          SSH 私钥路径          (默认 ~/tools/pem/rocky_test.pem)
#   SSH_USER     远程用户              (默认 root)
#   REMOTE_DIR   远程部署目录          (默认 /opt/quick-gateway)
#   PROJECT      docker compose 项目名 (默认 quick-gateway)
#   CREDS_SRC    本机凭证文件          (默认 ~/.quickwork/gateway-creds.json)
#   KIRO_UID     容器内 kiro 用户 uid  (默认 999)
set -euo pipefail

HOST="${HOST:-43.160.157.90}"
PEM="${PEM:-$HOME/tools/pem/rocky_test.pem}"
SSH_USER="${SSH_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/opt/quick-gateway}"
PROJECT="${PROJECT:-quick-gateway}"
CREDS_SRC="${CREDS_SRC:-$HOME/.quickwork/gateway-creds.json}"
KIRO_UID="${KIRO_UID:-999}"

SYNC_CREDS=1
SYNC_CODE=1
case "${1:-}" in
  --no-creds)   SYNC_CREDS=0 ;;
  --creds-only) SYNC_CODE=0 ;;
  "" )          ;;
  * ) echo "未知参数: $1" >&2; exit 2 ;;
esac

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

if [[ "$SYNC_CREDS" == 1 ]]; then
  if [[ ! -f "$CREDS_SRC" ]]; then
    echo "找不到本机凭证 $CREDS_SRC —— 先在已登录 Quick 的 Mac 上生成 (见 quick/DEPLOY_zh.md)。" >&2
    exit 1
  fi
  say "上传凭证并归属给 uid ${KIRO_UID} (容器内的 kiro，需可读可写以回写轮换 token)"
  scp "${SSH_OPTS[@]}" "$CREDS_SRC" "$REMOTE:${REMOTE_DIR}/quickwork/gateway-creds.json"
  ssh "${SSH_OPTS[@]}" "$REMOTE" "
    chown -R ${KIRO_UID}:${KIRO_UID} '${REMOTE_DIR}/quickwork' &&
    chmod 700 '${REMOTE_DIR}/quickwork' &&
    chmod 600 '${REMOTE_DIR}/quickwork/gateway-creds.json'
  "
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

say "冒烟测试 (容器内 localhost:8000)"
ssh "${SSH_OPTS[@]}" "$REMOTE" '
  curl -sS -m 60 http://localhost:8000/quick/v1/messages \
    -H "content-type: application/json" \
    -d "{\"model\":\"claude-opus-4-8\",\"max_tokens\":24,\"messages\":[{\"role\":\"user\",\"content\":\"say: deploy ok\"}]}" \
  | python3 -c "import sys,json; print(\"  →\", json.load(sys.stdin)[\"content\"][0][\"text\"])" \
  || echo "  冒烟测试失败，检查 docker logs quick-gateway"
'
say "完成 ✅"
