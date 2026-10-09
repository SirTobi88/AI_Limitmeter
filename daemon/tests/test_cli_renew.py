"""cli_renew: an expired token is renewed by running the Claude CLI once
(shared by the macOS and Windows daemons)."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from daemon import cli_renew

# What a directly startable claude is called here: Windows runs only an .exe
# (find_cli treats anything else as an npm shim to see through).
CLAUDE = "claude.exe" if sys.platform == "win32" else "claude"


class _FakeProc:
    pid = 4242

    def __init__(self, rc=0, err=b"", hang=False):
        self.returncode = None if hang else rc
        self._err = err
        self._hang = hang
        self._done = asyncio.Event()

    async def communicate(self):
        if self._hang:
            await self._done.wait()
        return b"", self._err

    def kill(self):
        self.returncode = -9
        self._done.set()

    async def wait(self):
        if self._hang:
            await self._done.wait()
        return self.returncode


class _Cli:
    """A fake claude executable plus a recorder for the processes it starts."""

    def __init__(self, monkeypatch, tmp_path, **proc_kw):
        self.exe = tmp_path / CLAUDE
        self.exe.write_bytes(b"")
        self.calls = []      # (args, kwargs)
        self.procs = []
        self.logs = []

        async def exec_(*args, **kwargs):
            self.calls.append((args, kwargs))
            proc = _FakeProc(**proc_kw)
            self.procs.append(proc)
            return proc

        monkeypatch.setattr(cli_renew.asyncio, "create_subprocess_exec", exec_)
        self.killed = []
        monkeypatch.setattr(cli_renew, "_kill_tree",
                            lambda p: (self.killed.append(p), p.kill()))

    def renew(self, enabled=True, **kw):
        return cli_renew.renew(enabled=enabled, claude_cli=str(self.exe),
                               log=self.logs.append, **kw)


def test_runs_a_minimal_quiet_cli_call(monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, tmp_path)
    assert asyncio.run(cli.renew()) is True
    (args, kwargs), = cli.calls
    assert args[0] == str(cli.exe)
    a = list(args[1:])
    assert a[:2] == ["-p", "Reply with OK."]
    assert a[a.index("--model") + 1] == "haiku"
    assert a[a.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in a and "--no-session-persistence" in a
    assert json.loads(a[a.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--bare" not in a                     # --bare skips OAuth
    assert kwargs["env"]["CLAWDMETER_SKIP_HOOK"] == "1"


def test_default_dir_never_sets_claude_config_dir(monkeypatch, tmp_path):
    """Setting CLAUDE_CONFIG_DIR (even to ~/.claude) switches Claude Code to
    another macOS Keychain entry -- the renewal would refresh the wrong token."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
    cli = _Cli(monkeypatch, tmp_path)
    asyncio.run(cli.renew())
    assert "CLAUDE_CONFIG_DIR" not in cli.calls[0][1]["env"]


def test_other_dir_is_passed_as_claude_config_dir(monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, tmp_path)
    work = tmp_path / "work"
    asyncio.run(cli.renew(config_dir=work))
    assert cli.calls[0][1]["env"]["CLAUDE_CONFIG_DIR"] == str(work)


def test_cli_dir_and_homebrew_are_on_path_for_npm_shebangs(monkeypatch, tmp_path):
    """npm's claude is `#!/usr/bin/env node`; launchd's PATH lacks node."""
    monkeypatch.setattr(cli_renew.sys, "platform", "darwin")
    # os.pathsep, not ":" -- only sys.platform is faked, the separator is the
    # host's (";" when this runs on Windows).
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    cli = _Cli(monkeypatch, tmp_path)
    asyncio.run(cli.renew())
    path = cli.calls[0][1]["env"]["PATH"].split(os.pathsep)
    assert path[0] == str(tmp_path)
    assert "/opt/homebrew/bin" in path and path[-2:] == ["/usr/bin", "/bin"]


def test_cools_down_per_dir_and_can_be_switched_off(monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, tmp_path)
    assert asyncio.run(cli.renew()) is True
    assert asyncio.run(cli.renew()) is False                 # within the cooldown
    assert asyncio.run(cli.renew(config_dir=tmp_path / "work")) is True  # other dir
    assert len(cli.calls) == 2

    monkeypatch.setattr(cli_renew, "_last_attempt", {})
    assert asyncio.run(cli.renew(enabled=False)) is False
    assert len(cli.calls) == 2
    assert any("cli_refresh = off" in m for m in cli.logs)


def test_reports_a_failing_cli(monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, tmp_path, rc=1, err=b"warming up\nOAuth token revoked\n")
    assert asyncio.run(cli.renew()) is False
    assert any("exited 1: OAuth token revoked" in m and "claude login" in m
               for m in cli.logs)


def test_hung_cli_is_ended_on_timeout(monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, tmp_path, hang=True)
    monkeypatch.setattr(cli_renew, "TIMEOUT_S", 0.05)
    assert asyncio.run(cli.renew()) is False
    assert cli.killed == cli.procs


def test_quit_does_not_wait_for_a_hung_cli(monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, tmp_path, hang=True)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(cli.renew(stop_event=stop))
        await asyncio.sleep(0.02)
        stop.set()
        return await asyncio.wait_for(task, timeout=1)   # not TIMEOUT_S

    assert asyncio.run(scenario()) is False
    assert cli.killed == cli.procs


def test_cancelled_renewal_leaves_no_cli_behind(monkeypatch, tmp_path):
    cli = _Cli(monkeypatch, tmp_path, hang=True)

    async def scenario():
        task = asyncio.create_task(cli.renew())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert cli.killed == cli.procs


def test_find_cli(monkeypatch, tmp_path):
    exe = tmp_path / CLAUDE
    exe.write_bytes(b"")
    assert cli_renew.find_cli(f'"{exe}"') == [str(exe)]
    # a wrong configured path is not guessed around
    assert cli_renew.find_cli(str(tmp_path / "missing")) is None
    monkeypatch.setattr(cli_renew.shutil, "which", lambda name: str(exe))
    assert cli_renew.find_cli(None) == [str(exe)]


def test_find_cli_falls_back_beyond_launchds_path(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_renew.sys, "platform", "darwin")
    monkeypatch.setattr(cli_renew.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli_renew.Path, "home", classmethod(lambda cls: tmp_path))
    assert cli_renew.find_cli(None) is None
    native = tmp_path / ".local" / "bin" / "claude"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"")
    assert cli_renew.find_cli(None) == [str(native)]


def test_npm_shim_runs_cli_js_with_node_not_cmd_exe(monkeypatch, tmp_path):
    """claude.cmd would run through cmd.exe and mangle the JSON --settings."""
    npm = tmp_path / "npm"
    script = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("")
    (npm / "claude.cmd").write_text("")
    (npm / "node.exe").write_bytes(b"")
    monkeypatch.setattr(cli_renew.sys, "platform", "win32")
    monkeypatch.setattr(cli_renew.shutil, "which", lambda name: str(npm / "claude.cmd"))
    assert cli_renew.find_cli(None) == [str(npm / "node.exe"), str(script)]
    script.unlink()                                 # a shim we can't see through
    assert cli_renew.find_cli(None) is None
