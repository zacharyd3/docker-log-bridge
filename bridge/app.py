"""HTTP API Home Assistant calls (via rest_command) to fetch recent logs.

    GET  /api/health            liveness
    GET  /api/hosts             which Docker hosts answer
    GET  /api/containers        inventory with state/health
    GET  /api/logs?minutes=10   the LLM-ready report (also POST with a JSON body)

Every /api/logs parameter can come from the query string or a JSON body, which
is what the Home Assistant blueprint sends (no URL-encoding headaches with
regexes). Add `format=text` to get just the report text - handy in a browser.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, Response, jsonify, request

from .collector import Collector, Query, parse_hosts, split_list
from .logproc import LEVELS

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("docker-log-bridge")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _tz():
    name = os.environ.get("TZ", "UTC") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown TZ %r, using UTC", name)
        return ZoneInfo("UTC")


HOSTS = parse_hosts(os.environ.get("DOCKER_HOSTS", "unix:///var/run/docker.sock|local"))
TOKEN = os.environ.get("API_TOKEN", "")
DEFAULTS = {
    "minutes": float(os.environ.get("DEFAULT_MINUTES", 10)),
    "level": os.environ.get("DEFAULT_LEVEL", "info").lower(),
    "max_lines": _env_int("DEFAULT_MAX_LINES", 150),
    "max_chars": _env_int("DEFAULT_MAX_CHARS", 40000),
    "exclude": split_list(os.environ.get("EXCLUDE_CONTAINERS", "")),
    "ignore": os.environ.get("IGNORE_PATTERN", ""),
}
COLLECTOR = Collector(HOSTS, _tz(), docker_timeout=_env_int("DOCKER_TIMEOUT", 20),
                      workers=_env_int("WORKERS", 8))

app = Flask(__name__)


class BadRequest(Exception):
    pass


@app.errorhandler(BadRequest)
def _bad_request(exc):
    return jsonify(error=str(exc)), 400


@app.before_request
def _auth():
    if not TOKEN or request.path == "/api/health":
        return None
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() \
        or request.args.get("token", "")
    if not hmac.compare_digest(supplied.encode(), TOKEN.encode()):
        return jsonify(error="unauthorized"), 401
    return None


def _params() -> dict:
    params = dict(request.args)
    if request.method == "POST":
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            params.update({k: v for k, v in body.items() if v is not None})
        elif request.data.strip():
            raise BadRequest("body must be a JSON object")
    params.pop("token", None)
    return params


def _bool(value, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _num(params, key, default, cast, lo, hi):
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = cast(float(raw))
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"{key} must be a number") from exc
    if value <= 0:  # 0 / negative = "use the default" (blueprint-friendly)
        return default
    return max(lo, min(hi, value))


def _query(params: dict) -> Query:
    level = str(params.get("level") or DEFAULTS["level"]).lower()
    level = {"warn": "warning", "err": "error", "all": "debug"}.get(level, level)
    if level not in LEVELS:
        raise BadRequest(f"level must be one of {', '.join(LEVELS)} (or 'all')")

    patterns = [p for p in (DEFAULTS["ignore"], str(params.get("ignore") or "")) if p.strip()]
    try:
        ignore = re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE) if patterns else None
    except re.error as exc:
        raise BadRequest(f"ignore is not a valid regex: {exc}") from exc

    return Query(
        minutes=_num(params, "minutes", DEFAULTS["minutes"], float, 0.5, 1440),
        hosts=split_list(params.get("hosts")) or None,
        containers=split_list(params.get("containers")) or None,
        exclude=DEFAULTS["exclude"] + split_list(params.get("exclude")),
        ignore=ignore,
        level=level,
        max_lines=_num(params, "max_lines", DEFAULTS["max_lines"], int, 5, 5000),
        max_chars=_num(params, "max_chars", DEFAULTS["max_chars"], int, 2000, 2_000_000),
        include_state=_bool(params.get("include_state"), True),
    )


@app.get("/")
def index():
    return jsonify(
        service="docker-log-bridge",
        hosts=[h.name for h in HOSTS],
        endpoints={
            "/api/health": "liveness",
            "/api/hosts": "reachability of each Docker host",
            "/api/containers": "container inventory (hosts, containers, exclude filters apply)",
            "/api/logs": "report for HA: minutes, level (all|info|warning|error), hosts, containers, "
                         "exclude, ignore (regex), max_lines, max_chars, include_state, format=text",
        },
        defaults={k: v for k, v in DEFAULTS.items()},
    )


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.get("/api/hosts")
def hosts():
    return jsonify(hosts=COLLECTOR.host_status())


@app.get("/api/containers")
def containers():
    return jsonify(COLLECTOR.containers(_query(_params())))


@app.route("/api/logs", methods=["GET", "POST"])
def logs():
    params = _params()
    report = COLLECTOR.report(_query(params))
    if str(params.get("format", "json")).lower() == "text":
        return Response(report["text"], mimetype="text/plain")
    return jsonify(report)


def main() -> None:
    from waitress import serve

    port = _env_int("PORT", 8080)
    log.info("docker-log-bridge on :%s, hosts: %s", port,
             ", ".join(f"{h.name}={h.url}" for h in HOSTS))
    if not TOKEN:
        log.info("API_TOKEN not set - API is open to anything that can reach this port")
    serve(app, host="0.0.0.0", port=port, threads=_env_int("HTTP_THREADS", 4))


if __name__ == "__main__":
    sys.exit(main())
