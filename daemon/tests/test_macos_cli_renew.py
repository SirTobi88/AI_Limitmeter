"""macOS daemon: a 401 lets the Claude CLI renew the token, then polls again.

The renewal itself (cli_renew.py) is covered by test_cli_renew.py."""
import asyncio
from pathlib import Path

import daemon.claude_usage_daemon as mod

WORK = Path("/tmp/claude-work")


def _setup(monkeypatch, dirs, polls, renewed=True):
    """`polls`: what poll_api does per call, in order (a dict or an exception)."""
    monkeypatch.setattr(mod, "read_config_dirs", lambda: dirs)
    monkeypatch.setattr(mod, "read_token_for", lambda d: "tok")
    calls = {"poll": 0, "renew": []}

    async def poll(_token):
        r = polls[calls["poll"]]
        calls["poll"] += 1
        if isinstance(r, Exception):
            raise r
        return r

    async def renew(**kw):
        calls["renew"].append(kw)
        return renewed

    monkeypatch.setattr(mod, "poll_api", poll)
    monkeypatch.setattr(mod.cli_renew, "renew", renew)
    return calls


def test_401_renewed_by_cli_returns_numbers(monkeypatch):
    calls = _setup(monkeypatch, [mod.DEFAULT_CONFIG_DIR],
                   [mod.TokenExpired(401), {"s": 12, "ok": True}])
    payload, dead = asyncio.run(mod.poll_active(mod.PlanSelector()))
    assert payload == {"s": 12, "ok": True} and dead is False
    (kw,) = calls["renew"]
    assert kw["config_dir"] is None            # default dir: Keychain entry untouched


def test_other_config_dir_is_renewed_as_itself(monkeypatch):
    calls = _setup(monkeypatch, [WORK], [mod.TokenExpired(401), {"s": 3, "ok": True}])
    asyncio.run(mod.poll_active(mod.PlanSelector()))
    assert calls["renew"][0]["config_dir"] == WORK


def test_401_without_renewal_is_dead(monkeypatch):
    calls = _setup(monkeypatch, [mod.DEFAULT_CONFIG_DIR], [mod.TokenExpired(401)],
                   renewed=False)
    assert asyncio.run(mod.poll_active(mod.PlanSelector())) == (None, True)
    assert calls["poll"] == 1


def test_403_does_not_run_the_cli(monkeypatch):
    calls = _setup(monkeypatch, [mod.DEFAULT_CONFIG_DIR], [mod.TokenExpired(403)])
    assert asyncio.run(mod.poll_active(mod.PlanSelector())) == (None, True)
    assert calls["renew"] == []


def test_no_cli_run_while_stopping(monkeypatch):
    calls = _setup(monkeypatch, [mod.DEFAULT_CONFIG_DIR], [mod.TokenExpired(401)])

    async def go():
        stop = asyncio.Event()
        stop.set()
        return await mod.poll_active(mod.PlanSelector(), stop_event=stop)

    asyncio.run(go())
    assert calls["renew"] == []


def test_config_reaches_the_shared_renewal(monkeypatch, tmp_path):
    seen = {}

    async def renew(**kw):
        seen.update(kw)
        return False

    monkeypatch.setattr(mod.cli_renew, "renew", renew)
    mod.CONFIG_FILE.write_text("cli_refresh = off\nclaude_cli = ~/bin/claude\n")
    asyncio.run(mod.renew_token_via_cli(mod.DEFAULT_CONFIG_DIR))
    assert seen["enabled"] is False and seen["claude_cli"] == "~/bin/claude"
