#!/usr/bin/env bash
# 阿里云 ECS 一键部署脚本（工单 0014）：在仓库根目录执行。
#
# 前置（一次性，见 docs/deployment.md）：
#   1. ECS ≥2C4G，Ubuntu 22.04/24.04，安全组放行 80 端口
#   2. 代码已同步到 ECS（git clone 或 scp）
#   3. .env 已填写 ATOMS_LLM_API_KEY（本脚本只自动生成 ATOMS_JWT_SECRET）
#
# 用法：bash deploy/aliyun/deploy.sh
set -euo pipefail

cd "$(dirname "$0")/../.."
REPO_ROOT=$(pwd)

echo "==> 检查 Docker"
if ! command -v docker >/dev/null 2>&1; then
    echo "    未安装 Docker，开始安装（官方脚本，国内网络慢可改用镜像源）..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "FAIL: 缺少 docker compose 插件，请先安装 docker-compose-plugin" >&2
    exit 1
fi

echo "==> 准备 .env"
if [ ! -f .env ]; then
    cp .env.example .env
    # 自动生成 >=32 字节随机 JWT 密钥
    SECRET=$(openssl rand -hex 32)
    sed -i "s|^ATOMS_JWT_SECRET=.*|ATOMS_JWT_SECRET=${SECRET}|" .env
    echo "    已从 .env.example 生成 .env 并写入随机 ATOMS_JWT_SECRET"
fi
# shellcheck disable=SC1091
source .env
if [ -z "${ATOMS_LLM_API_KEY:-}" ]; then
    echo "FAIL: .env 未设置 ATOMS_LLM_API_KEY，请填写后重试" >&2
    exit 1
fi

echo "==> 构建并启动容器"
docker compose up -d --build

echo "==> 等待后端就绪"
# 对外端口取 .env 的 ATOMS_HTTP_PORT（与 docker-compose 的端口映射同口径）
PORT="${ATOMS_HTTP_PORT:-80}"
for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        break
    fi
    sleep 2
    if [ "$i" = "30" ]; then
        echo "FAIL: 后端 60s 内未就绪，查看日志：docker compose logs backend" >&2
        exit 1
    fi
done

IP=$(curl -fsS --max-time 5 http://100.100.100.200/latest/meta-data/eipv4 2>/dev/null || echo "<ECS-IP>")
if [ "$PORT" = "80" ]; then BASE_URL="http://${IP}"; else BASE_URL="http://${IP}:${PORT}"; fi
echo
echo "==> 部署完成：${BASE_URL}"
echo "    线上验收：python3 deploy/smoke/smoke_online.py ${BASE_URL}"
echo "    数据位置：docker volume atoms-demo_atoms-data（重启不丢）"
echo "    备份方法见 docs/deployment.md"
