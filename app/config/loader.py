"""Reads config.yaml from disk and turns it into a validated GatewayConfig.

This is the only place in the code that knows the config lives in a YAML
file. If it later moves to an env var, etcd or a control plane, only this
file changes.
"""

from pathlib import Path

import yaml

from app.config.models import GatewayConfig


class ConfigError(Exception):
    """Raised when the config file is missing or not usable."""


def load_config(path: str | Path) -> GatewayConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    return parse_config(path.read_bytes(), source=str(path))


def parse_config(data: bytes, source: str = "<config>") -> GatewayConfig:
    """Validate config file contents. The hot reloader uses this directly, so
    it validates exactly the bytes it hashed, not a re-read of the file that
    may have changed in between."""
    try:
        # safe_load only builds plain dicts, lists and scalars. yaml.load can
        # run arbitrary Python through tags like !!python/object.
        raw = yaml.safe_load(data)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {source}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config file is empty or not a mapping: {source}")

    # Raises pydantic.ValidationError listing every problem at once.
    return GatewayConfig.model_validate(raw)
