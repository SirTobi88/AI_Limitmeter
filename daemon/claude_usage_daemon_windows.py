#!/usr/bin/env python3
"""Claude Usage Tracker Daemon — Windows (Phase 2).

Reads the Claude OAuth token from the native-Windows credentials path and
polls the Anthropic API for rate-limit utilization data. BLE glue added in
later plans.
"""

import asyncio
import calendar
import datetime
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

try:
    from . import clawd_activity
except ImportError:  # run as a script, not as the daemon package
    import clawd_activity

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
ACTIVITY_TICK = 1          # while Claude Code's state is mirrored: react within a second
CONNECT_RETRIES = 3        # D-01: attempts before giving up on a device
CONNECT_RETRY_DELAY = 2.0  # D-01: seconds between failed connect attempts
ZOMBIE_BREAK_LIMIT = 1     # D-03: consecutive write failures before abandoning a half-open link
                           # N=1: breaks at T=60s, leaves ~60s headroom for reconnect+poll inside 120s SLA
                           # N=2 would bust the 120s budget before reconnect even begins
RECONNECT_BACKOFF_CAP = 8  # D-05: fast-reconnect cap (seconds); keeps stacked retries inside 120s SLA
                           # ~5–10s band per CONTEXT.md Claude's Discretion; 8 chosen as middle ground

# Optional reset chime.
# Optional clock display. 
# Config lives under the same Clawdmeter dir as daemon.log.
CONFIG_FILE = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Clawdmeter" / "config"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}


def _build_file_logger() -> logging.Logger | None:
    """Create a rotating file logger for field diagnostics, or None.

    Autostart launches the tray under pythonw.exe, which has no console — stdout
    is discarded (and is in fact None, making print() unsafe). A rotating file is
    then the ONLY trail when the daemon stalls in the field. Windows-only: on the
    Linux dev box / CI the console print() suffices, and gating to win32 keeps the
    pure-helper unit tests from writing stray log files.
    """
    if sys.platform != "win32":
        return None
    logger = logging.getLogger("clawdmeter.daemon")
    if logger.handlers:
        return logger  # idempotent across re-import (tray imports this module)
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / "Clawdmeter" / "daemon.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        return None  # best-effort — logging setup must never stop the daemon
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


_FILE_LOGGER = _build_file_logger()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # Under pythonw sys.stdout is None and print() would raise — guard it so a
    # missing console can never crash the daemon thread (the silent-freeze mode).
    try:
        print(line, flush=True)
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    if _FILE_LOGGER is not None:
        _FILE_LOGGER.info(msg)


class AuthError(Exception):
    """Raised by poll_api on a genuine 401/403 — the token really is expired or
    invalid and the user must re-run `claude login`. Distinct from a None return,
    which means a TRANSIENT failure (network/DNS, timeout, rate-limit, 5xx) that
    must NOT be mislabeled as a token problem (SC#5: a boot-time `getaddrinfo
    failed` DNS blip wrongly fired the 'token expired' toast)."""

    @property
    def status(self) -> int:
        return self.args[0] if self.args else 0


def _config_value(key: str) -> str | None:
    """The raw value of `key` in the config file (the last one, if it appears
    twice), or None. Every config option is read through here."""
    found = None
    try:
        if CONFIG_FILE.exists():
            # utf-8-sig: Notepad may save a BOM; "replace": a stray ANSI
            # umlaut in a comment must not discard the whole config.
            text = CONFIG_FILE.read_text(encoding="utf-8-sig", errors="replace")
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                k, val = line.split("=", 1)
                if k.strip().lower() == key:
                    found = val.strip()
    except OSError:
        pass
    return found


def _config_choice(key: str, allowed: tuple[str, ...], default: str) -> str:
    """`key` lowercased if it is one of `allowed`, else `default`."""
    val = (_config_value(key) or "").lower()
    return val if val in allowed else default


def read_chime_setting() -> str:
    """Read the `chime` option from the config file. One of: off|on.

    Defaults to "off" so the device stays silent until the user opts in.
    """
    return _config_choice("chime", ("off", "on"), "off")


def read_clock_setting() -> str:
    """Read the `clock` option from the config file. One of: off|auto|12|24.

    Defaults to "auto": the device shows the time in place of the "Usage"
    title, 12h or 24h as this machine is set. `clock = off` keeps "Usage".
    """
    return _config_choice("clock", ("off", "auto", "12", "24"), "auto")


def read_activity_settings() -> dict:
    """Read the buddy options: activity (auto|on|off), screen_mode
    (usage|clawd|auto), corner_buddy (on|off), state_sounds (off|on).

    activity defaults to "auto": on once the Claude Code hooks are installed
    (clawd_activity.py --install, which install-windows.ps1 offers), off until
    then — without hooks there is no state to show.
    """
    opts = {"activity": "auto", "screen_mode": "auto", "corner_buddy": "on",
            "state_sounds": "on"}
    allowed = {"activity": ("auto", "off", "on"),
               "state_sounds": ("off", "on"),
               "screen_mode": ("usage", "clawd", "auto"),
               "corner_buddy": ("off", "on")}
    for key, ok in allowed.items():
        opts[key] = _config_choice(key, ok, opts[key])
    return opts


