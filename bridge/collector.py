"""Talks to the Docker hosts and builds the report HA asks for.

Hosts are configured exactly like Dozzle's remote hosts - a comma-separated
list of `url|Label` - so the same string can be copied straight across:

    DOCKER_HOSTS=unix:///var/run/docker.sock|Unraid,tcp://192.168.250.3:2375|Mjolnir
"""

from __future__ import annotations

import fnmatch
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import docker
from docker.errors import DockerException, NotFound

from .logproc import LEVELS, RANK, ContainerLog, format_entry

log = logging.getLogger("docker-log-bridge")


@dataclass
class Host:
    name: str
    url: str


def parse_hosts(spec: str) -> list[Host]:
    hosts: list[Host] = []
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        url, _, label = item.partition("|")
        url = url.strip()
        if not label.strip():
            label = "local" if url.startswith("unix://") else re.sub(r"^\w+://", "", url).split(":")[0]
        hosts.append(Host(name=label.strip(), url=url))
    names = [h.name for h in hosts]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate host labels in DOCKER_HOSTS: {', '.join(sorted(dupes))}")
    return hosts


def split_list(value) -> list[str]:
    """Accept 'a, b' strings or real lists (from JSON bodies)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def name_matches(host: str, name: str, patterns: list[str]) -> bool:
    """Glob match against `name` or `host/name` (case-insensitive)."""
    full = f"{host}/{name}".lower()
    name = name.lower()
    return any(fnmatch.fnmatchcase(name, p.lower()) or fnmatch.fnmatchcase(full, p.lower()) for p in patterns)


def _parse_docker_time(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-"):
        return None
    stamp = value.rstrip("Z")
    if "." in stamp:
        base, frac = stamp.split(".", 1)
        stamp = f"{base}.{re.sub(r'[^0-9].*$', '', frac)[:6]}"
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Query:
    minutes: float = 10
    hosts: list[str] | None = None
    containers: list[str] | None = None
    exclude: list[str] | None = None
    ignore: re.Pattern | None = None
    level: str = "info"
    max_lines: int = 150          # per container, after de-duplication
    max_chars: int = 40000        # whole report
    include_state: bool = True


class Collector:
    def __init__(self, hosts: list[Host], tz, docker_timeout: int = 20, workers: int = 8,
                 max_bytes_per_container: int = 20_000_000, fetch_tail: int = 10000):
        self.hosts = hosts
        self.tz = tz
        self.docker_timeout = docker_timeout
        self.workers = workers
        self.max_bytes = max_bytes_per_container
        self.fetch_tail = fetch_tail
        self._clients: dict[str, docker.DockerClient] = {}
        self._lock = threading.Lock()

    # -- Docker plumbing --------------------------------------------------
    def client(self, host: Host) -> docker.DockerClient:
        with self._lock:
            c = self._clients.get(host.name)
            if c is None:
                c = docker.DockerClient(base_url=host.url, timeout=self.docker_timeout)
                self._clients[host.name] = c
            return c

    def _drop_client(self, host: Host) -> None:
        with self._lock:
            c = self._clients.pop(host.name, None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def list_containers(self, host: Host) -> list[dict]:
        """Inspect every container on a host; raises on connection errors."""
        try:
            containers = self.client(host).containers.list(all=True)
        except Exception:
            self._drop_client(host)  # reconnect fresh next time
            raise
        out = []
        for c in containers:
            attrs = c.attrs or {}
            state = attrs.get("State", {}) or {}
            health = (state.get("Health") or {})
            out.append({
                "host": host.name,
                "id": c.id,
                "name": c.name,
                "image": (attrs.get("Config") or {}).get("Image", ""),
                "status": state.get("Status", c.status),
                "running": bool(state.get("Running")),
                "restarting": bool(state.get("Restarting")),
                "exit_code": state.get("ExitCode"),
                "oom_killed": bool(state.get("OOMKilled")),
                "error": state.get("Error") or "",
                "health": health.get("Status"),
                "health_log": _clean_health_output((health.get("Log") or [{}])[-1].get("Output")),
                "restart_count": attrs.get("RestartCount", 0),
                "started_at": _parse_docker_time(state.get("StartedAt")),
                "finished_at": _parse_docker_time(state.get("FinishedAt")),
            })
        return out

    def _read_logs(self, host: Host, info: dict, since: datetime, q: Query) -> ContainerLog:
        clog = ContainerLog(host=host.name, name=info["name"], ignore=q.ignore)
        # `tail` matters a lot: with `since` alone the daemon decodes the whole
        # log file from the start to find the window (seconds per large log,
        # minutes across a server). With `tail` it reads backwards from the
        # end and then applies `since`, so only the window is ever touched.
        stream = self.client(host).api.logs(info["id"], stream=True, follow=False, timestamps=True,
                                            stdout=True, stderr=True, since=int(since.timestamp()),
                                            tail=self.fetch_tail)
        buf = b""
        seen = 0
        received = 0
        try:
            for chunk in stream:
                seen += len(chunk)
                buf += chunk
                *lines, buf = buf.split(b"\n")
                received += len(lines)
                for line in lines:
                    clog.add(line.decode("utf-8", "replace"))
                if seen > self.max_bytes:
                    clog.add(f"{datetime.now(timezone.utc).isoformat()} [docker-log-bridge] "
                             f"WARN stopped reading after {self.max_bytes // 1_000_000} MB of output")
                    buf = b""
                    break
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        if buf:
            received += 1
            clog.add(buf.decode("utf-8", "replace"))
        if received >= self.fetch_tail:
            clog.capped_at = self.fetch_tail  # the window had more; we got the newest
        return clog

    # -- Reports ----------------------------------------------------------
    def selected_hosts(self, q: Query) -> list[Host]:
        if not q.hosts:
            return list(self.hosts)
        wanted = {h.lower() for h in q.hosts}
        return [h for h in self.hosts if h.name.lower() in wanted]

    def host_status(self) -> list[dict]:
        def probe(host: Host) -> dict:
            started = time.monotonic()
            try:
                containers = self.list_containers(host)
                return {"name": host.name, "url": host.url, "ok": True,
                        "containers": len(containers),
                        "running": sum(1 for c in containers if c["running"]),
                        "latency_ms": round((time.monotonic() - started) * 1000)}
            except Exception as exc:  # noqa: BLE001
                return {"name": host.name, "url": host.url, "ok": False, "error": _short_error(exc)}
        with ThreadPoolExecutor(max_workers=max(1, len(self.hosts))) as pool:
            return list(pool.map(probe, self.hosts))

    def containers(self, q: Query) -> dict:
        hosts = self.selected_hosts(q)
        result = {"hosts": [], "containers": []}
        with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
            futures = {h.name: pool.submit(self.list_containers, h) for h in hosts}
        for h in hosts:
            try:
                items = futures[h.name].result()
                result["hosts"].append({"name": h.name, "ok": True, "containers": len(items)})
                for c in items:
                    if self._wanted(c, q):
                        result["containers"].append(_jsonable(c))
            except Exception as exc:  # noqa: BLE001
                result["hosts"].append({"name": h.name, "ok": False, "error": _short_error(exc)})
        return result

    def _wanted(self, c: dict, q: Query) -> bool:
        if q.containers and not name_matches(c["host"], c["name"], q.containers):
            return False
        if q.exclude and name_matches(c["host"], c["name"], q.exclude):
            return False
        return True

    def report(self, q: Query) -> dict:
        started = time.monotonic()
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=q.minutes)
        hosts = self.selected_hosts(q)

        # 1. Inventory every host in parallel.
        host_results: list[dict] = []
        inventory: list[tuple[Host, dict]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
            futures = {h.name: pool.submit(self.list_containers, h) for h in hosts}
        for h in hosts:
            try:
                items = futures[h.name].result()
                host_results.append({"name": h.name, "ok": True, "containers": len(items),
                                     "running": sum(1 for c in items if c["running"])})
                inventory.extend((h, c) for c in items if self._wanted(c, q))
            except Exception as exc:  # noqa: BLE001
                log.warning("host %s unreachable: %s", h.name, exc)
                host_results.append({"name": h.name, "ok": False, "error": _short_error(exc)})

        # 2. Container state problems (things that may never show up in logs).
        problems = _state_problems([c for _, c in inventory], since, now) if q.include_state else []

        # 3. Logs: running containers, plus any that stopped inside the window.
        targets = [(h, c) for h, c in inventory
                   if c["running"] or c["restarting"] or (c["finished_at"] and c["finished_at"] >= since)]
        logs: list[ContainerLog] = []
        log_errors: list[str] = []

        def read(pair):
            h, c = pair
            try:
                return self._read_logs(h, c, since, q)
            except NotFound:
                return None  # removed between list and read
            except (DockerException, OSError, ValueError) as exc:
                log_errors.append(f"{h.name}/{c['name']}: {_short_error(exc)}")
                return None

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for clog in pool.map(read, targets):
                if clog is not None:
                    logs.append(clog)
        logs.sort(key=lambda l: (l.host.lower(), l.name.lower()))

        # 4. Totals.
        totals = {lvl: 0 for lvl in LEVELS}
        raw = 0
        for clog in logs:
            raw += clog.raw_lines
            for lvl, n in clog.counts().items():
                totals[lvl] += n

        text, shown, truncated, noisy = self._render(logs, problems, host_results, log_errors,
                                                     since, now, q, len(inventory))
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log.info("report: %g min, %d containers on %d host(s), %d lines -> %d chars in %d ms",
                 q.minutes, len(targets), len(hosts), raw, len(text), elapsed_ms)
        return {
            "generated_at": now.astimezone(self.tz).isoformat(timespec="seconds"),
            "since": since.astimezone(self.tz).isoformat(timespec="seconds"),
            "window_minutes": q.minutes,
            "level": q.level,
            "hosts": host_results,
            "hosts_down": [h["name"] for h in host_results if not h["ok"]],
            "problems": problems,
            "read_errors": log_errors,
            "stats": {
                "containers_scanned": len(targets),
                "containers_with_output": sum(1 for l in logs if l.raw_lines),
                "raw_lines": raw,
                "unique_lines": sum(len(l.entries) for l in logs),
                "lines_shown": shown,
                "errors": totals["error"],
                "warnings": totals["warning"],
                "truncated": truncated,
                "elapsed_ms": elapsed_ms,
            },
            "noisiest": noisy,
            # True when there's anything worth an LLM's time at the requested level.
            "has_findings": bool(problems or log_errors or any(not h["ok"] for h in host_results)
                                 or any(RANK[e.level] >= max(RANK[q.level], RANK["warning"])
                                        for l in logs for e in l.entries)),
            "text": text,
        }

    def _render(self, logs, problems, host_results, log_errors, since, now, q, n_containers):
        fmt = "%Y-%m-%d %H:%M"
        header = [
            f"Docker logs from {since.astimezone(self.tz).strftime(fmt)} to "
            f"{now.astimezone(self.tz).strftime('%H:%M %Z')} ({q.minutes:g} min), "
            f"{len(host_results)} host(s), {n_containers} container(s).",
            f"Showing level >= {q.level}. Line format: HH:MM:SS [D/I/W/E] (xN = repeated N times) message.",
        ]
        down = [h for h in host_results if not h["ok"]]
        if down:
            header.append("")
            header.append("## Unreachable Docker hosts")
            header.extend(f"- {h['name']}: {h['error']}" for h in down)
        if problems:
            header.append("")
            header.append("## Container state problems")
            header.extend(f"- {p}" for p in problems)
        if log_errors:
            header.append("")
            header.append("## Could not read logs")
            header.extend(f"- {e}" for e in log_errors)

        noisy = sorted(((l.raw_lines, f"{l.host}/{l.name}") for l in logs if l.raw_lines), reverse=True)[:5]
        noisy_out = [{"container": n, "lines": c} for c, n in noisy]

        # Try progressively tighter per-container caps until the report fits.
        cap = q.max_lines
        while True:
            body, shown, hidden_any = self._render_logs(logs, q.level, cap)
            text = "\n".join(header + [""] + body).strip() + "\n"
            if len(text) <= q.max_chars or cap <= 5:
                break
            cap = max(5, cap // 2)
        truncated = hidden_any or any(l.capped_at for l in logs)
        if len(text) > q.max_chars:
            text = text[: q.max_chars - 60].rsplit("\n", 1)[0] + "\n... [report truncated to fit max_chars]\n"
            truncated = True
        return text, shown, truncated, noisy_out

    def _render_logs(self, logs, level, cap):
        body = ["## Logs"]
        shown = 0
        hidden_any = False
        quiet = []
        for clog in logs:
            entries, hidden = clog.select(level, cap)
            if not entries:
                if clog.raw_lines:
                    quiet.append(f"{clog.host}/{clog.name}")
                continue
            counts = clog.counts()
            bits = [f"{clog.raw_lines} lines"]
            if counts["error"]:
                bits.append(f"{counts['error']} error")
            if counts["warning"]:
                bits.append(f"{counts['warning']} warn")
            if clog.ignored:
                bits.append(f"{clog.ignored} ignored")
            if clog.capped_at:
                bits.append(f"only the newest {clog.capped_at} were read")
            body.append("")
            body.append(f"### {clog.host}/{clog.name} ({', '.join(bits)})")
            for e in entries:
                body.extend(format_entry(e, self.tz))
            shown += len(entries)
            if hidden or clog.dropped_unique:
                hidden_any = True
                body.append(f"... {hidden + clog.dropped_unique} less severe unique line(s) omitted")
        if shown == 0:
            body.append("(no log lines at this level in the window)")
        if quiet and level != "debug":
            body.append("")
            body.append(f"Other containers with output below '{level}': {', '.join(quiet)}")
        return body, shown, hidden_any


def _state_problems(containers: list[dict], since: datetime, now: datetime) -> list[str]:
    out = []
    for c in containers:
        who = f"{c['host']}/{c['name']}"
        if c["restarting"]:
            out.append(f"{who}: restart loop (restart count {c['restart_count']}, last exit code {c['exit_code']})")
        elif c["health"] == "unhealthy":
            detail = f" - last check: {c['health_log']}" if c["health_log"] else ""
            out.append(f"{who}: UNHEALTHY{detail}")
        elif not c["running"] and c["finished_at"] and c["finished_at"] >= since:
            why = "OOM-killed" if c["oom_killed"] else f"exit code {c['exit_code']}"
            err = f" ({c['error']})" if c["error"] else ""
            out.append(f"{who}: stopped during window, {why}{err}{_restarts(c)}")
        elif c["running"] and c["started_at"] and c["started_at"] >= since:
            out.append(f"{who}: started during window, "
                       f"{int((now - c['started_at']).total_seconds() // 60)} min ago{_restarts(c)}")
        if c["running"] and c["health"] == "starting" and c["started_at"] and \
                (now - c["started_at"]) > timedelta(minutes=10):
            out.append(f"{who}: health check still 'starting' after "
                       f"{int((now - c['started_at']).total_seconds() // 60)} min")
    return out


_PROGRESS_RE = re.compile(r"^[\s\d.:%kKMG-]*$|^\s*% Total|^\s*Dload")


def _clean_health_output(output: str | None) -> str:
    """Last health-check output, minus curl/wget progress meters and blank lines."""
    lines = [re.sub(r"\s*% Total.*$", "", ln).strip() for ln in re.split(r"[\r\n]+", output or "")]
    lines = [ln for ln in lines if ln and not _PROGRESS_RE.match(ln)]
    return " | ".join(lines)[:300]


def _restarts(c: dict) -> str:
    return f" (restart count {c['restart_count']})" if c["restart_count"] else ""


def _short_error(exc: Exception) -> str:
    msg = str(exc) or exc.__class__.__name__
    # requests/urllib3 errors are long and repetitive; the tail is the useful bit.
    msg = re.sub(r"\s+", " ", msg)
    m = re.search(r"(Connection refused|timed out|Name or service not known|No route to host|"
                  r"No such file or directory|Permission denied)", msg)
    if m:
        return m.group(1)
    return msg[:200]


def _jsonable(c: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in c.items()}
