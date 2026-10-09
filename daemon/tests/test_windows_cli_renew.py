"""Windows daemon: an expired token is renewed by running the Claude CLI once."""
import asyncio
import json
from pathlib import Path

import pytest

from daemon import clawd_activity as ca
import daemon.claude_usage_daemon_windows as mod


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_last_cli_renew", 0.0)
    monkeypatch.setattr(mod, "CONFIG_FILE", tmp_path / "config")


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
        await self._done.wait() if self._hang else None
        return self.returncode


def _fake_exec(calls, rc=0, hang=False):
    async def exec_(*args, **kwargs):
        calls.append((args, kwargs))
        proc = _FakeProc(rc, hang=hang)
        calls.append(proc)
        return proc
    return exec_


def _with_cli(monkeypatch, tmp_path, **kw):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    (tmp_path / "config").write_text(f"claude_cli = {exe}\n")
    calls = []
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec(calls, **kw))
    return exe, calls


def test_renew_runs_a_minimal_quiet_cli_call(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    (tmp_path / "config").write_text(f"claude_cli = {exe}\n")
    calls = []
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec(calls))

    assert asyncio.run(mod.renew_token_via_cli()) is True
    (args, kwargs), _proc = calls
    assert args[0] == str(exe)
    a = list(args[1:])
    assert a[:2] == ["-p", "Reply with OK."]
    assert a[a.index("--model") + 1] == "haiku"
    assert a[a.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in a and "--no-session-persistence" in a
    assert json.loads(a[a.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--bare" not in a                     # --bare skips OAuth
    assert kwargs["env"]["CLAWDMETER_SKIP_HOOK"] == "1"


def test_renew_cools_down_and_can_be_switched_off(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    (tmp_path / "config").write_text(f"claude_cli = {exe}\n")
    calls = []
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec(calls))

    assert asyncio.run(mod.renew_token_via_cli()) is True
    assert asyncio.run(mod.renew_token_via_cli()) is False   # within the cooldown
    assert len(calls) == 2                                   # one (args, proc) pair

    monkeypatch.setattr(mod, "_last_cli_renew", 0.0)
    (tmp_path / "config").write_text(f"claude_cli = {exe}\ncli_refresh = off\n")
    assert asyncio.run(mod.renew_token_via_cli()) is False
    assert len(calls) == 2


def test_renew_reports_a_failing_cli(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    (tmp_path / "config").write_text(f"claude_cli = {exe}\n")
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec([], rc=1))
    assert asyncio.run(mod.renew_token_via_cli()) is False


def test_find_claude_cli(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    cfg.write_text(f'claude_cli = "{exe}"\n')
    assert mod.find_claude_cli() == [str(exe)]
    cfg.write_text(f"claude_cli = {tmp_path / 'missing.exe'}\n")
    assert mod.find_claude_cli() is None           # a wrong path is not guessed around
    cfg.write_text("")
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(exe))
    assert mod.find_claude_cli() == [str(exe)]


def test_npm_shim_runs_cli_js_with_node_not_cmd_exe(monkeypatch, tmp_path):
    """claude.cmd would run through cmd.exe and mangle the JSON --settings."""
    npm = tmp_path / "npm"
    script = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    script.parent.mkdir(parents=True)
    script.write_text("")
    (npm / "claude.cmd").write_text("")
    (npm / "node.exe").write_bytes(b"")
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod.shutil, "which", lambda name: str(npm / "claude.cmd"))
    assert mod.find_claude_cli() == [str(npm / "node.exe"), str(script)]
    script.unlink()                                 # a shim we can't see through
    assert mod.find_claude_cli() is None


def test_hung_cli_is_ended_on_timeout(monkeypatch, tmp_path):
    _exe, calls = _with_cli(monkeypatch, tmp_path, hang=True)
    killed = []
    monkeypatch.setattr(mod, "CLI_RENEW_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mod, "_kill_tree", lambda p: (killed.append(p), p.kill()))
    assert asyncio.run(mod.renew_token_via_cli()) is False
    assert killed == [calls[1]]


def test_quit_does_not_wait_for_a_hung_cli(monkeypatch, tmp_path):
    _exe, calls = _with_cli(monkeypatch, tmp_path, hang=True)
    killed = []
    monkeypatch.setattr(mod, "_kill_tree", lambda p: (killed.append(p), p.kill()))

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(mod.renew_token_via_cli(stop))
        await asyncio.sleep(0.02)
        stop.set()
        return await asyncio.wait_for(task, timeout=1)   # not CLI_RENEW_TIMEOUT_S

    assert asyncio.run(scenario()) is False
    assert killed == [calls[1]]


def test_cancelled_renewal_leaves_no_cli_behind(monkeypatch, tmp_path):
    _exe, calls = _with_cli(monkeypatch, tmp_path, hang=True)
    killed = []
    monkeypatch.setattr(mod, "_kill_tree", lambda p: (killed.append(p), p.kill()))

    async def scenario():
        task = asyncio.create_task(mod.renew_token_via_cli())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert killed == [calls[1]]


class _FakeClient:
    is_connected = True
    writes: list = []

    def __init__(self, *_a, **_kw):
        pass

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def start_notify(self, *_a):
        pass

    async def write_gatt_char(self, _uuid, data, response=False):
        _FakeClient.writes.append(json.loads(data))


def _session(monkeypatch, poll, renew):
    _FakeClient.writes = []
    monkeypatch.setattr(mod, "BleakClient", _FakeClient)
    monkeypatch.setattr(mod, "POLL_INTERVAL", 3600)
    monkeypatch.setattr(mod, "read_token", lambda: "tok")
    monkeypatch.setattr(mod, "poll_api", poll)
    monkeypatch.setattr(mod, "renew_token_via_cli", renew)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(mod.connect_and_run("AA:BB", stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(scenario())
    return _FakeClient.writes


def test_401_renewed_by_cli_sends_numbers_not_no_data(monkeypatch):
    polls = []

    async def poll(_token):
        polls.append(1)
        if len(polls) == 1:
            raise mod.AuthError(401)
        return {"s": 12, "w": 3, "ok": True}

    async def renew(_stop=None):
        return True

    writes = _session(monkeypatch, poll, renew)
    assert len(polls) == 2
    assert writes and writes[0]["s"] == 12
    assert {"ok": False} not in writes


def test_401_without_renewal_still_signals_no_data(monkeypatch):
    async def poll(_token):
        raise mod.AuthError(401)

    async def renew(_stop=None):
        return False

    writes = _session(monkeypatch, poll, renew)
    assert writes == [{"ok": False}]


def test_403_does_not_run_the_cli(monkeypatch):
    renewals = []

    async def poll(_token):
        raise mod.AuthError(403)

    async def renew(_stop=None):
        renewals.append(1)
        return True

    writes = _session(monkeypatch, poll, renew)
    assert renewals == []
    assert writes == [{"ok": False}]


def test_hook_is_silent_inside_the_renewal_run(monkeypatch):
    monkeypatch.delenv("CLAWDMETER_SKIP_HOOK", raising=False)
    assert ca._hook_disabled() is False
    monkeypatch.setenv("CLAWDMETER_SKIP_HOOK", "1")
    assert ca._hook_disabled() is True
