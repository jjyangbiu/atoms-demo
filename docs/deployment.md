# 部署文档（工单 0013 / 0014）

两条可选路径，链路语义完全一致：

- **免 Docker 裸机部署（演示推荐，见第 2 节）**：nginx + uvicorn（systemd），组件最少、排查最直接；
  `/p/` 与 `/api` 都反代后端，后端自带发布校验与 CSP 防护。
- **Docker Compose（工单 0013，见第 1 / 4 节）**：**web**（nginx：前端 SPA + `/p/` 公开静态直出 + `/api` 反代）
  与 **backend**（FastAPI/uvicorn）两容器，数据落 **atoms-data** 卷，重启不丢。

## 1. 容器化快速开始（本地 / ECS 相同）

```bash
cp .env.example .env     # 填写 ATOMS_JWT_SECRET 与 ATOMS_LLM_API_KEY
docker compose up -d --build
curl http://127.0.0.1/api/health    # {"ok": true}
```

阿里云 ECS 上可直接执行一键脚本（自动装 Docker、生成 JWT 密钥、构建启动）：

```bash
bash deploy/aliyun/deploy.sh
```

## 2. 免 Docker 裸机部署（演示推荐）

后端本身就同时承担 `/api`、`/preview/` 鉴权托管与 `/p/` 公开托管，前端只是一份静态产物，
因此不依赖容器：nginx 出静态 + 反代，uvicorn 跑后端，数据落 `backend/data/`。

```bash
# 在仓库根目录（代码已在机器上，见第 5 节同步方式）
bash deploy/aliyun/deploy-bare.sh
# 首次运行会生成 /opt/atoms/backend/.env 并停住：填入 ATOMS_LLM_API_KEY 后重跑即可
```

脚本一次性完成：装 python3/nginx/nodejs → 同步源码到 `/opt/atoms`（保留线上 `.env` 与 `data/`）→
venv 装依赖 → 生成随机 `ATOMS_JWT_SECRET` → systemd 守护 uvicorn（`atoms-backend`）→
`npm ci && npm run build` → 安装站点配置 `deploy/aliyun/nginx-bare.conf` → 健康检查。

与容器化方案的差别：`/p/` 不直出静态而是反代后端（发布校验与 CSP sandbox 由后端完成，
无需符号链接）；配置仍写在 `backend/.env`（`ATOMS_` 前缀，清单同第 3 节，
存储路径用默认相对值、无需容器内固定路径）。

## 3. 环境变量清单

全部经环境变量注入（`ATOMS_` 前缀，语义与默认值见 `backend/app/config.py`），compose 读取仓库根目录 `.env`：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `ATOMS_JWT_SECRET` | 是 | 登录令牌签名密钥，>=32 字节随机串（`openssl rand -hex 32`） |
| `ATOMS_LLM_API_KEY` | 是 | MiniMax（OpenAI 兼容）API Key；生成与 embedding 共用 |
| `ATOMS_LLM_BASE_URL` | 否 | 默认 `https://api.minimaxi.com/v1` |
| `ATOMS_LLM_MODEL` | 否 | 默认 `MiniMax-M3` |
| `ATOMS_LLM_TEMPERATURE` | 否 | 默认 `0.2` |
| `ATOMS_AGENT_MAX_STEPS` / `ATOMS_AGENT_MAX_RETRIES` | 否 | 智能体工具循环步数 / 失败重试，默认 `20` / `2` |
| `ATOMS_EMBEDDING_MODEL` | 否 | 默认 `embo-01`；支持 `provider:model` 前缀（自动取冒号后模型名） |
| `ATOMS_EMBEDDING_BASE_URL` | 否 | 独立 OpenAI 兼容 embedding 端点；置空复用 LLM 端点 |
| `ATOMS_EMBEDDING_API_KEY` | 否 | 置空复用 `ATOMS_LLM_API_KEY` |
| `ATOMS_RATE_LIMIT_PER_USER_HOURLY` | 否 | 每用户每小时生成上限，默认 `10`（线上保守值） |
| `ATOMS_RATE_LIMIT_MAX_CONCURRENT` | 否 | 全局并发上限，默认 `2`（线上保守值） |
| `ATOMS_JWT_EXPIRES_MINUTES` | 否 | 登录有效期，默认 `10080`（7 天） |
| `ATOMS_DATABASE_URL` | 否 | 容器内固定 `sqlite:////app/data/atoms.db`，勿改 |
| `ATOMS_STORAGE_ROOT` | 否 | 容器内固定 `/app/data/storage`，勿改 |
| `ATOMS_MILVUS_URI` | 否 | 容器内固定 `/app/data/milvus/atoms.db`，勿改 |
| `ATOMS_HTTP_PORT` | 否 | nginx 宿主端口，默认 `80` |
| `ATOMS_CORS_ORIGINS` | 否 | 同源部署留空；本地前端直连容器时填 `http://localhost:5173` |

> 存储路径三项之所以固定：backend 与 web 把同一数据卷挂到**同一路径** `/app/data`，
> 公开链接 `/p/{slug}` 的符号链接（`backend/app/public_links.py` 维护，绝对目标）才能两边解析。

