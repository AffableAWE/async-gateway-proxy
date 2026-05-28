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
        # Normalize: strip trailing slash so '/users/' and '/users' behave the same.
        return v.rstrip("/") or "/"

    @field_validator("upstream")
    @classmethod
    
    def upstream_must_be_url(cls, v: str) -> str:
        # Pydantic's HttpUrl is strict; we want a string we can pass to httpx,
        # so we validate then return as str. This catches typos like missing scheme.
        HttpUrl(v)
        return v.rstrip("/")
