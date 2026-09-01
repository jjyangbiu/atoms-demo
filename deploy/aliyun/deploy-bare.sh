#!/usr/bin/env bash
# 阿里云 ECS 免 Docker 一键部署（工单 0014，演示推荐路径）。
# 架构：python venv + uvicorn（systemd 守护）+ nginx（前端静态 + /api 与 /p/ 反代）。
#
# 前置：ECS ≥2C4G Ubuntu 22.04/24.04，安全组放行 80；代码已在仓库根目录就绪。
# 用法：在仓库根目录执行  bash deploy/aliyun/deploy-bare.sh
# 更新发布：把新代码同步到 /opt/atoms 后，重跑本脚本即可（数据在 /opt/atoms/backend/data，不受影响）。
set -euo pipefail

cd "$(dirname "$0")/../.."
DEPLOY_DIR=/opt/atoms

echo "==> 安装系统依赖（python3 / nginx / nodejs）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx curl ca-certificates
if ! command -v node >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    apt-get install -y nodejs
fi

echo "==> 同步代码到 ${DEPLOY_DIR}（只拷源码：保留线上 backend/.env 与 backend/data）"
mkdir -p "$DEPLOY_DIR/backend" "$DEPLOY_DIR/frontend"
cp -r backend/app backend/scripts backend/requirements.txt "$DEPLOY_DIR/backend"/
cp -r frontend/src frontend/index.html frontend/package.json frontend/package-lock.json \
      frontend/tsconfig.json frontend/vite.config.ts "$DEPLOY_DIR/frontend"/
cp -r deploy "$DEPLOY_DIR"/

echo "==> 后端：虚拟环境与依赖"
cd "$DEPLOY_DIR/backend"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -U pip
./.venv/bin/pip install -r requirements.txt

echo "==> 后端：.env 配置"
if [ ! -f .env ]; then
    cat > .env <<EOF
# 免 Docker 部署配置：路径用 config.py 默认值（相对 backend/ 目录，数据落 backend/data/）
ATOMS_JWT_SECRET=$(openssl rand -hex 32)
# 必填：MiniMax API Key
ATOMS_LLM_API_KEY=
ATOMS_LLM_BASE_URL=https://api.minimaxi.com/v1
ATOMS_LLM_MODEL=MiniMax-M3
# 限流保守值（工单 0014）
ATOMS_RATE_LIMIT_PER_USER_HOURLY=10
ATOMS_RATE_LIMIT_MAX_CONCURRENT=2
EOF
    echo "    已生成 ${DEPLOY_DIR}/backend/.env，请填入 ATOMS_LLM_API_KEY 后重跑本脚本"
    exit 0
fi
if ! grep -Eq '^ATOMS_LLM_API_KEY=.+' .env; then
    echo "FAIL: ${DEPLOY_DIR}/backend/.env 未设置 ATOMS_LLM_API_KEY" >&2
    exit 1
fi

echo "==> 后端：systemd 服务"
cat > /etc/systemd/system/atoms-backend.service <<EOF
[Unit]
Description=Atoms backend (FastAPI/uvicorn)
After=network.target

[Service]
WorkingDirectory=${DEPLOY_DIR}/backend
ExecStart=${DEPLOY_DIR}/backend/.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable atoms-backend
systemctl restart atoms-backend

echo "==> 前端：生产构建"
cd "$DEPLOY_DIR/frontend"
npm ci
npm run build

echo "==> nginx：站点配置"
cp "$DEPLOY_DIR/deploy/aliyun/nginx-bare.conf" /etc/nginx/sites-available/atoms
ln -sf /etc/nginx/sites-available/atoms /etc/nginx/sites-enabled/atoms
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl restart nginx

echo "==> 等待后端就绪"
for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1/api/health >/dev/null 2>&1; then
        break
    fi
    sleep 2
    if [ "$i" = "30" ]; then
        echo "FAIL: 后端 60s 内未就绪，查看日志：journalctl -u atoms-backend -n 100" >&2
        exit 1
    fi
done

IP=$(curl -fsS --max-time 5 http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null || echo "<ECS-IP>")
echo
echo "==> 部署完成：http://${IP}"
echo "    线上验收：python3 ${DEPLOY_DIR}/deploy/smoke/smoke_online.py http://${IP}"
echo "    数据位置：${DEPLOY_DIR}/backend/data/（SQLite / Milvus Lite / 生成文件）"
echo "    后端日志：journalctl -u atoms-backend -f"
