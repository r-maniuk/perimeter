# syntax=docker/dockerfile:1.7
# The message broker: the official NATS server with this deployment's configuration, running as an
# unprivileged user. Password hashes are not part of the image; the secrets job provides them.
FROM nats:2.15.0-alpine@sha256:ac8f88a6494bffc2c2a5289a0ca61cb28a9145c11ba5677cf24265d07f46d8d4

RUN addgroup -S -g 10002 nats \
 && adduser -S -D -H -h /data -s /sbin/nologin -u 10002 -G nats nats \
 && install -d -o nats -g nats -m 0700 /data \
 && install -d -m 0755 /etc/nats/auth \
 && rm /etc/nats/nats-server.conf
COPY --chmod=0444 nats.conf /etc/nats/nats.conf

USER 10002:10002
EXPOSE 4222
CMD ["nats-server", "--config", "/etc/nats/nats.conf"]
