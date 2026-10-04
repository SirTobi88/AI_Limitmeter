"""What Claude Code is doing right now, for the device's buddy.

Two halves, both stdlib-only:

* ``python3 clawd_activity.py --hook`` is what Claude Code runs on each hook
  event. It reads the event JSON from stdin and drops the essentials into one
  small file per session under ACTIVITY_DIR. It must be fast and must never
  fail a Claude Code session, so every error is swallowed and it always exits 0.

* ``current_anim()`` is what the daemon calls every tick. It reads those files
  and folds all sessions into one splash animation name for the payload's
  ``a`` field (the names live in firmware/src/splash_animations.h).

``--install`` / ``--uninstall`` add or remove the hook entries in
~/.claude/settings.json. Only entries running this file with --hook are
touched; anything
else in that file is left as it was.

The hooks replace guessing: the Session Browser infers state from .jsonl
mtimes, which can't tell a long-running tool from a permission prompt. Hooks
say so directly.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

def _default_activity_dir() -> Path:
    # Windows keeps the daemon's config and log under %LOCALAPPDATA%\Clawdmeter;
    # the session files go next to them.
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Clawdmeter" / "activity"
    return Path.home() / ".config" / "claude-usage-monitor" / "activity"


ACTIVITY_DIR = _default_activity_dir()
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_MARK = "clawd_activity.py"

# Events the hook is registered for. Tool events get a "*" matcher; the rest
# take none.
HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse",
               "Notification", "Stop", "SessionEnd")
_TOOL_EVENTS = ("PreToolUse", "PostToolUse")

# Tools whose job is writing files get the "write" animation; every other tool
# call is "work coding".
_WRITE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}

# How long a state holds without a newer event before it decays. Working
# states need a ceiling because an interrupted turn (Esc) fires no Stop hook.
WORK_STALE_S = 300
ALLOW_STALE_S = 1800
DONE_S = 180          # "Your turn" this long after Stop, then idle
SLEEP_S = 900         # no event in this long -> sleeping
FORGET_S = 6 * 3600   # session files older than this are deleted

# Highest first: one session needing you beats five that are busy.
_PRIORITY = ("limit", "allow", "write", "work coding", "work think",
             "done", "idle breathe", "expression sleep")


# ---- hook side ----------------------------------------------------------

def _state_for(event: dict) -> str | None:
    """Animation a single hook event puts its session into, or None to keep
    whatever the session was showing."""
    name = event.get("hook_event_name") or ""
    if name == "UserPromptSubmit":
        return "work think"
    if name == "PreToolUse":
        return "write" if event.get("tool_name") in _WRITE_TOOLS else "work coding"
    if name == "PostToolUse":
        return "work think"
    if name == "Stop":
        return "done"
    if name == "Notification":
        kind = (event.get("notification_type") or "").lower()
        msg = (event.get("message") or "").lower()
        if kind == "permission_prompt" or "permission" in msg:
            return "allow"
        if kind == "idle_prompt" or "waiting for your input" in msg:
            return "done"
        return None
    return None


def handle_hook(raw: str, now: float | None = None,
                activity_dir: Path = ACTIVITY_DIR) -> None:
    event = json.loads(raw)
    sid = "".join(c for c in str(event.get("session_id") or "")
                  if c.isalnum() or c in "-_")
    if not sid:
        return
    path = activity_dir / f"{sid}.json"
    if event.get("hook_event_name") == "SessionEnd":
        path.unlink(missing_ok=True)
        return
    state = _state_for(event)
    if state is None:
        return
    activity_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"state": state,
                               "ts": time.time() if now is None else now}))
    os.replace(tmp, path)


# ---- daemon side --------------------------------------------------------

def _decay(state: str, age: float) -> str:
    if age >= SLEEP_S:
        return "expression sleep"
    if state == "allow":
        return state if age < ALLOW_STALE_S else "idle breathe"
    if state == "done":
        return state if age < DONE_S else "idle breathe"
    if state in ("write", "work coding", "work think"):
        return state if age < WORK_STALE_S else "idle breathe"
    return "idle breathe"


def current_anim(limit_hit: bool = False, now: float | None = None,
                 activity_dir: Path = ACTIVITY_DIR) -> str:
    """One animation name for all live sessions together."""
    if limit_hit:
        return "limit"
    now = time.time() if now is None else now
    best = "expression sleep"
    try:
        files = list(activity_dir.glob("*.json"))
    except OSError:
        files = []
    for f in files:
        try:
            d = json.loads(f.read_text())
            age = now - float(d["ts"])
            state = str(d["state"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if age > FORGET_S:
            f.unlink(missing_ok=True)
            continue
        s = _decay(state, age)
        if s in _PRIORITY and _PRIORITY.index(s) < _PRIORITY.index(best):
            best = s
    return best


# ---- settings.json install ---------------------------------------------

def _hook_command() -> str:
    script = Path(__file__).resolve()
    if sys.platform == "win32":
        # Claude Code starts the hook as a process of its own on every tool
        # call; python.exe would flash a console window each time. The base
        # interpreter's pythonw.exe has none and still reads the event from
        # stdin (the hook is stdlib-only, so it needs no venv — and the venv's
        # pythonw is a stub that relaunches the console python). Forward
        # slashes read the same whether the line goes to cmd.exe or Git Bash.
        pythonw = Path(sys.base_exec_prefix) / "pythonw.exe"
        exe = pythonw if pythonw.exists() else Path(sys.executable)
        return f'"{exe.as_posix()}" "{script.as_posix()}" --hook'
    return f'"{sys.executable}" "{script}" --hook'


def _is_ours(h) -> bool:
    cmd = str(h.get("command", "")) if isinstance(h, dict) else ""
    return HOOK_MARK in cmd and "--hook" in cmd


def _strip_ours(groups: list) -> list:
    out = []
    for g in groups:
        if not isinstance(g, dict):
            out.append(g)
            continue
        hooks = [h for h in g.get("hooks") or [] if not _is_ours(h)]
        if hooks:
            out.append({**g, "hooks": hooks})
    return out


_installed_cache: dict = {}


def is_installed(settings_path: Path = CLAUDE_SETTINGS) -> bool:
    """True when our hook is registered in Claude Code's settings.json.
    Cached on the file's mtime, so the daemon may ask every tick."""
    try:
        mtime = settings_path.stat().st_mtime
    except OSError:
        return False
    key = str(settings_path)
    hit = _installed_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        hooks = json.loads(settings_path.read_text(encoding="utf-8")).get("hooks") or {}
        found = any(_is_ours(h)
                    for groups in hooks.values() if isinstance(groups, list)
                    for g in groups if isinstance(g, dict)
                    for h in g.get("hooks") or [])
    except (OSError, ValueError, AttributeError):
        found = False
    _installed_cache[key] = (mtime, found)
    return found


def set_installed(on: bool, settings_path: Path = CLAUDE_SETTINGS) -> None:
    # UTF-8 explicitly: Windows would read and write the locale code page and
    # mangle any non-ASCII text the user keeps in settings.json.
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for ev in HOOK_EVENTS:
        groups = _strip_ours(hooks.get(ev) or [])
        if on:
            group = {"hooks": [{"type": "command", "command": _hook_command(),
                                "timeout": 5}]}
            if ev in _TOOL_EVENTS:
                group = {"matcher": "*", **group}
            groups.append(group)
        if groups:
            hooks[ev] = groups
        else:
            hooks.pop(ev, None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, settings_path)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--hook":
        try:
            handle_hook(sys.stdin.read())
        except Exception:
            pass
        sys.exit(0)
    if arg in ("--install", "--uninstall"):
        set_installed(arg == "--install")
        print(("Installed" if arg == "--install" else "Removed")
              + f" Clawdmeter hooks in {CLAUDE_SETTINGS}")
        sys.exit(0)
    if arg == "--status":
        print(current_anim())
        sys.exit(0)
    print("usage: clawd_activity.py --install | --uninstall | --status | --hook")
    sys.exit(2)
