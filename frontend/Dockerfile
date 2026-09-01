# 入口容器（工单 0013）：前端静态 + /p/ 公开静态直出 + /api 反代（含 /preview/ 鉴权代理）。
# 构建分两段：node 产物 → nginx 运行时。
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