_SCREEN_MODES = {"usage": 0, "clawd": 1, "auto": 2}
_WORK_ANIMS = {"work think", "work coding", "write"}
WORK_ANIM_HOLD_S = 4


def _activity_key(payload: dict) -> tuple:
    """What the device sees of the buddy settings; a change in any of them is
    worth a write between polls."""
    return payload.get("a"), payload.get("sm"), payload.get("ua"), payload.get("ss")


def add_activity_fields(payload: dict) -> str | None:
    """Add what Claude Code is doing ("a"), the display mode ("sm"), the
    corner buddy switch ("ua") and the state-sound switch ("ss") when the
    config opts in. Returns the animation name sent, or None when activity is
    off (fields omitted entirely, so the device picks its own animations)."""
    opts = read_activity_settings()
    active = opts["activity"] == "on" or (
        opts["activity"] == "auto" and clawd_activity.is_installed())
    if not active:
        for k in ("a", "sm", "ua", "ss"):
            payload.pop(k, None)
        return None
    limit_hit = int(payload.get("s", 0) or 0) >= 100 or int(payload.get("w", 0) or 0) >= 100
    anim = clawd_activity.current_anim(limit_hit=limit_hit)
    payload["a"] = anim
    payload["sm"] = _SCREEN_MODES[opts["screen_mode"]]
    payload["ua"] = opts["corner_buddy"] == "on"
    if opts["state_sounds"] == "on":
        payload["ss"] = True        # the device sounds allow / done / limit
    else:
        payload.pop("ss", None)
    return anim


DEFAULT_VOLUME = 70


def read_volume_setting() -> int:
    """Read `volume` (0..100, % of the board's full sound level). Default 70."""
    try:
        return max(0, min(100, int((_config_value("volume") or "").rstrip("%"))))
    except ValueError:
        return DEFAULT_VOLUME


def add_chime_field(payload: dict) -> None:
    """Add "c":1 to the payload when the config opts in, so the firmware may
    sound the session-reset chime. Omitted entirely when chime is off."""
    if read_chime_setting() == "on":
        payload["c"] = 1
    payload["vol"] = read_volume_setting()   # level for every sound on the device


