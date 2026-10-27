"""Typed schema for config.yaml.

The rest of the gateway never touches raw YAML or dicts. It only sees these
models, so a bad config fails once at startup with a clear error instead of
failing later inside a request.
"""

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class RouteConfig(BaseModel):
    """One rule: requests whose path starts with `path_prefix` go to `upstream`."""

    # extra="forbid" turns a typo like `upstrem:` into a startup error
    # instead of a silently ignored key.
    model_config = ConfigDict(extra="forbid")

    path_prefix: str = Field(..., description="Path prefix to match, e.g. '/users'")
    upstream: str = Field(..., description="Base URL of the upstream, e.g. 'http://users:8000'")

    @field_validator("path_prefix")
    @classmethod
    def normalize_prefix(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"path_prefix must start with '/', got {v!r}")
        # '/users/' and '/users' should behave the same. A bare '/' stays '/'.
        return v.rstrip("/") or "/"

    @field_validator("upstream")
    @classmethod
    def validate_upstream(cls, v: str) -> str:
        # HttpUrl rejects things like a missing scheme ('localhost:8001').
        # We keep the value as a plain string because that is what httpx wants.
        HttpUrl(v)
        return v.rstrip("/")


class GatewayConfig(BaseModel):
    """Top-level config. New features (auth keys, rate limits) get fields here."""

    model_config = ConfigDict(extra="forbid")

    routes: list[RouteConfig] = Field(..., min_length=1)
    upstream_timeout_seconds: float = Field(5.0, gt=0)

    @model_validator(mode="after")
    def check_and_sort_routes(self) -> "GatewayConfig":
        prefixes = [r.path_prefix for r in self.routes]
        duplicates = {p for p in prefixes if prefixes.count(p) > 1}
        if duplicates:
            raise ValueError(f"duplicate path_prefix values: {sorted(duplicates)}")
        # Sort longest prefix first, once, at startup. The router can then
        # return the first match and it will always be the most specific one.
        self.routes.sort(key=lambda r: len(r.path_prefix), reverse=True)
        return self
