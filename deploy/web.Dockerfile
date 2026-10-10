# Build from the repository root: docker build -f deploy/web.Dockerfile .
FROM node:22-alpine AS build
WORKDIR /app

COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

# Keep the image build independent of local dist, generated data, and assets.
# TypeScript includes tests in its build input even though they are not shipped.
COPY web/index.html web/tsconfig.json web/vite.config.ts ./
COPY web/src/ ./src/
COPY web/tests/ ./tests/
RUN npm run build

FROM nginxinc/nginx-unprivileged:stable-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/ /usr/share/nginx/html/
RUN command -v wget && nginx -t
EXPOSE 8080
