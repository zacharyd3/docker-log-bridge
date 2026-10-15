from datetime import timezone

import pytest

from bridge.collector import name_matches, parse_hosts
from bridge.logproc import ContainerLog, detect_level, format_entry, split_docker_timestamp


@pytest.mark.parametrize("line,level", [
    ('time="2026-10-15T10:00:00Z" level=info msg="0 failed, 3 ok"', "info"),
    ('{"level":"ERROR","msg":"db down"}', "error"),
    ("2026-10-15 10:00:00,123 WARNING [sonarr] Indexer slow", "warning"),
    ("[ERROR] could not bind port", "error"),
    ("E1015 10:00:00.000000 1 reflector.go:1] boom", "error"),
    ("Connection refused while contacting 10.0.0.2", "error"),
    ("Retrying in 5 seconds", "warning"),
    ("GET /api/health 200 3ms", "info"),
    ("DEBUG: cache hit", "debug"),
    ("panic: runtime error: index out of range", "error"),
])
def test_detect_level(line, level):
    assert detect_level(line) == level


def test_docker_timestamp():
    ts, rest = split_docker_timestamp("2026-10-15T21:24:01.123456789Z hello world")
    assert rest == "hello world"
    assert ts.tzinfo is not None and ts.microsecond == 123456
    assert split_docker_timestamp("no timestamp here") == (None, "no timestamp here")


def _feed(clog, lines):
    for i, line in enumerate(lines):
        clog.add(f"2026-10-15T21:{i // 60:02d}:{i % 60:02d}.000000000Z {line}")


def test_dedupe_and_counts():
    clog = ContainerLog("Thor", "radarr")
    _feed(clog, [f"Failed to connect to indexer {n} after {n * 3}ms" for n in range(40)] + ["all good"])
    assert len(clog.entries) == 2
    assert clog.entries[0].count == 40 and clog.entries[0].level == "error"
    assert clog.counts()["error"] == 40
    out = format_entry(clog.entries[0], timezone.utc)[0]
    assert "(x40, last 21:00:39)" in out and "[E]" in out


def test_traceback_attached_and_escalated():
    clog = ContainerLog("Mjolnir", "app")
    _feed(clog, [
        "Handling request /sync",
        "Traceback (most recent call last):",
        '  File "app.py", line 3, in <module>',
        "ValueError: bad",
        "next request",
    ])
    first = clog.entries[0]
    assert first.level == "error"
    assert first.extra[0].startswith("Traceback")
    assert len(clog.entries) == 2


def test_ansi_and_ignore():
    import re
    clog = ContainerLog("h", "c", ignore=re.compile("healthcheck"))
    _feed(clog, ["\x1b[31mERROR\x1b[0m red alert", "GET /healthcheck 200"])
    assert clog.entries[0].text == "ERROR red alert"
    assert clog.ignored == 1 and len(clog.entries) == 1


def test_select_keeps_most_severe():
    clog = ContainerLog("h", "c")
    _feed(clog, [f"info line {chr(65 + i)}" for i in range(10)] + ["ERROR boom", "WARN hmm"])
    kept, hidden = clog.select("debug", 3)
    assert hidden == 9
    assert [e.level for e in kept] == ["info", "error", "warning"]  # time order restored
    kept, hidden = clog.select("warning", 50)
    assert [e.text for e in kept] == ["ERROR boom", "WARN hmm"] and hidden == 0


def test_parse_hosts_dozzle_format():
    hosts = parse_hosts("unix:///var/run/docker.sock|Unraid, tcp://192.168.250.3:2375|Mjolnir,tcp://10.0.0.9:2375")
    assert [(h.name, h.url) for h in hosts] == [
        ("Unraid", "unix:///var/run/docker.sock"),
        ("Mjolnir", "tcp://192.168.250.3:2375"),
        ("10.0.0.9", "tcp://10.0.0.9:2375"),
    ]
    with pytest.raises(ValueError):
        parse_hosts("tcp://a:1|X,tcp://b:1|X")


def test_name_matching():
    assert name_matches("Thor", "plex", ["plex"])
    assert name_matches("Thor", "Plex", ["thor/*"])
    assert name_matches("Thor", "binhex-sonarr", ["*sonarr"])
    assert not name_matches("Thor", "plex", ["mjolnir/*"])


def test_repeated_traceback_frames_are_swallowed():
    clog = ContainerLog("h", "c")
    block = ["ERROR - Database is locked", "Traceback (most recent call last):",
             '  File "/app/x.py", line 3, in sync', "sqlite3.OperationalError: database is locked"]
    _feed(clog, block * 5 + ["done"])
    assert [e.text for e in clog.entries] == ["ERROR - Database is locked", "done"]
    assert clog.entries[0].count == 5 and len(clog.entries[0].extra) == 3


# Real lines from an unraid box that were mis-levelled by keyword guessing.
@pytest.mark.parametrize("line,level", [
    ("[22:30:42] [Server thread/INFO] [de.sa.ep.Epitaphs/]: No players found, skipping save", "info"),
    ("[22:26:20] [Server thread/WARN] [minecraft/MinecraftServer]: Can't keep up! Is the server overloaded?", "warning"),
    ("2026-10-15T22:30:00.500Z [debug][Plex Scan]: GUIDs are cached. Skipping metadata request.", "debug"),
    ("[Error] DownloadedMovieImportService: Import failed, path does not exist", "error"),
    ("[Warn] Torznab: Indexer rss sync didn't cover the period", "warning"),
    ("2026-10-15 18:26:55,680::INFO::main.py::IP for plex.example.org updated successfully", "info"),
    ("GET /error/page 404", "error"),  # no tag -> keyword fallback still works
])
def test_detect_level_app_tags(line, level):
    assert detect_level(line) == level


def test_long_traceback_keeps_head_and_tail():
    clog = ContainerLog("h", "c")
    frames = [f'  File "/app/m{i}.py", line {i}, in f{i}' for i in range(40)]
    _feed(clog, ["ERROR boom", "Traceback (most recent call last):", *frames, "ValueError: the real cause"])
    out = format_entry(clog.entries[0], timezone.utc)
    assert out[1].strip().startswith("Traceback")
    assert out[-1].strip() == "ValueError: the real cause"
    assert any("more lines" in line for line in out)
    assert len(out) == 1 + 3 + 1 + 8


def test_health_output_strips_progress_meter():
    from bridge.collector import _clean_health_output
    raw = ("Health check exceeded timeout (30s):   % Total    % Received % Xferd  Average Speed   Time\n"
           "                                 Dload  Upload   Total   Spent    Left  Speed\n"
           "\r  0     0    0     0    0     0      0      0 --:--:-- --:--:-- --:--:--     0\r  0     0    0")
    assert _clean_health_output(raw) == "Health check exceeded timeout (30s):"
    assert _clean_health_output("PONG\n") == "PONG"
    assert _clean_health_output(None) == ""
