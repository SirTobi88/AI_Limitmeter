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
    def __init__(self, rc=0, err=b""):
        self.returncode = rc
        self._err = err

    async def communicate(self):
        return b"", self._err

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


def _fake_exec(calls, rc=0):
    async def exec_(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProc(rc)
    return exec_


def test_renew_runs_a_minimal_quiet_cli_call(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    (tmp_path / "config").write_text(f"claude_cli = {exe}\n")
    calls = []
    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", _fake_exec(calls))

    assert asyncio.run(mod.renew_token_via_cli()) is True
    (args, kwargs), = calls
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
    assert len(calls) == 1

    monkeypatch.setattr(mod, "_last_cli_renew", 0.0)
    (tmp_path / "config").write_text(f"claude_cli = {exe}\ncli_refresh = off\n")
    assert asyncio.run(mod.renew_token_via_cli()) is False
    assert len(calls) == 1


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
    assert mod.find_claude_cli() == str(exe)
    cfg.write_text(f"claude_cli = {tmp_path / 'missing.exe'}\n")
    assert mod.find_claude_cli() is None           # a wrong path is not guessed around
    cfg.write_text("")
    monkeypatch.setattr(mod.shutil, "which", lambda name: r"C:\bin\claude.exe")
    assert mod.find_claude_cli() == r"C:\bin\claude.exe"


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

    async def renew():
        return True

    writes = _session(monkeypatch, poll, renew)
    assert len(polls) == 2
    assert writes and writes[0]["s"] == 12
    assert {"ok": False} not in writes


def test_401_without_renewal_still_signals_no_data(monkeypatch):
    async def poll(_token):
        raise mod.AuthError(401)

    async def renew():
        return False

    writes = _session(monkeypatch, poll, renew)
    assert writes == [{"ok": False}]


def test_hook_is_silent_inside_the_renewal_run(monkeypatch):
    monkeypatch.delenv("CLAWDMETER_SKIP_HOOK", raising=False)
    assert ca._hook_disabled() is False
    monkeypatch.setenv("CLAWDMETER_SKIP_HOOK", "1")
    assert ca._hook_disabled() is True
