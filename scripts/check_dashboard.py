"""Check that every query on the Grafana dashboard returns data from Prometheus.

    python scripts/traffic.py --seconds 30   # make some traffic first
    python scripts/check_dashboard.py

A panel can be wrong in ways nothing else notices: a typo in a label, a
metric that was renamed, a query that's valid but matches nothing. Grafana
just shows "No data". This runs each query over the last few minutes and
fails if any of them comes back empty.

Standard library only. Exit code 0 = every query returned data.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "deploy/grafana/dashboards/gateway.json"

# Grafana fills these in before sending a query. Values close to what it
# would pick for a short time range with a 5 s scrape interval.
GRAFANA_VARIABLES = {"$__rate_interval": "1m", "$__interval": "15s", "$__range": "5m"}


def query_range(prom: str, expr: str, minutes: float) -> list:
    end = time.time()
    params = {"query": expr, "start": end - minutes * 60, "end": end, "step": "15"}
    url = f"{prom}/api/v1/query_range?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.load(resp)
    if body.get("status") != "success":
        raise RuntimeError(body.get("error", body))
    return body["data"]["result"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prom", default="http://localhost:9090")
    parser.add_argument("--minutes", type=float, default=5, help="how far back to look")
    args = parser.parse_args()

    dashboard = json.loads(DASHBOARD.read_text())
    empty = 0
    total = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            total += 1
            expr = target["expr"]
            for name, value in GRAFANA_VARIABLES.items():
                expr = expr.replace(name, value)
            try:
                series = query_range(args.prom, expr, args.minutes)
                status = f"{len(series)} series" if series else "NO DATA"
            except Exception as exc:  # a broken query counts as empty
                series, status = [], f"ERROR {exc}"
            if not series:
                empty += 1
            print(f"  {'ok  ' if series else 'FAIL'}  {panel['title'][:40]:40}  {status}")

    print(f"\n{total - empty}/{total} dashboard queries returned data")
    return 1 if empty else 0


if __name__ == "__main__":
    sys.exit(main())
