"""Shared fixtures for the daemon tests."""
import pytest


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
