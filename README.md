# Kiro Gateway

Kiro API（AWS CodeWhisperer / Amazon Q Developer）的代理网关，提供 OpenAI 兼容接口（`/v1/chat/completions`）和 Anthropic 兼容接口（`/v1/messages`）。可配合 Claude Code、Cursor、Cline、OpenAI SDK 等工具使用。

## 来源与改动

本项目 fork 自 [jwadow/kiro-gateway](https://github.com/jwadow/kiro-gateway)，在此基础上做了以下改动：

- **启动时自动探测 Kiro API region**，无需手动配置
- **支持独立的 `KIRO_REGION`**，允许 API region 与 OIDC region 分离
- **工具名自动截短**：超过 Kiro 64 字符限制的工具名（如 Claude Code MCP 插件）自动映射，客户端无感知
- **认证增强**：token 刷新时从磁盘重新加载 credentials，修复 token rotation 导致的认证失效问题
- **`run.sh` 优化**：默认执行 restart、自动释放残留占用端口

---

## 安装

```bash
# 克隆仓库
git clone https://github.com/qinqiang2000/kiro-gateway.git
cd kiro-gateway

# 安装依赖（直接安装）
pip install -r requirements.txt

# 或使用 venv 虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate    # macOS/Linux
# .venv\Scripts\activate     # Windows
pip install -r requirements.txt

# 配置（参见配置部分）
cp .env.example .env
```

---

## 配置

编辑 `.env`，至少需要配置：

1. **`PROXY_API_KEY`**：自定义的网关访问密码，用于客户端连接时的 API key
2. **认证方式**（四选一）：

| 方式 | 配置项 | 适用场景 |
|------|--------|----------|
| Kiro IDE JSON 文件 | `KIRO_CREDS_FILE=~/.aws/sso/cache/kiro-auth-token.json` | Kiro IDE 登录后自动生成 |
| Refresh Token | `REFRESH_TOKEN=your_token` | 手动抓取 token |
| kiro-cli SQLite | `KIRO_CLI_DB_FILE=~/.local/share/kiro-cli/data.sqlite3` | 使用 kiro-cli |
| AWS SSO 缓存文件 | `KIRO_CREDS_FILE=~/.aws/sso/cache/your-sso-file.json` | 企业 IAM Identity Center |

其余可选配置（代理、region、调试模式等）参见 `.env.example` 中的注释。

---

## 启动

**macOS / Linux（推荐使用 run.sh 后台运行）：**

```bash
./run.sh              # 启动（或重启）服务，并实时查看日志
./run.sh stop         # 停止服务
./run.sh status       # 查看运行状态
./run.sh log          # 查看日志
./run.sh start --port 9000  # 指定端口启动
```

**直接运行：**

```bash
python main.py              # 默认 0.0.0.0:8000
python main.py --port 9000  # 自定义端口
```

服务启动后访问 `http://localhost:8000`，健康检查：`http://localhost:8000/health`

---

## 使用示例

### Claude Code

编辑 `~/.claude/settings.json`，将请求指向本网关：

```json
{
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "123",
    "ANTHROPIC_BASE_URL": "http://localhost:8000"
  }
}
```

> `ANTHROPIC_AUTH_TOKEN` 填写 `.env` 中配置的 `PROXY_API_KEY`。

或者直接通过环境变量启动：

```bash
export ANTHROPIC_BASE_URL="http://localhost:8000" ANTHROPIC_AUTH_TOKEN="123"
claude
```

### API 调用

```bash
# OpenAI 接口
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer 123" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "你好"}], "stream": true}'

# Anthropic 接口
curl http://localhost:8000/v1/messages \
  -H "x-api-key: 123" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-6", "max_tokens": 1024, "messages": [{"role": "user", "content": "你好"}]}'
```

---

## License

[AGPL-3.0](LICENSE)
