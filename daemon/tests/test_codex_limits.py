"""Codex's rate limits from its session logs -> the payload's "cx"."""
import asyncio
import json
import os

import pytest

from daemon import codex_limits as cl
import daemon.claude_usage_daemon_windows as win
from daemon.tests.test_windows_activity import _run_session

NOW = 1_800_000_000
_REAL_READ = cl.read_codex_limits   # captured before conftest stubs it per test


@pytest.fixture
def real_reader(monkeypatch):
    """conftest stubs the reader out; these tests want the real one."""
    monkeypatch.setattr(cl, "read_codex_limits", _REAL_READ)
    cl._cache.clear()
    return _REAL_READ


def _event(ts, p_used=40.0, p_reset=NOW + 3600, s_used=70.0, s_reset=NOW + 86400,
           plan="plus", primary=True):
    rl = {"limit_id": "codex",
          "primary": {"used_percent": p_used, "window_minutes": 300, "resets_at": p_reset}
          if primary else None,
          "secondary": {"used_percent": s_used, "window_minutes": 10080, "resets_at": s_reset},
          "plan_type": plan}
    return json.dumps({"timestamp": ts, "type": "event_msg",
                       "payload": {"type": "token_count", "info": {}, "rate_limits": rl}})


def _rollout(home, day, name, lines, mtime):
    d = home / "sessions" / "2026" / "10" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{name}.jsonl"
    f.write_text("\n".join(lines) + "\n")
    os.utime(f, (mtime, mtime))
    return f


def test_last_event_of_newest_file_wins(tmp_path, real_reader):
    _rollout(tmp_path, "05", "old", [_event("2026-10-05T10:00:00Z", p_used=90)], NOW - 900)
    _rollout(tmp_path, "06", "new", [
        _event("2026-10-06T10:00:00Z", p_used=10),
        '{"type":"response_item","payload":{}}',
        _event("2026-10-06T11:00:00Z", p_used=49.4),
        '{"type":"event_msg","payload":{"type":"agent_message"}}',
    ], NOW - 60)
    assert real_reader(tmp_path, now=NOW) == {"s": 49, "sr": 60, "w": 70, "wr": 1440, "pl": "plus"}


def test_null_primary_and_broken_lines_are_skipped(tmp_path, real_reader):
    _rollout(tmp_path, "06", "a", [
        _event("2026-10-06T10:00:00Z", p_used=33),
        _event("2026-10-06T11:00:00Z", primary=False),
        '{"timestamp":"2026-10-06T12:00:00Z","payload":{"rate_limits": {broken',
    ], NOW)
    assert real_reader(tmp_path, now=NOW)["s"] == 33


def test_fresh_session_without_turns_falls_back_to_older_file(tmp_path, real_reader):
    _rollout(tmp_path, "06", "busy", [_event("2026-10-06T10:00:00Z", p_used=12)], NOW - 600)
    _rollout(tmp_path, "06", "fresh", ['{"type":"session_meta","payload":{}}'], NOW)
    assert real_reader(tmp_path, now=NOW)["s"] == 12


def test_passed_reset_reads_as_empty_window(tmp_path, real_reader):
    _rollout(tmp_path, "06", "a", [_event("2026-10-06T10:00:00Z", p_used=95,
                                          p_reset=NOW - 1, s_reset=NOW + 61)], NOW)
    got = real_reader(tmp_path, now=NOW)
    assert (got["s"], got["sr"]) == (0, -1)
    assert (got["w"], got["wr"]) == (70, 2)       # 61 s -> rounds up to 2 min


def test_archived_sessions_count(tmp_path, real_reader):
    _rollout(tmp_path, "05", "old", [_event("2026-10-05T10:00:00Z", p_used=5)], NOW - 900)
    arch = tmp_path / "archived_sessions"
    arch.mkdir()
    f = arch / "rollout-x.jsonl"
    f.write_text(_event("2026-10-06T10:00:00Z", p_used=66) + "\n")
    assert real_reader(tmp_path, now=NOW)["s"] == 66


def test_long_file_is_read_from_the_end(tmp_path, real_reader, monkeypatch):
    monkeypatch.setattr(cl, "_CHUNK", 64)     # force many chunk boundaries
    filler = ['{"type":"response_item","payload":{"text":"%s"}}' % ("x" * 50)] * 40
    _rollout(tmp_path, "06", "a", [_event("2026-10-06T09:00:00Z", p_used=1)] + filler
             + [_event("2026-10-06T10:00:00Z", p_used=77)] + filler, NOW)
    assert real_reader(tmp_path, now=NOW)["s"] == 77


def test_no_codex_at_all(tmp_path, real_reader):
    assert real_reader(tmp_path / "missing", now=NOW) is None
    (tmp_path / "sessions").mkdir()
    assert real_reader(tmp_path, now=NOW) is None


def test_codex_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert cl.codex_home() == tmp_path
    monkeypatch.delenv("CODEX_HOME")
    assert cl.codex_home().name == ".codex"


# ---- the Windows daemon's loop -------------------------------------------

def _codex_world(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("activity = off\n")
    monkeypatch.setattr(win, "CONFIG_FILE", cfg)
    monkeypatch.setattr(win, "POLL_INTERVAL", 3600)
    monkeypatch.setattr(win, "TICK", 0.01)
    monkeypatch.setattr(win, "CODEX_TICK", 0.02)
    cx = {"v": {"s": 10, "sr": 60, "w": 20, "wr": 600}}
    monkeypatch.setattr(win.codex_limits, "read_codex_limits", lambda: cx["v"])
    return cx


def test_codex_change_is_sent_between_polls(tmp_path, monkeypatch):
    """A Codex turn moves its numbers; they go out without a new Claude poll."""
    cx = _codex_world(monkeypatch, tmp_path)
    polls = []

    async def fake_poll(_token):
        polls.append(1)
        return {"s": 50, "w": 5, "ok": True}

    monkeypatch.setattr(win, "poll_api", fake_poll)

    async def during():
        await asyncio.sleep(0.08)
        cx["v"] = {"s": 11, "sr": 59, "w": 20, "wr": 599}
        await asyncio.sleep(0.08)

    writes = _run_session(monkeypatch, during)
    assert len(polls) == 1
    assert [w["cx"]["s"] for w in writes] == [10, 11]
    assert all(w["s"] == 50 and w["ok"] for w in writes)


def test_dead_claude_token_still_carries_codex(tmp_path, monkeypatch):
    """A 401 shows "No data" for Claude, but the Codex half keeps updating."""
    cx = _codex_world(monkeypatch, tmp_path)

    async def during():
        await asyncio.sleep(0.08)
        cx["v"] = None                        # Codex logs gone too
        await asyncio.sleep(0.08)

    async def dead_poll(_token):
        raise win.AuthError(401)

    monkeypatch.setattr(win, "poll_api", dead_poll)
    writes = _run_session(monkeypatch, during)
    assert writes[0] == {"ok": False, "cx": {"s": 10, "sr": 60, "w": 20, "wr": 600}}
    assert writes[-1] == {"ok": False}
