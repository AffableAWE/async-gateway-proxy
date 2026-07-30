# app/config.py
from pathlib import Path
from typing import List
import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator


class RouteConfig(BaseModel):
    """One routing rule: 'requests whose path starts with `prefix`
    should be forwarded to `upstream`'."""
    prefix: str = Field(..., description="Path prefix to match, e.g. '/users'")
    upstream: str = Field(..., description="Base URL of the upstream service")

    @field_validator("prefix")
    @classmethod
    def prefix_must_start_with_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"prefix must start with '/': got {v!r}")
        # Normalize: strip trailing slash so '/users/' and '/users' behave the same. idk 
        return v.rstrip("/") or "/"

    @field_validator("upstream")
    @classmethod
    
    def upstream_must_be_url(cls, v: str) -> str:
        # Pydantic's HttpUrl is strict; we want a string we can pass to httpx,
        # so we validate then return as str. This catches typos like missing scheme.
        HttpUrl(v)
        return v.rstrip("/")

class GatewayConfig(BaseModel):
    """Top-level config object. Add fields here as the gateway grows
    (timeouts, auth keys, rate limits) — every new feature gets a typed home."""
    routes: List[RouteConfig]
    upstream_timeout_seconds: float = 5.0

def load_config(path: str | Path) -> GatewayConfig:
    """Read YAML from disk and parse into a validated GatewayConfig.
    Called once at startup from main.py's lifespan."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r") as f:
        raw = yaml.safe_load(f)  # safe_load avoids arbitrary code execution from YAML tags
    return GatewayConfig(**raw)
