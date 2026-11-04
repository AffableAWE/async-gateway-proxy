"""Send a mix of traffic through the gateway so every dashboard panel has data.

    python scripts/traffic.py                  # 60 s against localhost:8000
    python scripts/traffic.py --seconds 300 --rps 40

Standard library only, so it runs without the gateway's virtualenv.

The mix, per request:
  - mostly good requests to /api (dummy upstream) and /cpp (your C++ server)
  - some slow and some failing upstream calls (latency, retries, 5xx panels)
  - some 404s from the C++ server
  - some requests with no key or a wrong key (auth panel)
  - every few seconds a short burst over the rate limit (429 panel)
  - halfway through, a short burst of 503s from the dummy upstream: its
    circuit breaker opens, /api fails fast for ~10 s, then recovers
    (breaker panels). --no-outage turns it off.
"""

from __future__ import annotations

import argparse
import random
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

DEV_KEY = "gw_dev_local_only"

# (weight, path, key). key None = no header.
MIX = [
    (30, "/api/items/{n}", DEV_KEY),
    (25, "/cpp/", DEV_KEY),
    (10, "/cpp/about", DEV_KEY),
    (5, "/cpp/nope", DEV_KEY),  # 404 from the C++ server
    (8, "/api/slow?delay=0.3", DEV_KEY),  # latency
    (5, "/api/flaky?key={n}&fail=1", DEV_KEY),  # fails once, the retry succeeds
    (2, "/api/status/500", DEV_KEY),  # upstream 5xx (never retried)
    (3, "/api/items/{n}", None),  # 401
    (2, "/api/items/{n}", "wrong-key"),  # 401
]


def request(base: str, path: str, key: str | None, stats: Counter, lock: threading.Lock) -> None:
    req = urllib.request.Request(base + path.format(n=random.randint(1, 10**9)))
    if key is not None:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        code = exc.code
    except OSError:
        code = "conn-error"
    with lock:
        stats[code] += 1


def summary(stats: Counter) -> str:
    return "  ".join(f"{code}:{n}" for code, n in sorted(stats.items(), key=str))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--rps", type=float, default=20, help="steady requests per second")
    parser.add_argument(
        "--burst-every", type=float, default=10, help="seconds between 429 bursts (0 = off)"
    )
    parser.add_argument(
        "--outage",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="open the /api circuit breaker halfway through",
    )
    args = parser.parse_args()

    weights = [w for w, _, _ in MIX]
    stats: Counter = Counter()
    lock = threading.Lock()
    threads: list[threading.Thread] = []

    def fire(path: str, key: str | None) -> None:
        t = threading.Thread(target=request, args=(args.base, path, key, stats, lock), daemon=True)
        t.start()
        threads.append(t)

    start = time.monotonic()
    next_burst = start + args.burst_every if args.burst_every else float("inf")
    next_report = start + 5
    outage_at = start + args.seconds / 2 if args.outage else float("inf")
    while (now := time.monotonic()) - start < args.seconds:
        _, path, key = random.choices(MIX, weights)[0]
        fire(path, key)
        if now >= next_burst:
            # More than the bucket holds, at once: the tail gets 429s.
            for _ in range(80):
                fire("/api/items/{n}", DEV_KEY)
            next_burst = now + args.burst_every
        if now >= outage_at:
            # Enough upstream failures in a row to open the breaker
            # (threshold 5 in deploy/gateway/config.yaml).
            print(f"{now - start:5.0f}s  simulating an upstream outage on /api")
            for _ in range(10):
                fire("/api/status/503", DEV_KEY)
            outage_at = float("inf")
        if now >= next_report:
            with lock:
                print(f"{now - start:5.0f}s  {summary(stats)}")
            next_report = now + 5
        threads[:] = [t for t in threads if t.is_alive()]
        time.sleep(1 / args.rps)

    for t in threads:
        t.join(timeout=15)
    total = sum(stats.values())
    print(f"done: {total} requests  {summary(stats)}")


if __name__ == "__main__":
    main()
