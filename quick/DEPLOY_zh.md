# quick-gateway 认证原理（中文）

> **操作步骤（本地运行 / 跨机部署 / litellm 接入）看 [`quick/RUNBOOK.md`](RUNBOOK.md)。**
> 本文只讲**为什么**这么设计——认证怎么工作、token 生命周期、凭证文件的加载逻辑，
> 供理解与排查用，不重复命令。

## 1. 认证怎么工作

Amazon Quick 桌面端登录后，凭证存在 **macOS 钥匙串（Keychain）**里，而不是文件。
但真正让网关跑起来所需的东西全在那份凭证 JSON blob 里，且刷新走的是**公网 Keycloak**，
所以把这份 JSON 搬到任何机器就能脱离钥匙串独立运行——这就是跨机部署的基础。

关键链路：

1. **钥匙串取 blob**（仅 macOS，仅首次）：
   `security find-generic-password -s quickwork-enterprise-<profileId> -a session -w`
   （profileId 来自 `~/.quickwork/profiles.json` 的 `last_active`）。
2. **Keycloak 刷新**（公网，任何机器都能做）：`POST {token_endpoint}` 表单
   `grant_type=refresh_token&client_id=quick-desktop&refresh_token=...` → 换回新的
   `id_token`（DataPlane 的 bearer）、`access_token`，并**轮换** `refresh_token`。
3. **调用推理**：`POST {tenant_url}/integration/quick-work/bedrock-proxy-stream`，
   头 `Authorization: Bearer <id_token>`（是 id_token，不是 access_token）。

blob 字段（`~/.quickwork/gateway-creds.json`）：

| 字段 | 作用 | 跨机必需 |
|------|------|:---:|
| `refresh_token` | 换新 token 的长寿命凭证（~90 天，会轮换） | ✅ |
| `token_endpoint` | Keycloak 刷新地址 | ✅ |
| `client_id` | OIDC 客户端（`quick-desktop`，公开无 secret） | ✅ |
| `tenant_url` | 租户 DataPlane 地址（`https://qbs-<uuid>.dp.appintegrations...`） | ✅ |
| `id_token` / `access_token` | 短寿命 token（各 ~5 分钟） | 可空，自动刷出 |
| `region` / `user_arn` | 信息性字段 | 可选 |

## 2. 凭证文件的加载优先级（`QuickAuthManager`, `quick/auth.py`）

1. **缓存文件优先** `QUICK_CREDS_FILE`（默认 `~/.quickwork/gateway-creds.json`）：存在就直接用，
   **完全不碰钥匙串**——这是 Linux 唯一走的路径。
2. **钥匙串兜底**（仅当文件缺失 **且** 系统有 `security`，即 macOS）：读一次钥匙串，随即把 blob
   **原子写入**缓存文件（`0600`），以后启动走路径 1，不再弹密码框。
3. 两者都无 → 报错，提示从 Mac 拷贝该文件。

每次刷新后，轮换出的 refresh_token + 新 id_token 都**写回缓存文件**，保持自举。

## 3. token 生命周期（为什么日志频繁刷新）

id_token / access_token 的寿命是 Keycloak 定的 **300 秒**（后端设定，非本项目）。DataPlane 用
id_token 鉴权，每次请求前若发现它距过期 < 60s（`TOKEN_REFRESH_THRESHOLD_SECONDS`）就刷一批新的
——所以 `Refreshed ... 300s` 日志频繁是**正常**的。真正长寿命的是 **refresh_token（~90 天）**，
轮换后写回文件。

> ⚠️ 懒刷新只在「有请求」时触发。若网关连续 **>90 天零流量**，refresh_token 会静默过期，需重拷 creds。
> 有正常流量则无需担心；`QUICK_KEEPALIVE_INTERVAL`（默认每天一次）的后台保活也用于兜住闲置期。

## 4. 为什么只能在一台机器跑

refresh_token 每次刷新都会**轮换**。两台机器用同一份 creds 文件互相刷新，迟早把对方持有的
refresh_token 作废。所以网关只在**一台**主机跑；重拷 creds 的时机是：offline token 彻底过期
（~90 天无流量），或在 Mac 上重新登录了 Quick。

## 5. 多账号（账号池，已上线 2026-08-27）

**没有**照搬 `kiro-gateway-N` + `kiro-nginx`：Quick 每次响应都白送 `usageSummary`（该账号
剩余额度），所以网关自己就能按剩余额度选账号，而 nginx 看不见配额——那正是 kiro 那套最后
退化成"人肉注释 upstream"的原因。

一个容器，`quickwork/` 下每个 `gateway-creds*.json` 就是一个账号（文件名即账号名：
`gateway-creds.json`→`default`，`gateway-creds-b.json`→`b`）。**加账号 = 多放一个文件 + 重启**，
不动 nginx、不动 litellm。

- 选路：按会话剩余额度（按 10% 分桶）排序 → 在途请求数 → 已服务次数（同桶内轮询）。
- 失败自动换账号重试（配额/限流/凭证/后端错误）；400 这类请求本身有问题的**不换**。
- 固定绑定：`POST /quick/pin/{account}/v1/messages`（保底额度 / 调试用）。
- 状态页：<http://43.160.157.90:9090/>（只读、外网可见、无凭证信息）。

细节见 `quick/README.md` 的 "Account pool" 一节；日常操作见 `quick/RUNBOOK.md` §1/§3/§5。

> ⚠️ 上传凭证必须点名账号：`./quick/deploy.sh --creds b`。默认的 `./quick/deploy.sh` **不动凭证**——
> 容器一直在轮换并回写每个账号的文件，把 Mac 上那份旧副本盖回去等于弄死这个账号。
