# 入口容器（工单 0013）：前端静态 + /p/ 公开静态直出 + /api 反代（含 /preview/ 鉴权代理）。
# 构建分两段：node 产物 → nginx 运行时。
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Rollup 打包 monaco-editor 内存峰值较高，小内存主机（2C4G）构建时抬高 Node 堆上限，
# 避免 heap OOM；若主机物理内存+swap 仍不足，建议改为本地构建后挂载产物。
ENV NODE_OPTIONS=--max-old-space-size=2048
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
