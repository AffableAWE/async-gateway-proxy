"""Typed schema for config.yaml.

The rest of the gateway never touches raw YAML or dicts. It only sees these
models, so a bad config fails once at startup with a clear error instead of
failing later inside a request.
"""

import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PrivateAttr,
    field_validator,
    model_validator,
)

from app.ratelimit.base import Limit
from app.security import hash_api_key


def _normalize_prefix(v: str) -> str:
    if not v.startswith("/"):
        raise ValueError(f"path prefix must start with '/', got {v!r}")
    # '/users/' and '/users' should behave the same. A bare '/' stays '/'.
    return v.rstrip("/") or "/"


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
        return _normalize_prefix(v)

    @field_validator("upstream")
    @classmethod
    def validate_upstream(cls, v: str) -> str:
        # HttpUrl rejects things like a missing scheme ('localhost:8001').
        # We keep the value as a plain string because that is what httpx wants.
        HttpUrl(v)
        return v.rstrip("/")


class LimitConfig(BaseModel):
    """A token bucket: `capacity` is the largest burst, `refill_per_second`
    the long-run average rate."""

    model_config = ConfigDict(extra="forbid")

    capacity: int = Field(..., gt=0, le=1_000_000)
    refill_per_second: float = Field(..., gt=0)

    def to_limit(self) -> Limit:
        return Limit(self.capacity, self.refill_per_second)


class ApiKeyConfig(BaseModel):
    """One API key: who it belongs to and which routes it may use.

    Only the SHA-256 of the key is stored. The config file can then live in
    git or a Kubernetes ConfigMap without leaking anything usable.
    """

    model_config = ConfigDict(extra="forbid")

    # The name is logged and sent upstream in a header, so keep it plain.
    client: str = Field(..., pattern=r"^[A-Za-z0-9._-]{1,64}$")
    key_sha256: str
    routes: list[str] = Field(..., min_length=1, description="route prefixes this key may use")
    # Optional per-client limit. Unset: the rate_limit.default applies.
    rate_limit: LimitConfig | None = None

    @field_validator("key_sha256")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("key_sha256 must be 64 hex characters (a SHA-256 hash)")
        return v.lower()

    @field_validator("routes")
    @classmethod
    def normalize_routes(cls, v: list[str]) -> list[str]:
        return [_normalize_prefix(p) for p in v]


class AuthConfig(BaseModel):
    """If this section is present in config.yaml, every request needs a key."""

    model_config = ConfigDict(extra="forbid")

    keys: list[ApiKeyConfig] = Field(..., min_length=1)

    # hash -> key entry, built once at load time. PrivateAttr keeps it out
    # of the schema, so it can't be set from YAML.
    _by_hash: dict[str, ApiKeyConfig] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def index_keys(self) -> "AuthConfig":
        for entry in self.keys:
            if entry.key_sha256 in self._by_hash:
                raise ValueError(f"the same key is listed twice (client {entry.client!r})")
            self._by_hash[entry.key_sha256] = entry
        return self

    def lookup(self, presented_key: str) -> ApiKeyConfig | None:
        """The key entry matching a key a client sent, or None.

        A normal string compare can leak, through response timing, how many
        leading characters of a guess were right. Here the guess is hashed
        first, so the dict compares hashes the attacker can't steer: learning
        a hash prefix tells them nothing about the key.
        """
        return self._by_hash.get(hash_api_key(presented_key))


class RateLimitConfig(BaseModel):
    """If this section is present in config.yaml, requests are rate limited."""

    model_config = ConfigDict(extra="forbid")

    default: LimitConfig

    # memory: buckets inside this process. Fine for a single process.
    # redis:  buckets shared by every gateway process and replica.
    backend: Literal["memory", "redis"] = "memory"
    redis_url: str | None = None
    redis_timeout_seconds: float = Field(0.25, gt=0)
    # If Redis is down: true lets requests through (availability first),
    # false rejects them with 429 (protection first).
    fail_open: bool = True

    @model_validator(mode="after")
    def check_redis(self) -> "RateLimitConfig":
        if self.backend == "redis":
            if not self.redis_url:
                raise ValueError("backend 'redis' needs redis_url")
            if not self.redis_url.startswith(("redis://", "rediss://", "unix://")):
                raise ValueError("redis_url must start with redis://, rediss:// or unix://")
        return self


class RetryConfig(BaseModel):
    """If present, failed upstream calls are retried (see app/resilience/retry.py)."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(3, ge=1, le=10)  # total, including the first try
    base_delay_seconds: float = Field(0.05, ge=0)
    max_delay_seconds: float = Field(1.0, ge=0)
    # Retry budget, per upstream: retries <= ratio * requests in the last
    # 10 s, plus a floor of min_per_second * 10 s.
    budget_ratio: float = Field(0.2, ge=0)
    budget_min_per_second: float = Field(1.0, ge=0)

    @model_validator(mode="after")
    def check_delays(self) -> "RetryConfig":
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        return self


class CircuitBreakerConfig(BaseModel):
    """If present, each upstream gets a circuit breaker."""

    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(5, ge=1)  # failures in a row before opening
    recovery_timeout_seconds: float = Field(30.0, gt=0)  # open this long, then probe


class GatewayConfig(BaseModel):
    """Top-level config. New features (auth keys, rate limits) get fields here."""

    model_config = ConfigDict(extra="forbid")

    routes: list[RouteConfig] = Field(..., min_length=1)
    upstream_timeout_seconds: float = Field(5.0, gt=0)
    # Request bodies up to this size are read into memory, so they can be
    # sent again on a retry. Bigger ones are streamed: constant memory, but
    # no retries. 1 MiB by default.
    max_buffered_request_bytes: int = Field(1024 * 1024, ge=0)
    auth: AuthConfig | None = None  # None: auth off, every request allowed
    rate_limit: RateLimitConfig | None = None  # None: no rate limiting
    retries: RetryConfig | None = None  # None: one attempt, no retries
    circuit_breaker: CircuitBreakerConfig | None = None  # None: no breakers

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

    @model_validator(mode="after")
    def check_key_routes_exist(self) -> "GatewayConfig":
        # A key allowed on '/aip' (typo) would silently grant nothing.
        if self.auth is not None:
            known = {r.path_prefix for r in self.routes}
            for entry in self.auth.keys:
                unknown = sorted(set(entry.routes) - known)
                if unknown:
                    raise ValueError(f"client {entry.client!r} lists unknown routes: {unknown}")
        return self
