# docker-log-bridge

A small read-only HTTP API that reads container logs from the same Docker hosts
Dozzle uses (the local socket plus `tcp://…:2375` remotes) and serves them as a
compact, LLM-ready report. A Home Assistant blueprint calls it every few minutes
and has `ai_task.generate_data` look for problems.

```
 Docker hosts                     docker-log-bridge                 Home Assistant
 ─────────────                    ─────────────────                 ──────────────
 /var/run/docker.sock  ─┐                                           time_pattern /10
 tcp://…250.3:2375 Mjolnir ├──▶  POST /api/logs {minutes: 10}  ◀──  rest_command
 tcp://…250.2:2375 Thor ─┘        • last N min of every container    │
                                  • crash loops / unhealthy / OOM     ▼
                                  • repeats collapsed (x37)         ai_task.generate_data
                                  • trimmed to a size budget          │
                                                                      ▼
                                                                  notify / event
```

## What the AI gets

```
Docker logs from 2026-10-15 17:22 to 17:32 EDT (10 min), 3 host(s), 41 container(s).
Showing level >= warning. Line format: HH:MM:SS [D/I/W/E] (xN = repeated N times) message.

## Unreachable Docker hosts
- Mjolnir: Connection refused

## Container state problems
- Thor/sick: UNHEALTHY - last check: db ping failed
- Thor/crasher: stopped during window, exit code 3 (restart count 5)

## Logs

### Thor/plex (165 lines, 33 error)
17:31:24 [E] (x33, last 17:32:28) ERROR - Database is locked (attempt 7954)
    Traceback (most recent call last):
    File "/app/x.py", line 3, in sync
    sqlite3.OperationalError: database is locked

Other containers with output below 'warning': Thor/sonarr
```

The bridge does the cheap work before the AI sees anything:

* **Repeats collapse** into one line with a count. Numbers, hex and UUIDs are
  normalised first, so "attempt 7954" and "attempt 12" count as the same line.
* **Stack traces stay attached** to the line that started them.
* **Levels are detected** from explicit markers (`level=error`, `[WARN]`,
  JSON `"level"`, glog `E1015`) and fall back to keywords.
* **State problems are reported even with no logs**: restart loops,
  unhealthy health checks, containers that stopped or OOM'd during the window,
  and unreachable hosts.
* **There's a size budget.** When a report is too big, the least severe lines
  go first.
* The response includes **`has_findings`**, so the blueprint can skip the AI
  call entirely on a quiet window.

## Install on unraid

Same pattern as Salume Studio. Paste [`unraid-update.sh`](unraid-update.sh)
into the **User Scripts** plugin and run it:

```bash
#!/bin/bash
set -euo pipefail
REPO=zacharyd3/docker-log-bridge
BRANCH=main
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
echo "==> Downloading install.sh ($REPO @ $BRANCH)"
curl -fsSL "https://raw.githubusercontent.com/$REPO/$BRANCH/install.sh" -o "$TMP"
bash "$TMP"
```

[`install.sh`](install.sh) pulls the latest `main`, builds the image, and
recreates the `DockerLogBridge` container on port **8084**. The local Docker
socket is mounted read-only. Run it again any time to update.

By default it reads the local socket (labelled with the unraid hostname)
plus `tcp://192.168.250.3:2375|Mjolnir` and `tcp://192.168.250.2:2375|Thor`.
To change that or anything else, edit
**`/mnt/user/appdata/docker-log-bridge/bridge.env`**. It's created with commented
examples on the first run and is never overwritten, so your settings survive
updates. Re-run the script after editing it.

When the script finishes, it prints which hosts the bridge can reach:

```bash
curl -s http://192.168.250.4:8084/api/hosts
curl -s 'http://192.168.250.4:8084/api/logs?minutes=10&format=text'
```

### Elsewhere

```bash
docker compose up -d --build     # see docker-compose.yml
```

## Home Assistant

See [`home-assistant/README.md`](home-assistant/README.md). In short:

1. Add the `rest_command.docker_log_bridge` snippet to `configuration.yaml` and restart.
2. Import [`docker_log_analysis.yaml`](home-assistant/blueprints/docker_log_analysis.yaml)
   and create an automation from it: interval, look-back, hosts, containers,
   ignore regex, level, AI entity, extra instructions and notification actions
   are all configurable.

## API

| Endpoint | |
|---|---|
| `GET /api/health` | Liveness check (never needs the token) |
| `GET /api/hosts` | Reachability, container count and latency per Docker host |
| `GET /api/containers` | Container inventory with state, health and restart count |
| `GET` or `POST /api/logs` | The report: JSON by default, `format=text` for just the text |

`/api/logs` parameters, from the query string or a JSON body:

| Param | Default | |
|---|---|---|
| `minutes` | 10 | Look-back window (0.5 – 1440) |
| `level` | `info` | `all` / `info` / `warning` / `error`: lowest level shown |
| `hosts` | all | Comma-separated host labels |
| `containers` | all | Names or globs; `Host/name` also matches |
| `exclude` | – | Names or globs to skip (added to `EXCLUDE_CONTAINERS`) |
| `ignore` | – | Case-insensitive regex; matching lines are dropped (added to `IGNORE_PATTERN`) |
| `max_lines` | 150 | Unique lines per container, before the size budget applies |
| `max_chars` | 40000 | Size budget for the whole report |
| `include_state` | true | Include container state problems |

The JSON response has `text` (the report), `has_findings`, `stats`
(`errors`, `warnings`, `raw_lines`, `truncated`, …), `problems`, `hosts`,
`hosts_down`, `read_errors` and `noisiest`.

## Configuration (environment)

| Variable | Default | |
|---|---|---|
| `DOCKER_HOSTS` | `unix:///var/run/docker.sock\|local` | Dozzle format: `url\|Label,url\|Label` |
| `API_TOKEN` | – | If set, requires `Authorization: Bearer <token>` |
| `EXCLUDE_CONTAINERS` | – | Containers never read |
| `IGNORE_PATTERN` | – | Regex for lines always dropped |
| `DEFAULT_MINUTES` / `DEFAULT_LEVEL` / `DEFAULT_MAX_LINES` / `DEFAULT_MAX_CHARS` | 10 / info / 150 / 40000 | Defaults for requests that don't set them |
| `TZ` | UTC | Timezone for report timestamps (install.sh uses the unraid timezone) |
| `DOCKER_TIMEOUT` | 20 | Seconds per Docker API call |
| `PORT` | 8080 | Port inside the container |

## Security

The bridge only reads: it lists containers and fetches logs. It can't start,
stop or exec anything, and the local socket is mounted `:ro`. Logs can contain
secrets, though, and the API has no authentication by default. If anything
beyond your LAN can reach the port, set `API_TOKEN`. Keep in mind that the
`:2375` remote endpoints it reads from (the same ones Dozzle uses) are
unauthenticated Docker APIs that grant full control of those hosts.

## Development

```bash
pip install -r requirements.txt pytest
pytest
DOCKER_HOSTS='unix:///var/run/docker.sock|local' python -m bridge.app
```
