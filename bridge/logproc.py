"""Pure log-processing helpers: level detection, de-duplication, trimming.

Nothing in here talks to Docker, so it is all unit-testable (see tests/).
The goal is to turn a raw firehose of container output into something an LLM
can read cheaply: ANSI codes stripped, repeated lines collapsed into one with a
count, and - when there's too much - the least interesting lines dropped first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# Ordered least -> most severe. Index doubles as a numeric rank.
LEVELS = ("debug", "info", "warning", "error")
RANK = {name: i for i, name in enumerate(LEVELS)}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

# Explicit level markers that apps write themselves. When one is present we
# trust it over keyword guessing, so `level=info msg="0 failed"` stays info.
#   level=error  lvl=warn  "level":"ERROR"  severity: Warning
_KV_LEVEL_RE = re.compile(
    r"""(?:\blevel|\blvl|\bseverity|\bloglevel)["']?\s*[:=]\s*["']?([a-z]+)""",
    re.IGNORECASE,
)
#   [debug]  [Error]  [Warn]  - any case when bracketed on its own
_BRACKET_LEVEL_RE = re.compile(
    r"\[(trace|debug|dbg|info|inf|notice|warning|warn|wrn|error|err|critical|crit|fatal|panic|severe)\]",
    re.IGNORECASE,
)
#   ERROR:  WARN  |ERROR|  [Server thread/WARN]  ::INFO::  <E>
_TAG_LEVEL_RE = re.compile(
    r"""(?:^|[\s\[<(|/:])(TRACE|DEBUG|DBG|INFO|INF|NOTICE|WARNING|WARN|WRN|ERROR|ERR|"""
    r"""CRITICAL|CRIT|FATAL|FTL|PANIC|ALERT|EMERG|SEVERE)(?=$|[\s\]>)|:,-])""",
)
_GLOG_RE = re.compile(r"^([IWEF])\d{4} \d\d:\d\d:\d\d")

_LEVEL_WORDS = {
    "trace": "debug", "debug": "debug", "dbg": "debug", "verbose": "debug",
    "info": "info", "inf": "info", "information": "info", "notice": "info",
    "warning": "warning", "warn": "warning", "wrn": "warning",
    "error": "error", "err": "error", "eror": "error", "critical": "error",
    "crit": "error", "fatal": "error", "ftl": "error", "panic": "error",
    "alert": "error", "emerg": "error", "emergency": "error", "severe": "error",
}
_GLOG_LEVELS = {"I": "info", "W": "warning", "E": "error", "F": "error"}

# Fallback keyword heuristics for apps that don't tag their lines.
_ERROR_WORDS_RE = re.compile(
    r"\b(error|errors|exception|traceback|fatal|panic|panicked|critical|segfault|"
    r"segmentation fault|out of memory|oom|killed|crash(?:ed)?|unhandled|"
    r"failed|failure|refused|denied|unauthorized|forbidden|cannot|can't|unable to|"
    r"timed out|timeout|corrupt(?:ed)?|unreachable|no space left)\b",
    re.IGNORECASE,
)
_WARN_WORDS_RE = re.compile(
    r"\b(warn|warning|deprecated|retry(?:ing)?|slow|degraded|not found|missing|"
    r"invalid|skipp(?:ed|ing)|disconnected|reconnect(?:ing)?)\b",
    re.IGNORECASE,
)

# Lines that belong to the previous line: stack frames, indented detail,
# "Caused by", "During handling of the above exception" etc.
_CONTINUATION_RE = re.compile(
    r"^(\s+\S|at\s|\.\.\.\s*\d+ more|Caused by|During handling|The above exception|"
    r"Traceback|goroutine \d+|\w+(?:\.\w+)*(?:Error|Exception)\b)"
)

# Normalise volatile bits so near-identical lines collapse together.
_NORMALISE_RES = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "#"),
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def detect_level(line: str) -> str:
    """Best-effort severity for one log line: debug / info / warning / error."""
    m = _KV_LEVEL_RE.search(line)
    if m and m.group(1).lower() in _LEVEL_WORDS:
        return _LEVEL_WORDS[m.group(1).lower()]
    m = _GLOG_RE.match(line)
    if m:
        return _GLOG_LEVELS[m.group(1)]
    m = _BRACKET_LEVEL_RE.search(line[:120]) or _TAG_LEVEL_RE.search(line[:120])
    if m:
        return _LEVEL_WORDS.get(m.group(1).lower(), "info")
    if _ERROR_WORDS_RE.search(line):
        return "error"
    if _WARN_WORDS_RE.search(line):
        return "warning"
    return "info"


def is_continuation(line: str) -> bool:
    return bool(_CONTINUATION_RE.match(line))


def normalise(line: str) -> str:
    for rx, repl in _NORMALISE_RES:
        line = rx.sub(repl, line)
    return line.strip()


