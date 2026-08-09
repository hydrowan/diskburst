"""TOML config file support. Precedence: CLI flags > config file > built-in defaults."""

from __future__ import annotations

import os
import tomllib

DEFAULT_CONFIG_PATH = "~/.config/diskburst/config.toml"
VALID_KEYS = {"roots", "port", "workers", "min_file_mb", "cache", "live"}


def load_config(path: str | None = None) -> dict:
    """Read config TOML; missing default file is fine, missing explicit file is an error."""
    explicit = path is not None
    resolved = os.path.expanduser(path or DEFAULT_CONFIG_PATH)
    try:
        with open(resolved, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if explicit:
            raise SystemExit(f"config file not found: {resolved}") from None
        return {}
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"invalid TOML in {resolved}: {e}") from None
    unknown = sorted(set(data) - VALID_KEYS)
    if unknown:
        raise SystemExit(
            f"unknown config key(s) in {resolved}: {', '.join(unknown)} "
            f"(valid: {', '.join(sorted(VALID_KEYS))})"
        )
    return data
