FROM node:22-alpine AS web-build

WORKDIR /web
COPY package.json package-lock.json ./
# The existing lock file predates part of the template dependency tree.
# Resolve it in the disposable build stage without rewriting the host lock file.
RUN npm install --ignore-scripts --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY infra/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=web-build /web/dist /usr/share/nginx/html
EXPOSE 80
