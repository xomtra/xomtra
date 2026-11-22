# syntax=docker/dockerfile:1
# Caddy with a DNS provider plugin: certificates are issued via ACME DNS-01, so no
# port has to be opened for Let's Encrypt and issuance works before the port forward.
#   docker build -t hammond-caddy deploy/caddy

FROM caddy:2.11.9-builder@sha256:f9beaa5ddbe7df6f26ebbcf9cbf44e05a1917b9bba464f0c3229038d7e2e6e13 AS build
ARG CADDY_DNS_PLUGIN=github.com/caddy-dns/cloudflare@v0.2.4
RUN xcaddy build --with "${CADDY_DNS_PLUGIN}"

FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
LABEL org.opencontainers.image.source="https://github.com/zettabyte-cz/hammond" \
      org.opencontainers.image.description="HAmmond reverse proxy (Caddy + DNS-01)" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN apk upgrade --no-cache \
 && addgroup -S -g 10002 caddy \
 && adduser -S -D -H -u 10002 -G caddy caddy \
 && mkdir -p /data /config \
 && chown -R caddy:caddy /data /config
COPY --from=build /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile /etc/caddy/Caddyfile
USER 10002:10002
# Unprivileged ports inside the container; compose maps 443 -> 8443.
EXPOSE 8443
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
