"""Read OpenAI Codex's rate-limit windows from its local session logs.

Codex writes a `token_count` event into its rollout files after every turn,
and each one carries the account's current rate limits:

    {"timestamp": "...", "type": "event_msg",
     "payload": {"type": "token_count", ...,
                 "rate_limits": {"primary":   {"used_percent": 49.0, "window_minutes": 300,
                                               "resets_at": 1791300927},
                                 "secondary": {"used_percent": 91.0, "window_minutes": 10080,
                                               "resets_at": 1791656777},
                                 "plan_type": "plus", ...}}}

`primary` is the 5-hour window, `secondary` the weekly one -- the same shape
as Claude's, so the device shows them with the same gauges. No credentials and
no network: the numbers are as fresh as the last Codex turn, and a window whose
reset time has passed is reported as empty.
"""

import json
import math
import os
import time
from pathlib import Path

# How many of the newest rollout files to look into. The newest one usually
# answers; older ones only matter when a fresh session has not had a turn yet.
MAX_FILES = 8
_CHUNK = 64 * 1024

# (path, mtime, size) -> (timestamp, rate_limits) | None, so a 10 s re-read
# only parses files Codex has actually written to since.
_cache: dict[tuple[str, float, int], tuple[str, dict] | None] = {}


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def _newest_rollouts(home: Path, limit: int = MAX_FILES) -> list[Path]:
    """Newest rollout files by mtime, from sessions/YYYY/MM/DD and archived_sessions."""
    found: list[Path] = []
    sessions = home / "sessions"
    # Walk the date folders newest-first and stop once enough files are found,
    # so months of history are never listed.
    try:
        years = sorted((p for p in sessions.iterdir() if p.is_dir()), reverse=True)
    except OSError:
        years = []
    for y in years:
        for m in sorted((p for p in y.iterdir() if p.is_dir()), reverse=True):
            for d in sorted((p for p in m.iterdir() if p.is_dir()), reverse=True):
                found.extend(d.glob("rollout-*.jsonl"))
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break
    try:
        found.extend((home / "archived_sessions").glob("rollout-*.jsonl"))
    except OSError:
        pass

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(found, key=mtime, reverse=True)[:limit]


def _reverse_lines(path: Path):
    """Yield the file's lines last-first without reading it all."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        rest = b""
        while pos > 0:
            step = min(_CHUNK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + rest
            lines = buf.split(b"\n")
            rest = lines.pop(0)          # may be cut off; finish it next round
            for line in reversed(lines):
                if line.strip():
                    yield line
        if rest.strip():
            yield rest


def _last_rate_limits(path: Path) -> tuple[str, dict] | None:
    """(timestamp, rate_limits) of the file's last usable token_count event."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime, st.st_size)
    if key in _cache:
        return _cache[key]
    result = None
    try:
        for raw in _reverse_lines(path):
            if b'"rate_limits"' not in raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            rl = (ev.get("payload") or {}).get("rate_limits") if isinstance(ev, dict) else None
            if isinstance(rl, dict) and isinstance(rl.get("primary"), dict):
                result = (str(ev.get("timestamp") or ""), rl)
                break
    except OSError:
        return None
    _cache[key] = result
    return result


def _window(win, now: float) -> tuple[int, int]:
    """(used %, minutes to reset) for one window; a passed reset reads 0 / -1."""
    if not isinstance(win, dict):
        return 0, -1
    try:
        used = float(win.get("used_percent") or 0)
    except (TypeError, ValueError):
        used = 0.0
    resets_at = win.get("resets_at")
    if isinstance(resets_at, (int, float)) and resets_at > 0:
        if resets_at <= now:
            return 0, -1                 # the window rolled over since Codex last ran
        mins = math.ceil((resets_at - now) / 60)
    else:
        mins = -1
    return max(0, min(100, int(round(used)))), mins


def read_codex_limits(home: Path | None = None, now: float | None = None) -> dict | None:
    """Compact payload for the device ("cx"), or None when Codex left no numbers.

    {"s": 5h %, "sr": mins to 5h reset, "w": weekly %, "wr": mins to weekly reset,
     "pl": plan type}
    """
    home = home or codex_home()
    now = time.time() if now is None else now
    best: tuple[str, dict] | None = None
    for path in _newest_rollouts(home):
        hit = _last_rate_limits(path)
        if hit and (best is None or hit[0] > best[0]):
            best = hit
    if best is None:
        return None
    rl = best[1]
    s, sr = _window(rl.get("primary"), now)
    w, wr = _window(rl.get("secondary"), now)
    out = {"s": s, "sr": sr, "w": w, "wr": wr}
    plan = rl.get("plan_type")
    if isinstance(plan, str) and plan:
        out["pl"] = plan[:11]
    return out


if __name__ == "__main__":
    print(json.dumps(read_codex_limits()))
