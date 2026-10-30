# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Campus Twin 3D 可视化大屏（Vite 构建 → nginx 静态托管 :80，映射宿主 5173）
# 构建：docker build -t campus-twin/frontend:local ./frontend
# 运行：见根目录 docker-compose.yml
#
# 说明：本机未安装 Docker，本文件与 compose 仅做静态校验，未在本机 build/run 实测。
#       前端 API 地址是“浏览器侧地址”，容器内构建默认写 http://127.0.0.1:8000/8100
#       （即宿主机端口映射）；如部署到远端，请在 compose 中覆盖 build args。
# ---------------------------------------------------------------------------
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY index.html vite.config.js ./
COPY src ./src

ARG VITE_API_BASE=http://127.0.0.1:8000
ARG VITE_AGENT_BASE=http://127.0.0.1:8100
ENV VITE_API_BASE=$VITE_API_BASE \
    VITE_AGENT_BASE=$VITE_AGENT_BASE

RUN npm run build

# ---- 运行时：nginx 静态托管（SPA fallback）----
FROM nginx:1.27-alpine

RUN printf 'server {\n    listen 80;\n    server_name _;\n\n    root /usr/share/nginx/html;\n    index index.html;\n\n    location / {\n        try_files $uri $uri/ /index.html;\n    }\n}\n' > /etc/nginx/conf.d/default.conf

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
