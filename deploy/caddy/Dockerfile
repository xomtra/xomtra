# syntax=docker/dockerfile:1
# Caddy with a DNS provider plugin: certificates are issued via ACME DNS-01, so no
# port has to be opened for Let's Encrypt and issuance works before the port forward.
# In the local access mode (ADR 056) it uses its own local CA instead.
#   docker build -t hammond-caddy deploy/caddy

FROM caddy:2.11.9-builder@sha256:f9beaa5ddbe7df6f26ebbcf9cbf44e05a1917b9bba464f0c3229038d7e2e6e13 AS build
ARG CADDY_DNS_PLUGIN=github.com/caddy-dns/cloudflare@v0.2.4
# Security floors for transitive Go modules ahead of the next Caddy release
# (Trivy image scan). Drop an entry once Caddy ships the fixed version itself.
ARG GO_MODULE_FLOORS="golang.org/x/net@v0.62.0"
# The builder image sets XCADDY_SETCAP=1, which puts cap_net_bind_service on the built
# binary. A binary with file capabilities cannot be executed under compose's
# cap_drop: [ALL] (EPERM on exec), and Caddy listens on unprivileged ports anyway.
ENV XCADDY_SETCAP=0
RUN set -eu; extra=""; \
    for m in ${GO_MODULE_FLOORS}; do extra="$extra --replace ${m%@*}=$m"; done; \
    xcaddy build --with "${CADDY_DNS_PLUGIN}" $extra

FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
LABEL org.opencontainers.image.source="https://github.com/xomtra/Xomtra" \
      org.opencontainers.image.description="HAmmond reverse proxy (Caddy + DNS-01)" \
      org.opencontainers.image.licenses="Apache-2.0"
# CI starts the image with cap_drop ALL to catch file capabilities on the binary.
RUN apk upgrade --no-cache \
 && addgroup -S -g 10002 caddy \
 && adduser -S -D -H -u 10002 -G caddy caddy \
 && mkdir -p /data /config \
 && chown -R caddy:caddy /data /config
COPY --from=build /usr/bin/caddy /usr/bin/caddy
COPY Caddyfile Caddyfile.local /etc/caddy/
COPY --chmod=0755 start.sh /usr/local/bin/hammond-caddy
USER 10002:10002
# Unprivileged ports inside the container; compose maps 443 -> 8443.
EXPOSE 8443
# Chooses Caddyfile (public) or Caddyfile.local by HAMMOND_ACCESS_MODE (ADR 056).
CMD ["/usr/local/bin/hammond-caddy"]
