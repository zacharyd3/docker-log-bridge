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
PORT=8084                                         # preferred host port -> container :8080
                                                  # (if taken, the next free one is picked and
                                                  #  saved as HOST_PORT in bridge.env)
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

# Host port for the API (Home Assistant's "Bridge URL" uses it). Set
# automatically on install if the default 8084 is taken:
#HOST_PORT=8084

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

# Pick the host port. HOST_PORT in bridge.env wins; otherwise use $PORT, or
# the next free port after it - and save that choice so it never moves.
port_users() {
  docker ps --format '{{.Names}} {{.Ports}}' | grep -E "(^| |,)(0\.0\.0\.0|\[?::\]?|[0-9.]+):$1->" || true
  if command -v ss >/dev/null 2>&1; then
    ss -ltnH "( sport = :$1 )" 2>/dev/null | sed 's/^/listening: /' || true
  fi
}
SAVED_PORT=$(sed -n 's/^[[:space:]]*HOST_PORT=\([0-9][0-9]*\).*/\1/p' "$ENV_FILE" | tail -n1)
if [ -n "$SAVED_PORT" ]; then
  PORT=$SAVED_PORT
  if [ -n "$(port_users "$PORT")" ]; then
    echo "!! Port $PORT (HOST_PORT in $ENV_FILE) is already in use by:" >&2
    port_users "$PORT" | sed 's/^/     /' >&2
    echo "   Change or remove HOST_PORT in $ENV_FILE and re-run." >&2
    exit 1
  fi
else
  WANTED=$PORT
  while [ -n "$(port_users "$PORT")" ]; do
    PORT=$((PORT + 1))
    if [ "$PORT" -gt $((WANTED + 200)) ]; then
      echo "!! No free port found between $WANTED and $PORT - set HOST_PORT in $ENV_FILE" >&2
      exit 1
    fi
  done
  if [ "$PORT" != "$WANTED" ]; then
    echo "==> Port $WANTED is taken ($(port_users "$WANTED" | head -n1 | cut -d' ' -f1)); using $PORT"
    echo "    Saved as HOST_PORT=$PORT in $ENV_FILE so it stays put on updates."
  fi
  printf '\n# Chosen by install.sh - change it here if you need a different port.\nHOST_PORT=%s\n' "$PORT" >> "$ENV_FILE"
fi
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

HOST_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") print $(i + 1)}' || true)
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
echo "    Home Assistant blueprint \"Bridge URL\": http://${HOST_IP}:${PORT}"
echo "    Try it:   curl -s 'http://${HOST_IP}:${PORT}/api/logs?minutes=10&format=text'"
echo "    Settings: $ENV_FILE (re-run this script after editing)"
