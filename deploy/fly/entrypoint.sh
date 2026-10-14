#!/bin/sh
# Runs as root on Fly: fix ownership of the mounted volume, then exec the app as the unprivileged ff user (10001).
set -e
mkdir -p /data
chown 10001:10001 /data && chmod 700 /data
exec setpriv --reuid=10001 --regid=10001 --clear-groups --no-new-privs --inh-caps=-all --bounding-set=-all -- "$@"
