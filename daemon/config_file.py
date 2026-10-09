"""Reading the daemons' `key = value` config file (shared by macOS and Windows)."""
from pathlib import Path


def value(path: Path, key: str) -> str | None:
    """The raw value of `key` in the config file at `path` (the last one, if it
    appears twice), or None. `#` starts a comment; keys are case-insensitive."""
    found = None
    try:
        if path.exists():
            # utf-8-sig: Notepad may save a BOM; "replace": a stray ANSI
            # umlaut in a comment must not discard the whole config.
            text = path.read_text(encoding="utf-8-sig", errors="replace")
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


def choice(path: Path, key: str, allowed: tuple[str, ...], default: str) -> str:
    """`key` lowercased if it is one of `allowed`, else `default`."""
    val = (value(path, key) or "").lower()
    return val if val in allowed else default
