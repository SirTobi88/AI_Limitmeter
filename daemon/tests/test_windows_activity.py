"""Windows daemon: Claude Code's state (hooks) -> the payload's "a"/"sm"/"ua"/"ss"."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

from daemon import clawd_activity as ca
import daemon.claude_usage_daemon_windows as mod


def test_payload_fields_follow_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mod.clawd_activity, "current_anim",
                        lambda limit_hit=False: "limit" if limit_hit else "done")

    # Default "auto": off until the hooks are installed, then on.
    monkeypatch.setattr(mod.clawd_activity, "is_installed", lambda: False)
    p = {"s": 10, "w": 5}
    assert mod.add_activity_fields(p) is None
    assert "a" not in p and "sm" not in p and "ua" not in p
    monkeypatch.setattr(mod.clawd_activity, "is_installed", lambda: True)
    p = {"s": 10, "w": 5}
    assert mod.add_activity_fields(p) == "done"
    assert p == {"s": 10, "w": 5, "a": "done", "sm": 2, "ua": True, "ss": True}

    cfg.write_text("activity = off\n")
    p = {"s": 10, "w": 5}
    assert mod.add_activity_fields(p) is None and "a" not in p

    cfg.write_text("activity = on\nscreen_mode = clawd\ncorner_buddy = off\nstate_sounds = off\n")
    p = {"s": 10, "w": 5, "ss": True}
    assert mod.add_activity_fields(p) == "done"
    assert p == {"s": 10, "w": 5, "a": "done", "sm": 1, "ua": False}

    p = {"s": 100, "w": 5}
    assert mod.add_activity_fields(p) == "limit"


def test_volume_setting(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    assert mod.read_volume_setting() == 70
    for text, want in (("volume = 40", 40), ("volume = 150", 100),
                       ("volume = -5", 0), ("volume = 55%", 55), ("volume = loud", 70)):
        cfg.write_text(text + "\n")
        assert mod.read_volume_setting() == want, text
    cfg.write_text("volume = 30\n")
    p = {}
    mod.add_chime_field(p)
    assert p["vol"] == 30 and "c" not in p


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


def _run_session(monkeypatch, during):
    """Run connect_and_run against a fake link; `during` edits the world
    while it runs. Returns the payloads the device received."""
    _FakeClient.writes = []
    monkeypatch.setattr(mod, "BleakClient", _FakeClient)
    monkeypatch.setattr(mod, "read_token", lambda: "tok")

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(mod.connect_and_run("AA:BB", stop))
        await during()
        stop.set()
        await task

    asyncio.run(scenario())
    return _FakeClient.writes


def test_state_change_is_sent_between_polls(tmp_path, monkeypatch):
    """A new Claude Code state reaches the device within a tick, carrying the
    last reading's numbers; a poll happens only once."""
    cfg = tmp_path / "config"
    cfg.write_text("activity = on\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mod, "POLL_INTERVAL", 3600)
    monkeypatch.setattr(mod, "ACTIVITY_TICK", 0.01)
    state = {"anim": "work think"}
    monkeypatch.setattr(mod.clawd_activity, "current_anim",
                        lambda limit_hit=False: state["anim"])
    polls = []

    async def fake_poll(_token):
        polls.append(1)
        return {"s": 10, "w": 5, "ok": True}

    monkeypatch.setattr(mod, "poll_api", fake_poll)

    async def during():
        await asyncio.sleep(0.05)
        state["anim"] = "allow"           # wants you: goes out at once
        await asyncio.sleep(0.05)
        cfg.write_text("activity = on\nscreen_mode = usage\n")
        await asyncio.sleep(0.05)

    writes = _run_session(monkeypatch, during)
    assert len(polls) == 1
    assert [w["a"] for w in writes] == ["work think", "allow", "allow"]
    assert [w["sm"] for w in writes] == [2, 2, 0]
    assert all(w["s"] == 10 for w in writes)


def test_working_flips_are_held(tmp_path, monkeypatch):
    """thinking <-> tool flips inside a turn don't each cost a write."""
    cfg = tmp_path / "config"
    cfg.write_text("activity = on\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mod, "POLL_INTERVAL", 3600)
    monkeypatch.setattr(mod, "ACTIVITY_TICK", 0.01)
    state = {"anim": "work think"}
    monkeypatch.setattr(mod.clawd_activity, "current_anim",
                        lambda limit_hit=False: state["anim"])

    async def fake_poll(_token):
        return {"s": 10, "w": 5, "ok": True}

    monkeypatch.setattr(mod, "poll_api", fake_poll)

    async def during():
        await asyncio.sleep(0.03)
        state["anim"] = "work coding"
        await asyncio.sleep(0.05)

    writes = _run_session(monkeypatch, during)
    assert [w["a"] for w in writes] == ["work think"]


def test_dead_token_stops_activity_resends(tmp_path, monkeypatch):
    """After a 401 the device shows "No data"; a state change must not resend
    the last numbers over it."""
    cfg = tmp_path / "config"
    cfg.write_text("activity = on\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mod, "POLL_INTERVAL", 0.03)
    monkeypatch.setattr(mod, "ACTIVITY_TICK", 0.01)
    anims = iter(["done"] + ["allow"] * 1000)
    monkeypatch.setattr(mod.clawd_activity, "current_anim",
                        lambda limit_hit=False: next(anims))
    calls = {"n": 0}

    async def fake_poll(_token):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"s": 10, "w": 5, "ok": True}
        raise mod.AuthError(401)

    monkeypatch.setattr(mod, "poll_api", fake_poll)

    async def during():
        await asyncio.sleep(0.15)

    writes = _run_session(monkeypatch, during)
    assert writes[0]["a"] == "done"
    first_dead = writes.index({"ok": False})
    assert all(w == {"ok": False} for w in writes[first_dead:]), writes


# ---- clawd_activity on Windows -------------------------------------------

def test_activity_dir_is_outside_appdata(tmp_path, monkeypatch):
    """The Claude desktop app (MSIX) redirects AppData writes of everything it
    starts, hooks included; a daemon started outside it would never see them."""
    monkeypatch.setattr(ca.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    d = ca._default_activity_dir()
    assert d == Path.home() / ".config" / "claude-usage-monitor" / "activity"
    assert "AppData" not in d.parts


def test_hook_command_uses_windowless_python(tmp_path, monkeypatch):
    monkeypatch.setattr(ca.sys, "platform", "win32")
    monkeypatch.setattr(ca.sys, "base_exec_prefix", str(tmp_path))
    (tmp_path / "pythonw.exe").write_bytes(b"")
    cmd = ca._hook_command()
    assert cmd.startswith(f'"{(tmp_path / "pythonw.exe").as_posix()}" ')
    assert cmd.endswith("clawd_activity.py\" --hook")
    assert "\\" not in cmd              # same meaning in cmd.exe and Git Bash
    assert ca._is_ours({"command": cmd})

    (tmp_path / "pythonw.exe").unlink()  # no pythonw -> the running interpreter
    assert ca._hook_command().startswith(f'"{Path(sys.executable).as_posix()}" ')


def test_install_keeps_non_ascii_settings_intact(tmp_path):
    settings = tmp_path / "settings.json"
    original = {"statusLine": {"type": "command", "command": "echo Größe ✓"}}
    settings.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    ca.set_installed(True, settings)
    assert ca.is_installed(settings)
    ca.set_installed(False, settings)
    assert json.loads(settings.read_text(encoding="utf-8")) == original
