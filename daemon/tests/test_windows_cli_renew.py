"""Windows daemon: a 401 lets the Claude CLI renew the token, then polls again.

The renewal itself (cli_renew.py) is covered by test_cli_renew.py."""
import asyncio
import json

import pytest

from daemon import clawd_activity as ca
import daemon.claude_usage_daemon_windows as mod


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


def test_config_reaches_the_shared_renewal(monkeypatch, tmp_path):
    seen = {}

    async def renew(**kw):
        seen.update(kw)
        return False

    monkeypatch.setattr(mod.cli_renew, "renew", renew)
    mod.CONFIG_FILE.write_text("cli_refresh = OFF\nclaude_cli = C:\\x\\claude.exe\n")
    asyncio.run(mod.renew_token_via_cli())
    assert seen["enabled"] is False
    assert seen["claude_cli"] == "C:\\x\\claude.exe"
    assert "config_dir" not in seen                 # Windows: the default dir only


def test_hook_is_silent_inside_the_renewal_run(monkeypatch):
    monkeypatch.delenv("CLAWDMETER_SKIP_HOOK", raising=False)
    assert ca._hook_disabled() is False
    monkeypatch.setenv("CLAWDMETER_SKIP_HOOK", "1")
    assert ca._hook_disabled() is True
