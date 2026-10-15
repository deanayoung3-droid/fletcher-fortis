# Fly.io wrapper around the released image. Fly mounts volumes owned by root, and the app runs as uid 10001,
# so this starts as root only long enough to hand /data to that user, then drops all privileges.
ARG FF_IMAGE=ghcr.io/deanayoung3-droid/fletcher-fortis:1.3.5
FROM ${FF_IMAGE}
USER root
COPY --chmod=0755 entrypoint.sh /usr/local/bin/ff-entrypoint
ENTRYPOINT ["/usr/local/bin/ff-entrypoint"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
