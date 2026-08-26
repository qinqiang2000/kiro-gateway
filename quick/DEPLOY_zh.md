# quick-gateway 认证与跨机部署手册（中文）

> 目的：把 Amazon Quick 的登录凭证从一台**已登录 Quick 的 Mac**，搬到**另一台机器**
> （通常是 Linux 服务器）上运行 quick-gateway。将来**新账号 / 新机器**照此操作即可。

---

## 1. 原理：认证是怎么工作的

Amazon Quick 桌面端登录后，凭证存在 **macOS 钥匙串（Keychain）**里，而不是文件。
但真正让网关跑起来所需的东西，全在那份凭证 JSON blob 里，而且刷新走的是**公网 Keycloak**，
所以只要把这份 JSON 搬到任何机器，就能脱离钥匙串独立运行。

关键链路：

1. **钥匙串取 blob**（仅 macOS，仅首次）：
   `security find-generic-password -s quickwork-enterprise-<profileId> -a session -w`
   —— profileId 来自 `~/.quickwork/profiles.json` 的 `last_active`。
2. **Keycloak 刷新**（公网，任何机器都能做）：
   `POST {token_endpoint}` 表单 `grant_type=refresh_token&client_id=quick-desktop&refresh_token=...`
   —— 换回新的 `id_token`（DataPlane 的 bearer）、`access_token`，并**轮换** `refresh_token`。
3. **调用推理**：`POST {tenant_url}/integration/quick-work/bedrock-proxy-stream`，
   头 `Authorization: Bearer <id_token>`（注意是 id_token，不是 access_token）。

blob 里的字段（`~/.quickwork/gateway-creds.json`）：

| 字段 | 作用 | 跨机是否必需 |
|------|------|:---:|
| `refresh_token` | 换新 token 的长寿命凭证（~90 天，会轮换） | ✅ 核心 |
| `token_endpoint` | Keycloak 刷新地址（如 `https://quick.piaozone.net/realms/quick/.../token`） | ✅ |
| `client_id` | OIDC 客户端（`quick-desktop`，公开、无 secret） | ✅ |
| `tenant_url` | 租户 DataPlane 地址（`https://qbs-<uuid>.dp.appintegrations...`） | ✅ |
| `id_token` / `access_token` | 短寿命 token（各 ~5 分钟） | 可空，会自动刷出来 |
| `region` / `user_arn` | 信息性字段 | 可选 |

### 关于「token 5 分钟就刷新」
Keycloak 给 id_token / access_token 的寿命就是 **300 秒**（后端设定，非本项目）。
DataPlane 用 id_token 鉴权，所以每次请求前若发现 id_token 距过期 < 60s（`TOKEN_REFRESH_THRESHOLD_SECONDS`），
就用 refresh_token 换一批新的 —— 这是正常现象。**长寿命的是 refresh_token（~90 天）**，
每次刷新轮换后由代码写回 creds 文件。

> ⚠️ 保活提醒：Quick 刷新是「有请求才触发」的懒刷新。若 quick-gateway 连续 **超过 90 天**
> 零流量，refresh_token 会静默过期，届时需从 Mac 重新拷贝 creds（见第 5 节）。有正常流量则无需担心。

---

## 2. 凭证文件的加载优先级（代码行为）

`QuickAuthManager`（`quick/auth.py`）加载顺序：

1. **缓存文件优先**：`QUICK_CREDS_FILE`（默认 `~/.quickwork/gateway-creds.json`）。
   存在就直接用，**完全不碰钥匙串** —— 这是 Linux 唯一走的路径。
2. **钥匙串兜底**（仅当文件不存在 **且** 系统有 `security` 命令，即 macOS）：读一次钥匙串，
   随即把 blob **原子写入**缓存文件（`0600`），以后再启动就走路径 1，不再弹密码框。
3. 两者都没有 → 报错，提示从 Mac scp 这个文件过来。

每次 Keycloak 刷新后，轮换出的 refresh_token + 新 id_token 都会**写回缓存文件**，保持自举。

---

## 3. 在 Mac 上生成缓存文件（一次性，会弹一次开机密码）

```bash
# 任意一次本地启动都会触发首次钥匙串读取并落盘（弹一次密码，点“始终允许”）：
cd <repo>
python main.py --port 8000        # 或 ./run.sh restart

# 确认文件已生成（应为 -rw------- 即 0600）：
ls -la ~/.quickwork/gateway-creds.json
```

不想起整个服务，也可只跑加载逻辑生成文件：
```bash
python -c "from quick.auth import QuickAuthManager; QuickAuthManager()._load()"
```

> 新账号：先在这台 Mac 上用**该账号**登录 Amazon Quick 桌面端，再执行上面步骤，
> 生成的就是新账号的 creds 文件。

---

## 3.5 一键部署（推荐）：`quick/deploy.sh`

第 4 节的所有步骤（同步代码、上传凭证、chown 999、构建、健康检查、冒烟测试）已固化为
幂等脚本。生成好缓存文件（第 3 节）后，直接：

```bash
./quick/deploy.sh                                  # 用默认主机/密钥
HOST=1.2.3.4 PEM=~/xxx.pem ./quick/deploy.sh       # 换主机
./quick/deploy.sh --no-creds                       # 只更新代码，不动远程凭证
./quick/deploy.sh --creds-only                     # 只换凭证并重启（如轮到新账号/续期）
```

