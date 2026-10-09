"""Renew an expired Claude Code token by running the official CLI once.

Shared by the macOS and Windows daemons. The daemons never refresh the OAuth
token themselves: that would race Claude Code's own rotation (two refreshers,
one refresh token) and feed the OAuth endpoint's rate limit. But the token only
goes stale when no Claude Code has run for a while -- and Claude Code renews it
whenever it starts and finds it expired. So on a 401 the daemon runs the CLI
once, with the smallest request it accepts, and reads the token again: the
owner does the refresh, with its own locking (and on macOS its own Keychain
entry), and the device keeps its numbers.
"""
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

COOLDOWN_S = 15 * 60   # one attempt per quarter hour (per config dir), not per poll
TIMEOUT_S = 120
# Haiku, one word back, no tools, no MCP servers, nothing saved to the session
# history, no hooks -- the run must not show up on the device as Claude working
# (CLAWDMETER_SKIP_HOOK below covers a CLI that would ignore disableAllHooks).
# Not --bare: it skips OAuth, which is the whole point of the run.
ARGS = [
    "-p", "Reply with OK.",
    "--model", "haiku",
    "--tools", "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--settings", '{"disableAllHooks": true}',
]

# time.monotonic() of the last attempt, per config dir ("" = the default one)
_last_attempt: dict[str, float] = {}


def _fallbacks() -> list[Path]:
    """Where the installers put `claude` when it is not on PATH. A launchd
    agent's PATH has neither ~/.local/bin nor ~/.claude/local."""
    home = Path.home()
    if sys.platform == "win32":
        return [home / ".local" / "bin" / "claude.exe"]
    return [
        home / ".local" / "bin" / "claude",            # native installer
        home / ".claude" / "local" / "claude",         # older local install
        Path("/opt/homebrew/bin/claude"),              # Homebrew (Apple silicon)
        Path("/usr/local/bin/claude"),                 # Homebrew (Intel), npm -g
    ]


def _npm_shim_command(shim: Path) -> list[str] | None:
    """npm on Windows installs `claude` as a batch shim (claude.cmd). A batch
    file runs through cmd.exe, whose quoting is not the one subprocess writes
    for -- the JSON --settings value and the empty --tools argument would
    arrive mangled. So start the package's cli.js with node directly, as the
    shim itself does."""
    script = shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = shim.parent / "node.exe"
    node_cmd = str(node) if node.is_file() else shutil.which("node")
    if script.is_file() and node_cmd:
        return [node_cmd, str(script)]
    return None


def find_cli(configured: str | None) -> list[str] | None:
    """The command that starts the Claude CLI: `configured` (the config's
    `claude_cli`) if given, else PATH, else the installers' usual places.
    None when nothing usable exists -- a wrong configured path is not guessed
    around."""
    if configured:
        path = Path(configured.strip('"')).expanduser()
    else:
        found = shutil.which("claude")
        path = Path(found) if found else next(
            (p for p in _fallbacks() if p.is_file()), _fallbacks()[0])
    if not path.is_file():
        return None
    if sys.platform == "win32" and path.suffix.lower() != ".exe":
        # claude.cmd / claude.ps1, or npm's extensionless sh script beside them
        return _npm_shim_command(path)
    return [str(path)]


def _env(cmd: list[str], config_dir: Path | None) -> dict[str, str]:
    env = {**os.environ, "CLAWDMETER_SKIP_HOOK": "1"}
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    else:
        # The default dir. Setting CLAUDE_CONFIG_DIR, even to ~/.claude, makes
        # Claude Code use a different macOS Keychain entry.
        env.pop("CLAUDE_CONFIG_DIR", None)
    if sys.platform != "win32":
        # An npm install is a `#!/usr/bin/env node` script; under launchd's
        # short PATH `env` would not find node.
        extra = [str(Path(cmd[0]).parent), "/opt/homebrew/bin", "/usr/local/bin"]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def _kill_tree(proc) -> None:
    """End the CLI and everything it started. proc.kill() alone ends the CLI
    only; its children would outlive it."""
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # started in its own session
            return
        except OSError:
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _communicate_or_stop(proc, stop_event: asyncio.Event | None) -> bytes | None:
    """stderr of the finished CLI, or None when it ran past TIMEOUT_S or the
    daemon is stopping -- Quit must not wait for a hung CLI."""
    comm = asyncio.ensure_future(proc.communicate())
    waits = [comm]
    if stop_event is not None:
        waits.append(asyncio.ensure_future(stop_event.wait()))
    try:
        await asyncio.wait(waits, timeout=TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
    finally:
        pending = [t for t in waits if not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    if comm.done() and not comm.cancelled():
        return comm.result()[1] or b""
    return None


async def renew(*, enabled: bool, claude_cli: str | None, log: Callable[[str], None],
                stop_event: asyncio.Event | None = None,
                config_dir: Path | None = None) -> bool:
    """Run the Claude CLI once so it renews the expired token of `config_dir`
    (None = the default ~/.claude). True when the CLI ran and exited cleanly --
    the caller then reads the token again.

    `enabled` is the config's `cli_refresh`, `claude_cli` its path override."""
    if not enabled:
        log("CLI renewal is off (cli_refresh = off)")
        return False
    key = str(config_dir) if config_dir is not None else ""
    now = time.monotonic()
    last = _last_attempt.get(key)
    if last is not None and now - last < COOLDOWN_S:
        left = (COOLDOWN_S - (now - last)) / 60
        log(f"Last CLI renewal did not help; next attempt in {left:.0f} min")
        return False
    _last_attempt[key] = now
    cmd = find_cli(claude_cli)
    if not cmd:
        log("Token expired and no usable claude CLI found to renew it "
            "(set claude_cli = <path to claude> in the config)")
        return False
    where = f" for {config_dir}" if config_dir is not None else ""
    log(f"Token expired; letting the Claude CLI renew it{where} (one short `claude -p`)")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, *ARGS,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            env=_env(cmd, config_dir),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=sys.platform != "win32",
        )
    except (OSError, NotImplementedError) as e:
        log(f"Could not start the Claude CLI: {e}")
        return False
    try:
        err = await _communicate_or_stop(proc, stop_event)
    finally:
        # Timeout, Quit, or the caller cancelled: never leave the CLI behind.
        if proc.returncode is None:
            _kill_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
    if err is None:
        if stop_event is not None and stop_event.is_set():
            log("Stopping; ended the Claude CLI renewal")
        else:
            log(f"Claude CLI did not finish within {TIMEOUT_S}s; ended it")
        return False
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip().splitlines()
        log(f"Claude CLI exited {proc.returncode}: {msg[-1][:200] if msg else ''} "
            "-- run `claude login` if the sign-in itself has expired")
        return False
    log("Claude CLI ran; reading the token again")
    return True