def detect_hour_format() -> int:
    """Best-effort 12h/24h detection on Windows via the registry. Returns 12 or 24."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as k:
            # iTime: "1" = 24-hour, "0" = 12-hour.
            val, _ = winreg.QueryValueEx(k, "iTime")
            return 24 if str(val).strip() == "1" else 12
    except (ImportError, OSError):
        return 24


def add_clock_fields(payload: dict) -> None:
    """Add "t" (local wall-clock epoch) + "tf" (12|24) when the config opts in."""
    clock = read_clock_setting()
    if clock == "off":
        return
    tf = 24 if clock == "24" else 12 if clock == "12" else detect_hour_format()
    payload["t"] = int(time.time()) + time.localtime().tm_gmtoff
    payload["tf"] = tf


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        # Network/DNS/timeout — transient. Return None (no toast), retry next tick.
        log(f"API call failed: {e}")
        return None
    if resp.status_code in (401, 403):
        # Genuine auth rejection — the ONLY case that warrants the actionable
        # "run claude login" toast.
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        raise AuthError(resp.status_code)
    if resp.status_code >= 400:
        # A 429 means the plan's limit is used up — which rejects this probe
        # too. It still carries the rate-limit headers, though, and that is the
        # one moment the device most needs them: dropping the response here
        # left the display frozen on the last value and then flipped it to
        # the idle "no data" screen, exactly when usage hit 100 %. So a 429
        # WITH the headers is reported like any other reading. Everything
        # else (5xx, a 429 without headers) is transient: skip this tick.
        has_limits = bool(
            resp.headers.get("anthropic-ratelimit-unified-5h-utilization")
            or resp.headers.get("anthropic-ratelimit-unified-overage-utilization")
        )
        if resp.status_code != 429 or not has_limits:
            log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        log("API HTTP 429: limit reached, reporting it from the response headers")

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        # Clamped: at the limit the reported utilization can edge past 1.0,
        # and "103 %" on a bar that ends at 100 only reads as a glitch.
        try:
            return max(0, min(100, int(round(float(util) * 100))))
        except ValueError:
            return 0

    if resp.headers.get("anthropic-ratelimit-unified-5h-utilization"):
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
            "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
            "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
            "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
            "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
            "acct": "pro",
            "ok": True,
        }
    else:
        reset_ts = hdr("anthropic-ratelimit-unified-overage-reset")
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-overage-utilization")),
            "sr": reset_minutes(reset_ts),
            "w": 0,
            "wr": 0,
            "st": hdr("anthropic-ratelimit-unified-status", "unknown"),
            "acct": "ent",
            **_billing_period_info(now, reset_ts),
            "ok": True,
        }
    add_chime_field(payload)   # adds "c":1 iff the config opts in
    add_clock_fields(payload)   # adds "t" + "tf" iff the config opts in
    return payload


def _billing_period_info(now: float, reset_ts: str) -> dict:
    """Fraction of billing period elapsed (tp, 0-100) and period length in days (pd).

    Monthly window is assumed (headers expose only reset_ts, not period). Per the
    Claude Enterprise Admin API reference, spend-limit period's "only value today
    is monthly" — see the macOS daemon for the full note.
    """
    try:
        period_end = float(reset_ts)
    except ValueError:
        return {"tp": 0, "pd": 30, "rd": ""}
    if period_end <= 0:
        # reset_ts defaults to "0" whenever the overage-reset header is absent
        # (e.g. a 200 that simply carries no billing headers). fromtimestamp(0)
        # is 1970; stepping one month back lands in 1969, and datetime.timestamp()
        # raises OSError for pre-1970 dates on Windows — taking the whole poll
        # loop down. Bail out to the neutral default instead.
        return {"tp": 0, "pd": 30, "rd": ""}
    try:
        dt_end = datetime.datetime.fromtimestamp(period_end)
        prev_month = dt_end.month - 1 or 12
        prev_year = dt_end.year if dt_end.month > 1 else dt_end.year - 1
        prev_day = min(dt_end.day, calendar.monthrange(prev_year, prev_month)[1])
        dt_start = dt_end.replace(year=prev_year, month=prev_month, day=prev_day)
        period_start = dt_start.timestamp()
    except (OSError, OverflowError, ValueError):
        # Belt-and-braces beyond the <= 0 guard above (#104): Windows
        # datetime.timestamp()/fromtimestamp() also raise OSError(22)/
        # OverflowError/ValueError for out-of-range NON-zero values (e.g. a
        # far-future "99999999999999" header, which overflows fromtimestamp).
        # Garbage must never crash the daemon thread — degrade to the safe
        # default instead (field report: OSError(22) killed the poll loop).
        return {"tp": 0, "pd": 30, "rd": ""}
    period_len = period_end - period_start
    if period_len <= 0:
        return {"tp": 0, "pd": 30, "rd": ""}
    pct_val = (now - period_start) / period_len * 100
    return {
        "tp": max(0, min(100, int(round(pct_val)))),
        "pd": int(round(period_len / 86400)),
        "rd": f"{dt_end.strftime('%b')} {dt_end.day}",
    }


def _mac_from_pnp_instance_id(instance_id: str) -> str | None:
    """Recover a canonical BLE MAC ("AA:BB:CC:DD:EE:FF") from a PnP instance id.

    Windows encodes a paired BLE device's address in its PnP instance id as a
    12-hex run after a ``DEV_`` token, e.g.::

        BTHLE\\DEV_98A316A5D706\\7&B8081D1&0&98A316A5D706  ->  98:A3:16:A5:D7:06

    Returns None when no ``DEV_<12 hex>`` token is present. Pure — the
    subprocess that produces the instance ids lives in discover_bonded_addresses().
    """
    m = re.search(r"DEV_([0-9A-Fa-f]{12})(?![0-9A-Fa-f])", instance_id)
    if not m:
        return None
    h = m.group(1).upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def read_device_preference() -> str | None:
    """Read the `device` option from the config file, or None.

    Names which board this machine should drive when several are paired. Takes
    either the suffix the board advertises ("F629", as in "Clawdmeter F629") or
    a full address ("28:84:85:4B:F6:29"). Matching is on the hex digits alone,
    so colons and case don't matter.
    """
    val = re.sub(r"[^0-9A-Fa-f]", "", _config_value("device") or "").upper()
    return val or None


def discover_bonded_addresses() -> list[str]:
    """Return every bonded Clawdmeter address known to this machine.

    A device that is paired AND connected to Windows stops advertising, so
    BleakScanner can't see it (the steady state once paired — see
    README-windows.md). WinRT can still connect to it directly by address, so
    we recover the addresses from the OS:

    1. CLAWDMETER_BLE_ADDRESS env override (skips discovery — testing / pinning).
    2. Windows PnP table, filtered to the device's FriendlyName.

    ALL matches are returned, not just the first. Boards advertise their own
    name suffix now, so a desk can hold several — and taking the first row of
    that list meant one powered-off board could keep the daemon retrying it
    forever while a perfectly reachable one sat next to it. The caller walks
    this list on failure. A `device` line in the config is honoured by sorting
    its match to the front rather than by hiding the rest, so a typo degrades
    into "wrong order" instead of "no device at all".

    Non-Windows or any failure returns an empty list.
    """
    if override := os.environ.get("CLAWDMETER_BLE_ADDRESS"):
        return [override.strip().upper()]
    if sys.platform != "win32":
        return []
    command = (
        "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
        # -like, not -eq: boards append the last two bytes of their MAC
        # ("Clawdmeter 35F9") so several of them stay distinguishable.
        f"Where-Object {{ $_.FriendlyName -like '{DEVICE_NAME}*' }} | "
        "Select-Object -ExpandProperty InstanceId"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Bonded-address lookup failed: {e}")
        return []
    found: list[str] = []
    for line in result.stdout.splitlines():
        if (mac := _mac_from_pnp_instance_id(line)) and mac not in found:
            found.append(mac)
    if (want := read_device_preference()) and len(found) > 1:
        # Endswith, so "F629" matches 28:84:85:4B:F6:29 — that suffix is what
        # the board puts in its advertised name and what the user reads off it.
        found.sort(key=lambda m: not m.replace(":", "").endswith(want))
    return found


# Which bonded device to try next. Advanced by the main loop after a failed
# attempt, so an unreachable board is stepped over instead of retried forever;
# reset on a successful session so the working one stays the default.
_candidate_index = 0


def rotate_candidate() -> None:
    global _candidate_index
    _candidate_index += 1


async def acquire_target():
    """Return a connectable handle for the Clawdmeter, or None.

    Targets only devices bonded to THIS machine (via the PnP table /
    CLAWDMETER_BLE_ADDRESS) — it never scans for a nearby device by name, so it
    can't grab a stranger's or the wrong nearby unit. The device must be paired
    with Windows once first (the documented setup). Returns a BLEDevice or None.
    """
    addresses = discover_bonded_addresses()
    if not addresses:
        return None
    address = addresses[_candidate_index % len(addresses)]
    if len(addresses) > 1:
        log(f"Not advertising; connecting to bonded address {address} "
            f"({(_candidate_index % len(addresses)) + 1} of {len(addresses)} paired)")
    else:
        log(f"Not advertising; connecting to bonded address {address}")
    # CRITICAL: hand BleakClient a BLEDevice, not the bare address string. WinRT's
    # connect() resolves a bare string via an advertisement scan (find_device_by_address)
    # — which always fails for a bonded device that has stopped advertising, the very
    # case we are handling. A BLEDevice sets _device_info directly, so WinRT connects
    # via from_bluetooth_address_with_bluetooth_address_type_async and skips the scan.
    return BLEDevice(address, DEVICE_NAME, None)


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        # The refresh subscription is optional — the 60s poll loop works without it.
        # WinRT's start_notify() CCCD write can raise a raw OSError/WinError (not
        # wrapped as BleakError) when the peer GATT server is transiently unavailable,
        # e.g. a just-power-cycled ESP32 whose server is not yet ready (G-03-01, SC#3).
        # Degrade gracefully instead of crashing the daemon so it stays single-process
        # across a power-cycle reconnect (SC#4, no restart).
        try:
            await self.client.start_notify(REQ_CHAR_UUID, self._on_refresh)
        except (BleakError, ValueError, OSError) as e:
            log(f"Refresh subscription unavailable: {e}")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except (BleakError, OSError) as e:
            # WinRT can raise a raw OSError/WinError (NOT wrapped as BleakError)
            # when the peer GATT server goes transiently unavailable mid-write —
            # the same failure class setup_refresh_subscription() guards against.
            # Returning False trips the zombie-link break -> clean reconnect,
            # rather than an uncaught exception killing the daemon thread (the
            # silent-freeze failure mode, SC#2 field report).
            log(f"Write failed: {e}")
            return False


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        tok = data.get("accessToken")
        if isinstance(tok, str) and tok.strip():
            return tok
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _windows_credential_candidates() -> list[Path]:
    """Return the ordered list of credential file paths to probe (first hit wins).

    Priority:
    1. CLAUDE_CREDENTIALS_PATH env override (D-03, project-specific)
    2. CLAUDE_CONFIG_DIR env override (official Claude override)
    3. D-02 candidate list: home/.claude, LOCALAPPDATA/Claude, APPDATA/Claude
    """
    # Priority 1: project-specific env override (D-03)
    if override := os.environ.get("CLAUDE_CREDENTIALS_PATH"):
        return [Path(override)]
    # Priority 2: official CLAUDE_CONFIG_DIR env override
    if config_dir := os.environ.get("CLAUDE_CONFIG_DIR"):
        return [Path(config_dir) / ".credentials.json"]
    # Priority 3: D-02 candidate list — first hit wins
    home = Path.home()
    local_appdata = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return [
        home / ".claude" / ".credentials.json",          # primary (confirmed by docs)
        local_appdata / "Claude" / ".credentials.json",  # fallback 2
        appdata / "Claude" / ".credentials.json",        # fallback 3
    ]


def read_token() -> str | None:
    """Read the Claude OAuth access token from the first available credential file."""
    for path in _windows_credential_candidates():
        try:
            return _extract_access_token(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return None


def _read_expiry() -> str:
    """Return human-readable expiry from the first-hit credentials file.

    Reads claudeAiOauth.expiresAt (epoch milliseconds — JS convention).
    Divides by 1000 before passing to fromtimestamp (Python expects seconds).
    Returns 'expiry unknown' on any parse failure.
    """
    for path in _windows_credential_candidates():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = json.loads(raw)
            oauth = data.get("claudeAiOauth", {})
            expires_ms = oauth.get("expiresAt")
            if expires_ms is None:
                return "expiry unknown"
            # CRITICAL: expiresAt is JS-convention epoch milliseconds; divide by 1000
            # before fromtimestamp (Python expects seconds). Raw value -> year ~57000.
            dt = datetime.datetime.fromtimestamp(
                expires_ms / 1000, tz=datetime.timezone.utc
            )
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except (TypeError, ValueError, OSError, AttributeError, json.JSONDecodeError):
            return "expiry unknown"
    return "expiry unknown"


# ---------------------------------------------------------------------------
# Token renewal through the Claude CLI
# ---------------------------------------------------------------------------
# The daemon still never refreshes the OAuth token itself: that would race
# Claude Code's own rotation (two refreshers, one refresh token) and feed the
# OAuth endpoint's rate limit. But the token only goes stale when no Claude
# Code has run for a while -- and Claude Code renews it whenever it starts and
# finds it expired. So on a 401 the daemon runs the official CLI once, with
# the smallest request it accepts, and reads the file again: the owner does the
# refresh, with its own locking, and the device keeps its numbers.

CLI_RENEW_COOLDOWN_S = 15 * 60   # one attempt per quarter hour, not one per poll
CLI_RENEW_TIMEOUT_S = 120
# Haiku, one word back, no tools, no MCP servers, nothing saved to the session
# history, no hooks -- the run must not show up on the device as Claude working
# (CLAWDMETER_SKIP_HOOK below covers a CLI that would ignore disableAllHooks).
# Not --bare: it skips OAuth, which is the whole point of the run.
CLI_RENEW_ARGS = [
    "-p", "Reply with OK.",
    "--model", "haiku",
    "--tools", "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--settings", '{"disableAllHooks": true}',
]

_last_cli_renew = 0.0   # time.monotonic() of the last attempt; 0 = none yet


def read_cli_refresh_setting() -> str:
    """`cli_refresh` (on|off, default on): may the daemon run the Claude CLI to
    renew an expired token?"""
    return _config_choice("cli_refresh", ("on", "off"), "on")


def _npm_shim_command(shim: Path) -> list[str] | None:
    """npm installs `claude` as a batch shim (claude.cmd). A batch file runs
    through cmd.exe, whose quoting is not the one subprocess writes for -- the
    JSON --settings value and the empty --tools argument would arrive mangled.
    So start the package's cli.js with node directly, as the shim itself does."""
    script = shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = shim.parent / "node.exe"
    node_cmd = str(node) if node.is_file() else shutil.which("node")
    if script.is_file() and node_cmd:
        return [node_cmd, str(script)]
    return None


