#!/bin/bash
set -euo pipefail

# --- docker-log-bridge: build & (re)start on unraid ----------------------
# Fetches the latest source, builds the image and recreates the container.
# Run it again any time to update: it re-pulls main, rebuilds, and recreates.
#
# Settings you might want to change WITHOUT editing this file (it's
# re-downloaded on every update by unraid-update.sh) go in $DATA/bridge.env -
# it's created with commented examples on the first run and anything set there
# wins over the defaults below (DOCKER_HOSTS, API_TOKEN, IGNORE_PATTERN, ...).
SRC=/mnt/user/appdata/docker-log-bridge-src       # build context (throwaway)
DATA=/mnt/user/appdata/docker-log-bridge          # persistent: bridge.env
NAME=DockerLogBridge
IMAGE=docker-log-bridge:latest
PORT=8084                                         # host port -> container :8080
REPO=zacharyd3/docker-log-bridge
BRANCH=main

# Same `url|Label` list Dozzle uses. The local socket is mounted read-only.
DOCKER_HOSTS="unix:///var/run/docker.sock|$(hostname),tcp://192.168.250.3:2375|Mjolnir,tcp://192.168.250.2:2375|Thor"
# ------------------------------------------------------------------------

ENV_FILE="$DATA/bridge.env"
mkdir -p "$DATA"

if [ ! -f "$ENV_FILE" ]; then
  echo "==> Creating $ENV_FILE (edit it to override defaults, then re-run)"
  cat > "$ENV_FILE" <<'EOF'
# docker-log-bridge overrides - KEY=value, no quotes, no `export`.
# Anything set here wins over the defaults in install.sh.

# Docker hosts, same format as Dozzle (url|Label, comma-separated):
#DOCKER_HOSTS=unix:///var/run/docker.sock|Unraid,tcp://192.168.250.3:2375|Mjolnir,tcp://192.168.250.2:2375|Thor

# Require "Authorization: Bearer <token>" on every request except /api/health.
# If you set this, add the matching header to rest_command in Home Assistant.
#API_TOKEN=

# Containers never read (comma-separated names/globs, `Host/name` works too):
#EXCLUDE_CONTAINERS=dozzle,DockerLogBridge

# Lines dropped everywhere before analysis (case-insensitive regex):
#IGNORE_PATTERN=GET /(api/)?health|/ping\b

# Defaults for requests that don't say otherwise:
#DEFAULT_MINUTES=10
#DEFAULT_LEVEL=info
#DEFAULT_MAX_LINES=150
#DEFAULT_MAX_CHARS=40000

# Timezone used for timestamps in the report:
#TZ=America/Toronto
EOF
fi

# Only pass a default with -e when bridge.env doesn't set that key itself
# (docker gives -e precedence over --env-file).
env_args=()
set_default() {
  if ! grep -qE "^[[:space:]]*$1=" "$ENV_FILE"; then
    env_args+=(-e "$1=$2")
  fi
}
host_tz() {
  sed -n 's/^timeZone="\(.*\)".*/\1/p' /boot/config/ident.cfg 2>/dev/null | grep / && return
  readlink /etc/localtime 2>/dev/null | sed -n 's#.*/zoneinfo/##p' | grep . && return
  echo UTC
}
set_default DOCKER_HOSTS "$DOCKER_HOSTS"
set_default TZ "$(host_tz | head -n1)"

# Start the build context clean so files removed upstream don't linger from a
# previous run and get baked into the image.
rm -rf "$SRC"
mkdir -p "$SRC"
cd "$SRC"

echo "==> Fetching latest source ($REPO @ $BRANCH)"
curl -fL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" \
  | tar xz --strip-components=1

echo "==> Building image ($IMAGE)"
docker build -t "$IMAGE" .

echo "==> Recreating container ($NAME)"
docker stop "$NAME" 2>/dev/null || true
docker rm   "$NAME" 2>/dev/null || true
docker run -d \
  --name "$NAME" \
  -p "${PORT}:8080" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  --env-file "$ENV_FILE" \
  ${env_args[@]+"${env_args[@]}"} \
  --restart unless-stopped \
  --label net.unraid.docker.webui='http://[IP]:[PORT:8080]/api/logs?format=text' \
  --label net.unraid.docker.icon="https://raw.githubusercontent.com/$REPO/$BRANCH/icon.png" \
  "$IMAGE"

# Drop the dangling image the rebuild left behind.
docker image prune -f >/dev/null 2>&1 || true

HOST_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}')
HOST_IP=${HOST_IP:-localhost}

echo "==> Waiting for the API"
for _ in $(seq 1 20); do
  if curl -fs "http://localhost:${PORT}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> Docker hosts as seen by the bridge:"
TOKEN=$(sed -n 's/^[[:space:]]*API_TOKEN=//p' "$ENV_FILE" | tail -n1)
curl -fs ${TOKEN:+-H "Authorization: Bearer $TOKEN"} "http://localhost:${PORT}/api/hosts" \
  || echo "    (couldn't query /api/hosts - check: docker logs $NAME)"
echo

echo "==> Done. docker-log-bridge is up on http://${HOST_IP}:${PORT}/"
echo "    Try it:   curl -s 'http://${HOST_IP}:${PORT}/api/logs?minutes=10&format=text'"
echo "    Settings: $ENV_FILE (re-run this script after editing)"
