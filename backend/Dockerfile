# 后端镜像（工单 0013）：FastAPI + uvicorn，数据全部经 /app/data 卷持久化。
# 环境配置（API Key、模型、限流参数）全部经环境变量注入，见根目录 .env.example。
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# 容器内固定数据路径：SQLite / Milvus Lite / 生成文件 / 公开链接，
# 与 docker-compose 的数据卷挂载点一致（nginx 需挂同一路径，符号链接才有效）
ENV ATOMS_DATABASE_URL=sqlite:////app/data/atoms.db \
    ATOMS_STORAGE_ROOT=/app/data/storage \
    ATOMS_MILVUS_URI=/app/data/milvus/atoms.db

RUN mkdir -p /app/data
VOLUME /app/data
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# 应用工厂模式：必须带 --factory（app.main 只暴露 create_app）
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