def find_claude_cli() -> list[str] | None:
    """The command that starts the Claude CLI: the config's `claude_cli`, else
    PATH, else the native installer's %USERPROFILE%\\.local\\bin\\claude.exe.
    An npm shim is resolved to node + cli.js. None when nothing usable exists."""
    configured = _config_value("claude_cli")
    if configured:
        path = Path(configured.strip('"')).expanduser()
    else:
        found = shutil.which("claude")
        path = Path(found) if found else Path.home() / ".local" / "bin" / "claude.exe"
    if not path.is_file():
        return None
    if sys.platform == "win32" and path.suffix.lower() != ".exe":
        # claude.cmd / claude.ps1, or npm's extensionless sh script beside them
        return _npm_shim_command(path)
    return [str(path)]


def _kill_tree(proc) -> None:
    """End the CLI and everything it started. proc.kill() alone terminates
    claude.exe only; its children would outlive it."""
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # started in its own session
            return
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _communicate_or_stop(proc, stop_event: asyncio.Event | None) -> bytes | None:
    """stderr of the finished CLI, or None when it ran past CLI_RENEW_TIMEOUT_S
    or the daemon is stopping -- Quit must not wait for a hung CLI."""
    comm = asyncio.ensure_future(proc.communicate())
    waits = [comm]
    if stop_event is not None:
        waits.append(asyncio.ensure_future(stop_event.wait()))
    try:
        await asyncio.wait(waits, timeout=CLI_RENEW_TIMEOUT_S,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        pending = [t for t in waits if not t.done()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    if comm.done() and not comm.cancelled():
        return comm.result()[1] or b""
    return None


async def renew_token_via_cli(stop_event: asyncio.Event | None = None) -> bool:
    """Run the Claude CLI once so it renews the expired token. True when the
    CLI ran and exited cleanly -- the caller then reads the token again."""
    global _last_cli_renew
    if read_cli_refresh_setting() == "off":
        log("CLI renewal is off (cli_refresh = off)")
        return False
    now = time.monotonic()
    if _last_cli_renew and now - _last_cli_renew < CLI_RENEW_COOLDOWN_S:
        left = (CLI_RENEW_COOLDOWN_S - (now - _last_cli_renew)) / 60
        log(f"Last CLI renewal did not help; next attempt in {left:.0f} min")
        return False
    _last_cli_renew = now
    cmd = find_claude_cli()
    if not cmd:
        log("Token expired and no usable claude CLI found to renew it "
            "(set claude_cli = <path to claude.exe> in the config)")
        return False
    log("Token expired; letting the Claude CLI renew it (one short `claude -p`)")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, *CLI_RENEW_ARGS,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(),
            env={**os.environ, "CLAWDMETER_SKIP_HOOK": "1"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=sys.platform != "win32",
        )
    except (OSError, NotImplementedError) as e:
        log(f"Could not start the Claude CLI: {e}")
        return False
    try:
        err = await _communicate_or_stop(proc, stop_event)
    finally:
        # Timeout, Quit, or this task cancelled: never leave the CLI behind.
        if proc.returncode is None:
            _kill_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
    if err is None:
        if stop_event is not None and stop_event.is_set():
            log("Stopping; ended the Claude CLI renewal")
        else:
            log(f"Claude CLI did not finish within {CLI_RENEW_TIMEOUT_S}s; ended it")
        return False
    if proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip().splitlines()
        log(f"Claude CLI exited {proc.returncode}: {msg[-1][:200] if msg else ''} "
            "-- run `claude login` if the sign-in itself has expired")
        return False
    log("Claude CLI ran; reading the token again")
    return True


async def _poll_after_cli_renewal(stop_event: asyncio.Event | None = None) -> dict | None:
    """After a 401: let the CLI renew the token, then poll once more. Raises
    AuthError when the token is still dead (or renewal is off / cooling down);
    None when the daemon is stopping, so Quit does not flash "No data"."""
    def stopping() -> bool:
        return stop_event is not None and stop_event.is_set()

    if stopping():
        raise AuthError(401)        # quitting: don't start a CLI now
    if not await renew_token_via_cli(stop_event):
        if stopping():
            return None             # Quit interrupted the renewal
        raise AuthError(401)
    token = read_token()
    if not token:
        raise AuthError(401)
    return await poll_api(token)   # a second 401 propagates


async def _wait_first(*events: asyncio.Event, timeout: float) -> None:
    """Return when any of `events` is set, or after `timeout` seconds.

    Lets the poll loop's TICK wait wake immediately on a stop signal (clean,
    responsive Quit) without losing the refresh-request wakeup — instead of
    waiting only on refresh_requested and re-checking stop_event up to TICK
    later. Cancels and drains the loser tasks so they don't warn.
    """
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def connect_and_run(device, stop_event: asyncio.Event, tray_state=None) -> bool:
    """Connect to device and poll until disconnected or stopped.

    Returns True if at least one successful write occurred.

    `device` is a BLEDevice — either from an advertisement scan or built from the
    bonded address by acquire_target(). The getattr keeps the log line robust if a
    bare address string is ever passed in.
    """
    log(f"Connecting to {getattr(device, 'address', device)}...")
    # D-01: retry wrapper — defeats WinRT post-wake failure modes
    # (Could not get GATT services: Unreachable, stale is_connected).
    # Rebuild a fresh BleakClient each attempt (locked D-05 recipe).
    client = None
    for attempt in range(CONNECT_RETRIES):
        # D-05: pass BLEDevice (not address string), address_type="random" (NimBLE
        # static-random), use_cached_services=False (DIY firmware — WinRT GATT cache
        # may be stale after firmware reflash).
        client = BleakClient(
            device,
            address_type="random",
            use_cached_services=False,
        )
        try:
            await client.connect()
        except (BleakError, OSError, asyncio.TimeoutError, AssertionError) as e:
            # WinRT service discovery inside connect() can surface a raw OSError
            # (WinError) or even a bare AssertionError from bleak's FutureLike
            # (assert self._result) when the peer drops the link mid-discovery —
            # neither is wrapped as BleakError. Treat them as a normal failed
            # attempt so the D-01 retry loop handles them, instead of letting an
            # uncaught exception kill the daemon thread (the "daemon crashed"
            # tray toast + silent polling stop, field report).
            log(f"Connection attempt {attempt + 1}/{CONNECT_RETRIES} failed: {type(e).__name__}: {e}")
            try:
                await client.disconnect()
            except BleakError:
                pass
            if attempt < CONNECT_RETRIES - 1:
                await asyncio.sleep(CONNECT_RETRY_DELAY)
            continue

        if not client.is_connected:
            log(f"Connection attempt {attempt + 1}/{CONNECT_RETRIES} failed (not connected)")
            try:
                await client.disconnect()
            except BleakError:
                pass
            if attempt < CONNECT_RETRIES - 1:
                await asyncio.sleep(CONNECT_RETRY_DELAY)
            continue

        # Connected successfully
        break
    else:
        log(f"Connection failed after {CONNECT_RETRIES} attempts")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0  # D-03: poll immediately on first connect
    used_successfully = False
    consecutive_failures = 0  # D-03: zombie-link break counter
    # Claude Code's state between polls: the last reading is resent with the
    # new "a" (or a changed screen_mode / corner_buddy) as soon as it changes.
    last_payload: dict | None = None
    last_anim: str | None = None
    last_key: tuple = (None, None, None, None)
    last_anim_sent = 0.0

    def note_write_failure() -> bool:
        """Count a failed device write toward the zombie-link breaker.

        Returns True when too many writes have failed in a row and the caller
        should abandon the (likely zombie) link so the outer loop reconnects.
        Applies to every device write — data payloads and no-data beats alike —
        so a dead link still trips the breaker even when the token is also dead.
        """
        nonlocal consecutive_failures
        consecutive_failures += 1
        if consecutive_failures >= ZOMBIE_BREAK_LIMIT:
            log(
                f"Zombie link detected ({consecutive_failures} consecutive"
                f" write failures); abandoning connection"
            )
            return True
        return False

    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                # Pure free-ride: read whatever access token Claude Code currently
                # holds and NEVER refresh it ourselves. Claude Code (the token's owner)
                # does all refreshing; refreshing here would race its rotation and feed
                # the OAuth endpoint's rate limit (429). When the token is dead we just
                # show "No data" until the CLI re-seeds it.
                token = read_token()  # D-09: fresh each cycle
                if not token:
                    last_payload = None   # nothing to resend over "No data"
                    log("No token; signalling no-data to device")
                    if tray_state:
                        tray_state.set_error("token expired — run claude login")
                    if await session.write_payload({"ok": False}):
                        last_poll = time.time()
                        consecutive_failures = 0  # D-03: healthy link
                    elif note_write_failure():
                        break
                else:
                    payload = None
                    expired = False
                    try:
                        try:
                            payload = await poll_api(token)
                        except AuthError as e:
                            # We never refresh the token ourselves; Claude Code (its
                            # owner) does whenever it runs -- so let it run once.
                            # Only for a 401: a 403 is not an expired token, and
                            # renewing it would just bill a request every 15 min.
                            if e.status != 401:
                                raise
                            payload = await _poll_after_cli_renewal(stop_event)
                    except AuthError:
                        # Still dead: renewal is off, cooling down, failed (logged
                        # just above), or the sign-in itself expired and only
                        # `claude login` can re-seed it.
                        expired = True
                        log("Token still rejected; signalling no-data")
                        if tray_state:
                            tray_state.set_error("token expired — run claude login")
                    if payload is not None:
                        last_anim = add_activity_fields(payload)
                        last_key = _activity_key(payload)
                        if await session.write_payload(payload):
                            last_poll = last_anim_sent = time.time()
                            last_payload = payload
                            used_successfully = True
                            consecutive_failures = 0  # D-03: reset on success
                            if tray_state:
                                tray_state.set_connected(time.time())
                        elif note_write_failure():
                            break
                    elif expired:
                        # Token genuinely dead -> show "No data" now instead of stale numbers.
                        # Transient poll failures (payload None without expiry) stay silent.
                        last_payload = None
                        log("No data (token dead); signalling idle to device")
                        if await session.write_payload({"ok": False}):
                            last_poll = time.time()
                            consecutive_failures = 0  # D-03: healthy link
                        elif note_write_failure():
                            break
                    # else: payload is None from a TRANSIENT failure (network/DNS,
                    # timeout, rate-limit, 5xx). poll_api already logged it; do NOT
                    # toast "token expired" — that mislabeled a boot-time DNS blip
                    # as an auth problem (SC#5). Leave tray state unchanged; the next
                    # tick retries and set_connected() recovers it.
            elif last_payload is not None:
                # Between polls, push a changed Claude Code state -- or a
                # changed screen_mode / corner_buddy in the config -- right
                # away by resending the last reading with the new fields, so
                # the device reacts within a second instead of at the next poll.
                payload = dict(last_payload)
                anim = add_activity_fields(payload)
                # Within a turn Claude flips between thinking and tool calls
                # several times a second; hold each working animation a few
                # seconds. Anything that wants you (allow, done, limit) goes
                # out at once.
                churn = (anim in _WORK_ANIMS and last_anim in _WORK_ANIMS
                         and now - last_anim_sent < WORK_ANIM_HOLD_S)
                key = _activity_key(payload)
                mode_changed = key[1:] != last_key[1:]
                if mode_changed or (anim != last_anim and not churn):
                    add_clock_fields(payload)
                    if await session.write_payload(payload):
                        consecutive_failures = 0
                        last_payload = payload
                        last_key = key
                        if anim != last_anim:
                            last_anim = anim
                            last_anim_sent = now
                    elif note_write_failure():
                        break

            # Wake on a refresh request OR a stop, whichever comes first. Waking
            # promptly on stop_event is what lets the finally below run
            # client.disconnect() before the process exits, so the peer gets a
            # clean GATT disconnect (returns to its waiting screen) instead of
            # being left frozen on stale data after Quit (SC#3 graceful shutdown).
            # Activity on: tick fast so state changes reach the device quickly.
            tick = ACTIVITY_TICK if last_anim is not None else TICK
            await _wait_first(session.refresh_requested, stop_event, timeout=tick)
    finally:
        # Clean GATT disconnect on the way out — this is what tells the peripheral
        # the link is gone. WinRT can surface a raw OSError (not BleakError) here,
        # so swallow both; the link tears down regardless once we exit.
        try:
            await client.disconnect()
        except (BleakError, OSError, AssertionError):
            # bleak's WinRT disconnect() also has bare asserts (e.g. assert char
            # while tearing down notifications on an already-gone peer); swallow
            # it too — the link tears down regardless once we exit.
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


def _next_backoff(current: int, cap: int) -> int:
    """D-05: double current backoff value, clamped to cap.

    Pure helper — unit-testable without driving the main loop.
    Used by both slow-search (cap=60) and fast-reconnect (cap=RECONNECT_BACKOFF_CAP) regimes.
    """
    return min(current * 2, cap)


async def main(tray_state=None) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Populate the shared state object so the tray can route Quit through
    # loop.call_soon_threadsafe (RESEARCH Pitfall 2).  Additive — the existing
    # stop_event = asyncio.Event() line above is unchanged.
    if tray_state is not None:
        tray_state.loop = loop
        tray_state.stop_event = stop_event

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    # OS signal handlers can only be installed from the main thread, and
    # loop.add_signal_handler is unsupported on Windows. When running under the
    # tray (04-03) the loop lives in a background thread and the tray owns clean
    # shutdown via stop_event (loop.call_soon_threadsafe), so skip silently there.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                # Windows: add_signal_handler not supported; fall back to signal.signal
                try:
                    signal.signal(sig, _stop)
                except ValueError:
                    # Not the main thread of the main interpreter — tray owns shutdown.
                    pass

    log("=== Claude Usage Tracker Daemon (BLE, Windows) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    # D-05: two distinct backoff regimes — slow-search (device absent) vs fast-reconnect (link dropped)
    search_backoff = 1     # caps at 60s — gentle, for a device that is genuinely absent/off
    reconnect_backoff = 1  # caps at RECONNECT_BACKOFF_CAP — fast, to clear the 120s SLA after a drop
    while not stop_event.is_set():
        device = await acquire_target()
        if not device:
            # Slow-search regime: device was not found by scan — back off gently
            if tray_state:
                tray_state.set_scanning()
            log(f"Device not found, retrying in {search_backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=search_backoff)
            except asyncio.TimeoutError:
                pass
            search_backoff = _next_backoff(search_backoff, 60)
            continue

        ok = await connect_and_run(device, stop_event, tray_state)
        if not ok:
            # Step to the next paired board. With one paired device this is a
            # no-op; with several it stops an absent one from holding the
            # daemon hostage while a reachable board waits beside it.
            rotate_candidate()
            # Fast-reconnect regime: had/attempted a link that dropped — retry quickly
            if tray_state:
                tray_state.set_scanning()
            log(f"Connection lost, reconnecting in {reconnect_backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=reconnect_backoff)
            except asyncio.TimeoutError:
                pass
            reconnect_backoff = _next_backoff(reconnect_backoff, RECONNECT_BACKOFF_CAP)
        else:
            # Successful session — reset reconnect counter to floor; search_backoff also reset
            reconnect_backoff = 1
            search_backoff = 1
            # The rotation index is deliberately left alone: it already points
            # at the board that just worked, so a dropped link reconnects to
            # the same one instead of starting the walk over.


if __name__ == "__main__":
    if sys.platform != "win32":
        print(
            "Warning: running under Linux/WSL — WinRT BLE will not be available.",
            file=sys.stderr,
        )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