脚本要点：用 `rsync --checksum` 按内容同步（避免时间戳漏更新的坑）、**绝不 `--delete`
且排除 `quickwork/`**（永不误删远程凭证）、自动把凭证 chown 给 uid 999。
下面第 4 节是脚本背后手动等价步骤，供排查时参考。

## 4. 拷贝到目标机器（Linux）并部署

### 4.1 拷贝凭证
```bash
scp -i <你的私钥.pem> \
    ~/.quickwork/gateway-creds.json \
    root@<目标机IP>:/opt/quick-gateway/quickwork/gateway-creds.json
```

### 4.2 同步代码（构建上下文）
```bash
rsync -az --delete \
  -e "ssh -i <你的私钥.pem>" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude 'debug_logs*' --exclude '.pytest_cache' --exclude '.hypothesis' \
  --exclude '.env' --exclude '.env.*' --exclude 'log/' --exclude 'deploy/' \
  --exclude '*.icns' --exclude '*.ico' --exclude '.DS_Store' \
  <repo>/ root@<目标机IP>:/opt/quick-gateway/

# 单独放置独立 compose（仓库里的 deploy/quick/docker-compose.yml）
scp -i <你的私钥.pem> \
    <repo>/deploy/quick/docker-compose.yml \
    root@<目标机IP>:/opt/quick-gateway/docker-compose.yml
```
（注意 `--delete` 会删掉源里没有的目录，所以 `quickwork/` 要在 rsync **之后**再建/再拷。）

### 4.3 关键：容器用户是 uid 999（kiro）
镜像以非 root 用户 `kiro`（uid 999）运行。挂载进去的 creds 文件必须让 uid 999 能**读也能写**
（刷新要回写轮换后的 token）：
```bash
ssh -i <你的私钥.pem> root@<目标机IP> '
  chown -R 999:999 /opt/quick-gateway/quickwork
  chmod 700 /opt/quick-gateway/quickwork
  chmod 600 /opt/quick-gateway/quickwork/gateway-creds.json
'
```
（宿主机上 999 可能显示为别的用户名，无所谓；容器内它就是 kiro。）

### 4.4 构建并启动（独立 compose 项目，不动其它服务）
```bash
ssh -i <你的私钥.pem> root@<目标机IP> '
  cd /opt/quick-gateway
  docker compose -p quick-gateway up -d --build
  docker logs quick-gateway 2>&1 | tail -20   # 应看到 source=file，且无密码提示
'
```
启动日志里 Kiro 相关的 401 是**正常的**（compose 里 `REFRESH_TOKEN=dummy` 只为通过 main.py 的
启动校验；Kiro 的 `/v1/*` 路由在本容器里用不到）。

### 4.5 冒烟测试
```bash
# 容器内 localhost（compose 默认把端口绑到 127.0.0.1:8000）：
ssh -i <你的私钥.pem> root@<目标机IP> '
  curl -sS http://localhost:8000/quick/v1/messages \
    -H "content-type: application/json" \
    -d "{\"model\":\"claude-opus-4-8\",\"max_tokens\":32,\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}]}"
'
```

---

## 5. 日常运维要点

- **只在一台机器上跑网关**。refresh_token 每次刷新会轮换；两台机器用同一份文件互刷，
  迟早互相把对方的 refresh_token 弄失效。
- **需要重拷 creds 的场景**：offline refresh_token 彻底过期（~90 天无流量），或在 Mac 上重新登录了 Quick。
- **重新部署**：改完代码后重复 4.2 的 rsync + `docker compose -p quick-gateway up -d --build`；
  `quickwork/` 凭证目录不受 rsync 影响（compose 单独 scp，不在 rsync 范围内）。
- **模型**：默认 `QUICK_FORCE_MODEL=us.anthropic.claude-opus-4-8`（opus-5 该账号被 IAM 拒绝）。
  想按客户端选择就把该环境变量设空。

---

## 6.（可选）接入 litellm

quick-gateway 是 Anthropic 兼容接口，可直接作为 litellm 的一个 upstream：

1. 让 quick-gateway 容器加入 litellm 所在的 docker 网络（把 compose 的 `networks.default`
   设成那张外部网络，如 `kiro-gateway-network`），并把端口绑到 `127.0.0.1`（不对公网暴露，
   由 litellm 做鉴权边界）。
2. 在 litellm `config.yaml` 的 `model_list` 加一条（**改前先备份 config.yaml**）：
   ```yaml
   - litellm_params:
       api_base: http://quick-gateway:8000/quick   # litellm 会自动补 /v1/messages
       api_key: <随意，quick-gateway 会忽略>
       model: anthropic/claude-opus-quick
       input_cost_per_token: 1.5e-05
       output_cost_per_token: 7.5e-05
     model_name: claude-opus-quick
   ```
3. `docker restart litellm` 重新加载（config 是挂载进去的）。
4. 用 `model=claude-opus-quick` 调 litellm 的 `/v1/messages` 验证。

---

## 7.（规划中）多账号 / 多密钥

模仿 `kiro-gateway-N` + `kiro-nginx` 的做法：每个 Quick 账号一份自己的
`gateway-creds.json`、一个 `quick-gateway-N` 容器，前面加一个 `quick-nginx` 做负载均衡，
再把 litellm 的 `claude-opus-quick` 指向 `quick-nginx`。当前单容器即将来的 `quick-gateway-1`。
