# syntax=docker/dockerfile:1.7
# Edge: the dashboard's production build served by Caddy, which also fronts the API.
# Build context: the repository root (see docker-compose.yml).

# Base images are pinned by digest (Dependabot proposes updates).
FROM node:26.9.0-alpine@sha256:dbaa92e5758cbbcf85d65d5403fdb530fe3442cbe8c6dbfb7ef23365450d5070 AS web
WORKDIR /web
# Dependencies first: this layer is reused until the lockfile changes.
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build
# Compress text assets once, at build time, with the strongest settings; Caddy serves whichever
# encoding the browser accepts without spending CPU per request.
RUN apk add --no-cache brotli zstd \
 && find dist -type f -size +1k \
      \( -name '*.js' -o -name '*.mjs' -o -name '*.css' -o -name '*.html' -o -name '*.svg' \
         -o -name '*.json' -o -name '*.webmanifest' -o -name '*.txt' -o -name '*.wasm' \) \
      -exec gzip -9 -k -n {} \; -exec brotli -q 11 -k {} \; -exec zstd -19 -q -k {} \;

# The API reference page: Swagger UI at a pinned version, checked against the lockfile, and served
# from this image like the dashboard (no third-party script at runtime). Install scripts stay off:
# the package's analytics dependency would otherwise report the install.
FROM node:26.9.0-alpine@sha256:dbaa92e5758cbbcf85d65d5403fdb530fe3442cbe8c6dbfb7ef23365450d5070 AS docs
WORKDIR /docs
ENV SCARF_ANALYTICS=false
COPY infra/edge/docs/package.json infra/edge/docs/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm \
    npm ci --no-audit --no-fund --ignore-scripts
COPY infra/edge/docs/index.html infra/edge/docs/reference.js ./
RUN mkdir out \
 && cp index.html reference.js node_modules/swagger-ui-dist/swagger-ui-bundle.js \
       node_modules/swagger-ui-dist/swagger-ui.css out/

FROM caddy:2.11.4-alpine@sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b
# The image grants the binary cap_net_bind_service so it could bind :80 as a non-root user. This
# edge listens on :8080 and runs with no capabilities at all, which the kernel only allows for a
# binary that does not ask for any.
RUN apk add --no-cache --virtual .setcap libcap-setcap \
 && setcap -r /usr/bin/caddy \
 && apk del .setcap \
 && addgroup -S -g 10003 edge \
 && adduser -S -D -H -h /nonexistent -s /sbin/nologin -u 10003 -G edge edge
COPY --chmod=0444 infra/caddy/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv
COPY --from=docs /docs/out /srv/docs
RUN caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

USER 10003:10003
EXPOSE 8080
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
