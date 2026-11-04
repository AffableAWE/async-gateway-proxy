"""The docker-compose stack's config files agree with the code and each other.

These files are only read inside containers, so a mistake in them would
otherwise show up as a gateway that won't start, or a dashboard panel that
is quietly blank. Here it's a failing test instead.
"""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from prometheus_client.metrics_core import Metric

from app.config import load_config
from app.metrics import GatewayMetrics

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"


def compose_services() -> set[str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return set(compose["services"])


def dashboard() -> dict:
    return json.loads((DEPLOY / "grafana/dashboards/gateway.json").read_text())


def dashboard_queries() -> list[str]:
    return [t["expr"] for p in dashboard()["panels"] for t in p.get("targets", [])]


def alert_queries() -> list[str]:
    rules = yaml.safe_load((DEPLOY / "prometheus/alerts.yml").read_text())
    return [r["expr"] for g in rules["groups"] for r in g["rules"]]


def exported_metric_names() -> set[str]:
    """Every series name the gateway can export, including the _total,
    _bucket, _sum and _count suffixes Prometheus actually stores."""
    metrics = GatewayMetrics()
    metrics.watch_breakers(lambda: {})
    names = set()
    for family in metrics.registry.collect():
        family: Metric
        if family.type == "counter":
            names.add(family.name + "_total")
        elif family.type == "histogram":
            names.update(family.name + s for s in ("_bucket", "_sum", "_count"))
        else:
            names.add(family.name)
    return names


def names_in(query: str) -> set[str]:
    return set(re.findall(r"\bgateway_[a-z_]+", query))


def test_the_compose_config_is_valid():
    config = load_config(DEPLOY / "gateway/config.yaml")
    assert {r.path_prefix for r in config.routes} == {"/api", "/cpp"}
    assert config.rate_limit.backend == "redis"


def test_every_upstream_and_redis_is_a_compose_service():
    config = load_config(DEPLOY / "gateway/config.yaml")
    services = compose_services()
    for route in config.routes:
        assert urlsplit(route.upstream).hostname in services, route.upstream
    assert urlsplit(config.rate_limit.redis_url).hostname in services


def test_every_key_can_reach_only_configured_routes():
    config = load_config(DEPLOY / "gateway/config.yaml")
    prefixes = {r.path_prefix for r in config.routes}
    for key in config.auth.keys:
        assert set(key.routes) <= prefixes, key.client


@pytest.mark.parametrize("source", ["dashboard", "alerts"])
def test_queries_only_use_metrics_the_gateway_exports(source):
    # Rename a metric in app/metrics.py and forget the dashboard, and this
    # fails, instead of a panel showing "No data" forever.
    queries = dashboard_queries() if source == "dashboard" else alert_queries()
    exported = exported_metric_names()
    used = set().union(*(names_in(q) for q in queries))
    assert used, "no gateway_ metrics found in the queries"
    assert used <= exported, f"not exported by the gateway: {sorted(used - exported)}"


def test_dashboard_uses_the_provisioned_datasource():
    provisioned = yaml.safe_load(
        (DEPLOY / "grafana/provisioning/datasources/prometheus.yml").read_text()
    )
    uids = {d["uid"] for d in provisioned["datasources"]}
    for panel in dashboard()["panels"]:
        if panel["type"] == "row":
            continue
        assert panel["datasource"]["uid"] in uids, panel["title"]
        for target in panel["targets"]:
            assert target["datasource"]["uid"] in uids, panel["title"]


def test_dashboard_panel_ids_are_unique():
    ids = [p["id"] for p in dashboard()["panels"]]
    assert len(ids) == len(set(ids))


def test_prometheus_scrapes_the_gateway_metrics_port():
    prom = yaml.safe_load((DEPLOY / "prometheus/prometheus.yml").read_text())
    job = next(j for j in prom["scrape_configs"] if j["job_name"] == "gateway")
    (target,) = job["static_configs"][0]["targets"]
    host, port = target.split(":")
    assert host in compose_services()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert f"GATEWAY_METRICS_PORT={port}" in dockerfile
