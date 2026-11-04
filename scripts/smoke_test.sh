#!/usr/bin/env bash
# End-to-end check of the running stack: gateway, both upstreams, Redis,
# Prometheus and Grafana, each through the path a real user would take.
#
#   docker compose up -d --build --wait
#   ./scripts/smoke_test.sh
#
# Exit code 0 = everything works. CI runs this on every push.
#
# Settings (environment variables):
#   GATEWAY   gateway URL                  default http://localhost:8000
#   PROM      Prometheus URL               default http://localhost:9090
#   GRAFANA   Grafana URL ("" = skip)      default http://localhost:3000
#   API_KEY   a key the config accepts     default the public dev key
#   METRICS   metrics URL that must NOT be reachable from outside
#             ("" = skip)                  default http://localhost:9100/metrics

set -uo pipefail

GATEWAY=${GATEWAY-http://localhost:8000}
PROM=${PROM-http://localhost:9090}
GRAFANA=${GRAFANA-http://localhost:3000}
API_KEY=${API_KEY-gw_dev_local_only}
METRICS=${METRICS-http://localhost:9100/metrics}
GRAFANA_AUTH=${GRAFANA_AUTH-admin:admin}

failures=0
pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }

# check NAME EXPECTED_STATUS URL [curl args...]
check() {
  local name=$1 want=$2 url=$3; shift 3
  local got
  got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" "$url")
  if [ "$got" = "$want" ]; then pass "$name ($got)"; else fail "$name: want $want, got $got"; fi
}

# body_has NAME PATTERN URL [curl args...]
# PATTERN is an extended regex, matched case-insensitively. JSON patterns
# allow a space after the colon, since some APIs pretty-print.
body_has() {
  local name=$1 text=$2 url=$3; shift 3
  if curl -s --max-time 10 "$@" "$url" | grep -qiE -- "$text"; then
    pass "$name"
  else
    fail "$name: response from $url has no '$text'"
  fi
}

# eventually NAME TEXT URL [curl args...]: like body_has, retried for 60 s.
# For things that settle shortly after startup, like Prometheus's first scrape.
eventually() {
  local name=$1 text=$2 url=$3; shift 3
  for _ in $(seq 30); do
    if curl -s --max-time 5 "$@" "$url" | grep -qiE -- "$text"; then
      pass "$name"
      return
    fi
    sleep 2
  done
  fail "$name: no '$text' from $url after 60 s"
}

KEY=(-H "X-API-Key: $API_KEY")

echo "gateway  $GATEWAY"
check    "liveness /health"                 200 "$GATEWAY/health"
body_has "readiness /ready, Redis ok"       '"rate_limit_redis": ?"ok"' "$GATEWAY/ready"
check    "/api -> dummy upstream"           200 "$GATEWAY/api/items/1" "${KEY[@]}"
body_has "/cpp -> your C++ server"          "Dope Server Right\?" "$GATEWAY/cpp/" "${KEY[@]}"
check    "/cpp/about -> C++ server"         200 "$GATEWAY/cpp/about" "${KEY[@]}"
check    "C++ 404 passed through"           404 "$GATEWAY/cpp/no-such-page" "${KEY[@]}"
check    "no API key -> 401"                401 "$GATEWAY/api/items/1"
check    "wrong API key -> 401"             401 "$GATEWAY/api/items/1" -H "X-API-Key: wrong"
check    "unknown route -> 404"             404 "$GATEWAY/nothing-here" "${KEY[@]}"
body_has "X-Request-ID on responses"        "^x-request-id:" "$GATEWAY/api/items/1" -i "${KEY[@]}"
body_has "rate-limit headers (Redis)"       "^x-ratelimit-remaining:" "$GATEWAY/api/items/1" -i "${KEY[@]}"
if [ -n "$METRICS" ]; then
  if curl -s -o /dev/null --max-time 3 "$METRICS"; then
    fail "metrics port must not be reachable from outside: $METRICS answered"
  else
    pass "metrics port not published"
  fi
fi

echo "prometheus  $PROM"
eventually "gateway target is up" '"health": ?"up"' "$PROM/api/v1/targets?state=active"
# The /cpp requests above, counted by the gateway and scraped by Prometheus.
eventually "gateway metrics scraped" '"route": ?"/cpp"' \
  "$PROM/api/v1/query" -G --data-urlencode 'query=gateway_requests_total{route="/cpp"}'
body_has "alert rules loaded"               '"name": ?"UpstreamCircuitOpen"' "$PROM/api/v1/rules"

if [ -n "$GRAFANA" ]; then
  G=(-u "$GRAFANA_AUTH")
  echo "grafana  $GRAFANA"
  eventually "grafana is up"                '"database"' "$GRAFANA/api/health"
  body_has "datasource provisioned"         '"uid": ?"prometheus"' "$GRAFANA/api/datasources/uid/prometheus" "${G[@]}"
  # Grafana itself queries Prometheus: proves the datasource URL is right.
  body_has "datasource reaches Prometheus"  '"status": ?"OK"' "$GRAFANA/api/datasources/uid/prometheus/health" "${G[@]}"
  body_has "dashboard provisioned"          '"title": ?"API Gateway"' "$GRAFANA/api/dashboards/uid/gateway-overview" "${G[@]}"
  # A dashboard query, run by Grafana against Prometheus.
  eventually "dashboard data via Grafana"   '"route": ?"/cpp"' \
    "$GRAFANA/api/datasources/proxy/uid/prometheus/api/v1/query" "${G[@]}" \
    -G --data-urlencode 'query=sum by (route) (gateway_requests_total)'
fi

echo
if [ "$failures" -eq 0 ]; then
  echo "all checks passed"
else
  echo "$failures check(s) failed"
  exit 1
fi
