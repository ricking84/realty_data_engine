"""Load local-only Azure settings from environment variables and an ignored JSON file."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "docs" / "azure-creds.json"

_CONFIG_KEYS = {
    "azure_sql": {
        "server": "AZURE_SQL_SERVER",
        "database": "AZURE_SQL_DATABASE",
        "username": "AZURE_SQL_USERNAME",
        "password": "AZURE_SQL_PASSWORD",
        "authentication": "AZURE_SQL_AUTHENTICATION",
        "driver": "AZURE_SQL_DRIVER",
        "connection_timeout": "AZURE_SQL_CONNECTION_TIMEOUT",
        "connection_string": "AZURE_SQL_CONNECTION_STRING",
    },
    "azure_storage": {
        "account_url": "AZURE_STORAGE_ACCOUNT_URL",
        "container": "AZURE_STORAGE_CONTAINER",
    },
}


def load_settings(
    env: Mapping[str, str] | None = None,
    config_path: str | Path | None = None,
) -> dict[str, str]:
    """Return environment settings, optionally filling missing values from local JSON.

    Passing ``env`` explicitly keeps configuration file loading opt-in for callers and tests.
    Environment variables take precedence over values in the JSON file.
    """
    settings = dict(os.environ if env is None else env)
    if config_path is None and env is not None:
        return settings

    path = Path(config_path) if config_path is not None else Path(
        settings.get("REALTY_DATA_ENGINE_CREDENTIALS_FILE", DEFAULT_CONFIG_PATH)
    )
    if not path.exists():
        return settings

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in local credentials file: {path}") from error
    if not isinstance(config, dict):
        raise ValueError(f"Local credentials file must contain a JSON object: {path}")

    for section, key_map in _CONFIG_KEYS.items():
        values = config.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"Expected '{section}' to be a JSON object in {path}")
        for config_key, env_key in key_map.items():
            value = values.get(config_key)
            if value is not None and not settings.get(env_key):
                settings[env_key] = str(value)

    return settings