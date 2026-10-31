FROM node:24.22.0-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5 AS build
WORKDIR /app/frontend
COPY .nvmrc /app/.nvmrc
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN test "$(node --version)" = "v$(cat /app/.nvmrc)" \
    && test "$(npm --version)" = "$(node -p 'require("./package.json").engines.npm')" \
    && npm ci
COPY frontend/ ./
# Only these public settings enter the client. Backend .env is outside this stage.
ENV VITE_API_BASE_URL=/api \
    VITE_API_MOCKS=false
RUN npm run build

FROM nginx:1.30.0-alpine@sha256:0272e4604ed93c1792f03695a033a6e8546840f86e0de20a884bb17d2c924883
COPY LICENSE /usr/share/licenses/djedai/LICENSE
COPY infra/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/frontend/dist/ /usr/share/nginx/html/
EXPOSE 80
