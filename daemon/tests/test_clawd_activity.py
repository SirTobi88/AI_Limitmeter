"""Claude Code hook events -> the buddy animation the daemon sends as "a"."""
import json
from pathlib import Path

import pytest

from daemon import clawd_activity as ca
import daemon.claude_usage_daemon as mod


def hook(d: Path, sid: str, event: str, now: float, **extra) -> None:
    ca.handle_hook(json.dumps({"session_id": sid, "hook_event_name": event, **extra}),
                   now=now, activity_dir=d)


@pytest.mark.parametrize("event,extra,anim", [
    ("UserPromptSubmit", {}, "work think"),
    ("PreToolUse", {"tool_name": "Bash"}, "work coding"),
    ("PreToolUse", {"tool_name": "Edit"}, "write"),
    ("PostToolUse", {"tool_name": "Edit"}, "work think"),
    ("Stop", {}, "done"),
    ("Notification", {"notification_type": "permission_prompt"}, "allow"),
    ("Notification", {"message": "Claude needs your permission to use Bash"}, "allow"),
    ("Notification", {"notification_type": "idle_prompt"}, "done"),
])
def test_event_maps_to_anim(tmp_path, event, extra, anim):
    hook(tmp_path, "s1", event, 1000.0, **extra)
    assert ca.current_anim(now=1001.0, activity_dir=tmp_path) == anim


def test_unknown_notification_keeps_previous_state(tmp_path):
    hook(tmp_path, "s1", "PreToolUse", 1000.0, tool_name="Bash")
    hook(tmp_path, "s1", "Notification", 1001.0, message="something else")
    assert ca.current_anim(now=1002.0, activity_dir=tmp_path) == "work coding"


def test_no_sessions_is_sleep(tmp_path):
    assert ca.current_anim(now=1000.0, activity_dir=tmp_path) == "expression sleep"


def test_session_end_removes_session(tmp_path):
    hook(tmp_path, "s1", "Stop", 1000.0)
    hook(tmp_path, "s1", "SessionEnd", 1001.0)
    assert list(tmp_path.glob("*.json")) == []


def test_decay_done_then_idle_then_sleep(tmp_path):
    hook(tmp_path, "s1", "Stop", 1000.0)
    assert ca.current_anim(now=1000.0 + ca.DONE_S - 1, activity_dir=tmp_path) == "done"
    assert ca.current_anim(now=1000.0 + ca.DONE_S + 1, activity_dir=tmp_path) == "idle breathe"
    assert ca.current_anim(now=1000.0 + ca.SLEEP_S + 1, activity_dir=tmp_path) == "expression sleep"


def test_interrupted_turn_decays(tmp_path):
    # Esc fires no Stop hook; a working state must not last forever.
    hook(tmp_path, "s1", "UserPromptSubmit", 1000.0)
    assert ca.current_anim(now=1000.0 + ca.WORK_STALE_S + 1, activity_dir=tmp_path) == "idle breathe"


def test_priority_across_sessions(tmp_path):
    hook(tmp_path, "busy", "PreToolUse", 1000.0, tool_name="Bash")
    hook(tmp_path, "done", "Stop", 1000.0)
    assert ca.current_anim(now=1001.0, activity_dir=tmp_path) == "work coding"
    hook(tmp_path, "asks", "Notification", 1000.0, notification_type="permission_prompt")
    assert ca.current_anim(now=1001.0, activity_dir=tmp_path) == "allow"
    assert ca.current_anim(limit_hit=True, now=1001.0, activity_dir=tmp_path) == "limit"


def test_old_session_files_are_forgotten(tmp_path):
    hook(tmp_path, "s1", "Stop", 1000.0)
    ca.current_anim(now=1000.0 + ca.FORGET_S + 1, activity_dir=tmp_path)
    assert list(tmp_path.glob("*.json")) == []