## 4. 链路与验收对照（工单 0013）

| 验收项 | 实现 |
| --- | --- |
| 注册登录 / 生成 / 预览 / 公开链接经容器链路可用 | `docker compose up -d` 后浏览器打开 `http://127.0.0.1` 全链路操作；或跑冒烟脚本见下 |
| `/p/` 由 nginx 直出 | `frontend/nginx.conf` 的 `location /p/` 按符号链接直出静态文件，不经后端 |
| `/preview/` 经鉴权代理 | `/api/projects/{id}/preview/...` 走 `/api` 反代，后端按登录 Cookie 鉴权 |
| `/api` 反代 | `proxy_pass http://backend:8000`，SSE 关缓冲、长超时 |
| 重启不丢数据 | 全部状态在 `atoms-data` 卷；后端启动按发布记录重建 `/p/` 链接（`public_links.resync`） |

全链路冒烟（本地验证容器链路与线上验收共用同一脚本）：

```bash
python deploy/smoke/smoke_online.py http://127.0.0.1
```

## 5. 阿里云部署（工单 0014）

> 说明：仓库未配置 CI/CD 流水线，发布由人工执行：本机完成 CI 检查（见第 7 节）后，
> 按下述步骤把代码同步到 ECS 手动部署；后续变更重复步骤 2–4 即可（数据不受影响）。
> 演示推荐免 Docker 路径（`deploy-bare.sh`）；容器化路径（`deploy.sh`）为工单 0013 的完整形态。

**外部事实**：需先购置 ECS（建议 ≥2C4G，Ubuntu 22.04/24.04）并准备 MiniMax API Key。

1. 安全组放行 80 端口（本期不做 HTTPS/域名）。
2. 把代码同步到 ECS（任选其一）：
   - 有 git 仓库：`git clone <仓库地址> /root/atoms`（或任意目录）；后续更新 `git pull`。
   - 无仓库：本机打包后上传，例如（PowerShell）：
     `scp -r backend frontend deploy docker-compose.yml .env.example root@<IP>:/root/atoms/`
     （不要上传本地 `.env`、`backend/data`、`node_modules`、`.venv`）
3. `cd` 到仓库根目录执行一键脚本：
   - **免 Docker（推荐）**：`bash deploy/aliyun/deploy-bare.sh`；首次会生成 `/opt/atoms/backend/.env`，
     填入 `ATOMS_LLM_API_KEY` 后重跑。
   - **容器化**：`bash deploy/aliyun/deploy.sh`；自动生成随机 `ATOMS_JWT_SECRET`，但需在根目录 `.env`
     填 `ATOMS_LLM_API_KEY`。ECS 在国内拉取 Docker Hub 基础镜像通常正常；若遇拉取失败，在 Docker 的
     `daemon.json` 配置 `registry-mirrors`（如阿里云容器镜像服务提供的加速器）后重跑。
4. 线上验收冒烟：`python3 deploy/smoke/smoke_online.py http://<ECS-IP>`，覆盖
   注册 → 建项目（工程师模式）→ “做一个番茄钟” → 预览 → 迭代“加一个统计区” → 发布 →
   匿名打开 `/p/{slug}` → 另一账号克隆 → 继续迭代。

限流默认即保守值（每用户每小时 10 次、全局并发 2），可按需在配置中调整。

## 6. 运维操作

```bash
docker compose logs -f backend        # 查看后端日志
docker compose restart                # 重启（数据不丢）
docker compose down                   # 停止（保留数据卷）
docker compose down -v                # 停止并删除数据（危险！）
```

裸机部署对应操作：`journalctl -u atoms-backend -f`（日志）、`systemctl restart atoms-backend`（重启），
数据在 `/opt/atoms/backend/data/`，更新发布重跑 `deploy-bare.sh` 即可（脚本不会触碰数据目录）。

**备份**：容器化部署数据全部在 `atoms-demo_atoms-data` 卷：

```bash
docker run --rm -v atoms-demo_atoms-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/atoms-data-$(date +%F).tar.gz -C /data .
```

恢复：`docker compose up -d` 后 `docker cp`/tar 解压回卷，再 `docker compose restart backend`。
裸机部署直接 `tar czf` 打包 `/opt/atoms/backend/data/` 即可。

**Milvus Lite 内存回退路径**（见 ADR 0002）：若小内存 ECS 上向量库吃力，可关闭知识库相关能力
（清空 `ATOMS_EMBEDDING_*` 并保留最小配置），语义检索自动降级为关键词匹配，不影响生成主链路。

## 7. 发布前检查（本机侧，替代 CI）

发布任何变更前，在本机依次执行并全部保持绿色：

```bash
cd backend && pytest                    # 后端全量测试（含公开链接直出）
cd frontend && npm run typecheck        # 前端类型检查
cd frontend && npm run build            # 生产构建（web 镜像内的同一条命令）
```

容器侧抽查（可选，需本机可拉取基础镜像）：`docker compose up -d --build` 后跑
`python deploy/smoke/smoke_online.py http://127.0.0.1`。