def split_docker_timestamp(raw: str) -> tuple[datetime | None, str]:
    """Docker prefixes each line with an RFC3339Nano timestamp when asked to."""
    head, sep, rest = raw.partition(" ")
    if not sep or len(head) < 20 or head[4] != "-" or head[10] != "T":
        return None, raw
    # Python wants at most 6 fractional digits and no trailing Z.
    stamp = head.rstrip("Z")
    if "." in stamp:
        base, frac = stamp.split(".", 1)
        frac = re.sub(r"[^0-9].*$", "", frac)[:6]
        stamp = f"{base}.{frac}" if frac else base
    try:
        return datetime.fromisoformat(stamp + "+00:00"), rest
    except ValueError:
        return None, raw


@dataclass
class Entry:
    ts: datetime | None
    level: str
    text: str
    count: int = 1
    last_ts: datetime | None = None
    extra: list[str] = field(default_factory=list)  # continuation lines


@dataclass
class ContainerLog:
    """Accumulates one container's lines, collapsing repeats as it goes."""

    host: str
    name: str
    max_unique: int = 20000
    max_continuation: int = 300  # stored; format_entry shows head + tail
    ignore: re.Pattern | None = None

    raw_lines: int = 0
    ignored: int = 0
    dropped_unique: int = 0
    entries: list[Entry] = field(default_factory=list)
    _index: dict[str, Entry] = field(default_factory=dict)
    _last: Entry | None = None
    _attach: bool = False

    def add(self, raw: str) -> None:
        ts, text = split_docker_timestamp(raw.rstrip("\r\n"))
        text = strip_ansi(text).rstrip()
        if not text.strip():
            return
        self.raw_lines += 1
        if self.ignore is not None and self.ignore.search(text):
            self.ignored += 1
            self._last = None
            return

        # Stack frames etc. stick to the line that started them. Only the first
        # occurrence keeps them; a repeat's frames are swallowed with it.
        if self._last is not None and is_continuation(text):
            if self._attach and len(self._last.extra) < self.max_continuation:
                self._last.extra.append(text)
                # A traceback under an "info" line still means trouble.
                level = detect_level(text)
                if RANK[level] > RANK[self._last.level]:
                    self._last.level = level
            return

        key = normalise(text)
        entry = self._index.get(key)
        if entry is not None:
            entry.count += 1
            entry.last_ts = ts or entry.last_ts
            self._last, self._attach = entry, False
            return
        if len(self._index) >= self.max_unique:
            self.dropped_unique += 1
            self._last = None
            return
        entry = Entry(ts=ts, level=detect_level(text), text=text, last_ts=ts)
        self._index[key] = entry
        self.entries.append(entry)
        self._last, self._attach = entry, True

    def counts(self) -> dict[str, int]:
        out = {lvl: 0 for lvl in LEVELS}
        for e in self.entries:
            out[e.level] += e.count
        return out

    def select(self, min_level: str, max_lines: int) -> tuple[list[Entry], int]:
        """Entries at/above min_level, at most max_lines of them.

        When over the cap, keep the most severe first (errors, then warnings,
        then the rest), newest-first within a level, then restore time order.
        Returns (entries, number_hidden).
        """
        floor = RANK[min_level]
        pool = [e for e in self.entries if RANK[e.level] >= floor]
        if len(pool) <= max_lines:
            return pool, 0
        order = {id(e): i for i, e in enumerate(pool)}
        ranked = sorted(pool, key=lambda e: (RANK[e.level], order[id(e)]), reverse=True)
        keep = {id(e) for e in ranked[:max_lines]}
        kept = [e for e in pool if id(e) in keep]
        return kept, len(pool) - len(kept)


LEVEL_TAG = {"debug": "D", "info": "I", "warning": "W", "error": "E"}
TRACE_HEAD, TRACE_TAIL = 3, 8


def format_entry(entry: Entry, tz, with_time: bool = True) -> list[str]:
    parts = []
    if with_time and entry.ts is not None:
        parts.append(entry.ts.astimezone(tz).strftime("%H:%M:%S"))
    parts.append(f"[{LEVEL_TAG[entry.level]}]")
    if entry.count > 1:
        span = ""
        if with_time and entry.last_ts and entry.ts and entry.last_ts != entry.ts:
            span = f", last {entry.last_ts.astimezone(tz).strftime('%H:%M:%S')}"
        parts.append(f"(x{entry.count}{span})")
    parts.append(entry.text)
    lines = [" ".join(parts)]
    extra = entry.extra
    # Long stack traces: the first frames say where, the last say what.
    if len(extra) > TRACE_HEAD + TRACE_TAIL + 1:
        skipped = len(extra) - TRACE_HEAD - TRACE_TAIL
        extra = extra[:TRACE_HEAD] + [f"... {skipped} more lines ..."] + extra[-TRACE_TAIL:]
    lines.extend(f"    {x.strip()}" for x in extra)
    return lines