def test_hook_ignores_missing_session_id(tmp_path):
    ca.handle_hook(json.dumps({"hook_event_name": "Stop"}), now=1.0, activity_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_install_preserves_foreign_hooks_and_uninstall_restores(tmp_path):
    settings = tmp_path / "settings.json"
    original = {
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
    }
    settings.write_text(json.dumps(original))

    ca.set_installed(True, settings)
    ca.set_installed(True, settings)   # idempotent
    d = json.loads(settings.read_text())
    assert d["model"] == "opus"
    for ev in ca.HOOK_EVENTS:
        ours = [h for g in d["hooks"][ev] for h in g["hooks"] if ca._is_ours(h)]
        assert len(ours) == 1, ev
    assert d["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert {"type": "command", "command": "say done"} in d["hooks"]["Stop"][0]["hooks"]

    ca.set_installed(False, settings)
    assert json.loads(settings.read_text()) == original


def test_payload_fields_follow_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mod.clawd_activity, "current_anim",
                        lambda limit_hit=False: "limit" if limit_hit else "done")

    # Default "auto": off until the hooks are installed, then on.
    cfg.write_text("clock = 24\n")
    monkeypatch.setattr(mod.clawd_activity, "is_installed", lambda: False)
    p = {"s": 10, "w": 5}
    assert mod.add_activity_fields(p) is None
    assert "a" not in p and "sm" not in p and "ua" not in p
    monkeypatch.setattr(mod.clawd_activity, "is_installed", lambda: True)
    p = {"s": 10, "w": 5}
    assert mod.add_activity_fields(p) == "done"
    assert p == {"s": 10, "w": 5, "a": "done", "sm": 2, "ua": True}

    cfg.write_text("activity = off\n")
    p = {"s": 10, "w": 5}
    assert mod.add_activity_fields(p) is None and "a" not in p

    cfg.write_text("activity = on\nscreen_mode = clawd\ncorner_buddy = off\n")
    p = {"s": 10, "w": 5}
    assert mod.add_activity_fields(p) == "done"
    assert p == {"s": 10, "w": 5, "a": "done", "sm": 1, "ua": False}

    p = {"s": 100, "w": 5}
    assert mod.add_activity_fields(p) == "limit"


def test_mode_change_is_sent_between_polls(tmp_path, monkeypatch):
    """screen_mode edited in the config reaches the device within a tick, not
    at the next poll; the usage numbers are the last reading's."""
    import asyncio

    cfg = tmp_path / "config"
    cfg.write_text("activity = on\nscreen_mode = auto\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mod, "POLL_INTERVAL", 3600)
    monkeypatch.setattr(mod, "ACTIVITY_TICK", 0.01)
    monkeypatch.setattr(mod.clawd_activity, "current_anim", lambda limit_hit=False: "done")

    polls = []

    async def fake_poll():
        polls.append(1)
        return {"s": 10, "w": 5, "ok": True}, False

    monkeypatch.setattr(mod, "poll_active", fake_poll)

    class FakeClient:
        is_connected = True

        def __init__(self, _target):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def start_notify(self, *_a):
            pass

        async def write_gatt_char(self, _uuid, data, response=False):
            writes.append(json.loads(data))

    writes = []
    monkeypatch.setattr(mod, "BleakClient", FakeClient)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(mod.connect_and_run("AA:BB", stop))
        await asyncio.sleep(0.05)
        assert [w["sm"] for w in writes] == [2]
        cfg.write_text("activity = on\nscreen_mode = usage\ncorner_buddy = off\n")
        await asyncio.sleep(0.05)
        cfg.write_text("activity = off\n")
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(scenario())
    assert len(polls) == 1
    assert writes[1]["sm"] == 0 and writes[1]["ua"] is False and writes[1]["s"] == 10
    assert "a" not in writes[2] and "sm" not in writes[2]   # opted out -> device decides
    assert len(writes) == 3


def test_no_data_beat_stops_activity_resends(tmp_path, monkeypatch):
    """After a dead-token poll the device shows "No data"; a Claude Code state
    change must not resend the last numbers over it."""
    import asyncio

    cfg = tmp_path / "config"
    cfg.write_text("activity = on\n")
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mod, "POLL_INTERVAL", 0.03)
    monkeypatch.setattr(mod, "ACTIVITY_TICK", 0.01)
    anims = iter(["done"] + ["allow"] * 1000)
    monkeypatch.setattr(mod.clawd_activity, "current_anim", lambda limit_hit=False: next(anims))
    results = iter([({"s": 10, "w": 5, "ok": True}, False)] + [(None, True)] * 1000)

    async def fake_poll():
        return next(results)

    monkeypatch.setattr(mod, "poll_active", fake_poll)
    writes = []

    class FakeClient:
        is_connected = True
        def __init__(self, _t): pass
        async def connect(self): pass
        async def disconnect(self): pass
        async def start_notify(self, *_a): pass
        async def write_gatt_char(self, _u, data, response=False):
            writes.append(json.loads(data))

    monkeypatch.setattr(mod, "BleakClient", FakeClient)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(mod.connect_and_run("AA:BB", stop))
        await asyncio.sleep(0.15)
        stop.set()
        await task

    asyncio.run(scenario())
    assert writes[0]["a"] == "done"
    first_dead = writes.index({"ok": False})
    assert all(w == {"ok": False} for w in writes[first_dead:]), writes


def test_is_installed_follows_settings(tmp_path):
    settings = tmp_path / "settings.json"
    assert ca.is_installed(settings) is False            # no file
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "say done"}]}]}}))
    assert ca.is_installed(settings) is False            # foreign hooks only
    ca.set_installed(True, settings)
    assert ca.is_installed(settings) is True
    ca.set_installed(False, settings)
    assert ca.is_installed(settings) is False


def test_clock_defaults_to_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CONFIG_FILE", tmp_path / "config")
    assert mod.read_clock_setting() == "auto"
    (tmp_path / "config").write_text("clock = off\n")
    assert mod.read_clock_setting() == "off"
