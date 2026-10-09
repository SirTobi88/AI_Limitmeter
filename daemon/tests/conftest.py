"""Shared fixtures for the daemon tests."""
import asyncio

import pytest


@pytest.fixture(autouse=True)
def _no_real_cli(monkeypatch):
    """No test may start a real process through asyncio. A mocked 401 sends the
    Windows daemon to renew the token with `claude -p`, which on a dev machine
    would be a real request on the developer's account. Tests of the renewal
    replace this with their own fake."""
    async def blocked(*_a, **_kw):
        raise OSError("tests must not start processes")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked)


@pytest.fixture(autouse=True)
def _no_daemon_file_log(monkeypatch):
    """Keep test traffic out of the real %LOCALAPPDATA%\\Clawdmeter\\daemon.log.

    On Windows the daemon module opens that file at import, and the reconnect
    and poll tests' fake addresses, mocked 401s and "zombie link" breaks then
    land in the log of the daemon that is actually running — they read like a
    real outage when you go looking for one.
    """
    try:
        import daemon.claude_usage_daemon_windows as win
    except ImportError:
        return
    monkeypatch.setattr(win, "_FILE_LOGGER", None)
