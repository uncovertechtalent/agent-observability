#!/usr/bin/env bash
# Checks the alerting setup without sending anything. Run from the repo root; needs docker.
#
#   scripts/check-alerting.sh [extra rule files...]
#
# 1. promtool: the Prometheus config with its rule files, and every rule test in prometheus/tests.
# 2. amtool: the Alertmanager config, then the routing tree, case by case.
# 3. Every alert rule in prometheus/rules (and in the extra rule files, for example the NAS
#    Prometheus's) has a known severity and routes to the receivers that severity promises.
# 4. The unit tests of the ticket receiver and of the pager, and the pager image imports its code.
set -euo pipefail

PROM_IMAGE=prom/prometheus:v3.5.0
AM_IMAGE=prom/alertmanager:v0.34.1
PY_IMAGE=python:3.12-slim
fail=0

promtool() { docker run --rm -v "$PWD/prometheus:/p:ro" -w /p --entrypoint promtool "$PROM_IMAGE" "$@"; }
amtool() { docker run --rm -v "$PWD/alertmanager:/a:ro" -w /a --entrypoint amtool "$AM_IMAGE" "$@"; }

echo "== promtool"
promtool check config prometheus.yml
for t in prometheus/tests/*_test.yml; do
  promtool test rules "tests/$(basename "$t")"
done

echo "== amtool"
amtool check-config alertmanager.yml

# route <expected receivers> <label=value...>
route() {
  local want=$1; shift
  local got
  got=$(amtool config routes test --config.file=alertmanager.yml "$@")
  if [ "$got" = "$want" ]; then
    echo "ok   $* -> $got"
  else
    echo "FAIL $* -> $got (want $want)"; fail=1
  fi
}

route pager,board alertname=OllamaDown severity=page prometheus=agent-observability
route pager,board alertname=UCGMaxSomethingNew severity=page prometheus=nas
route alerting-path alertname=PagerDown severity=page prometheus=agent-observability
route alerting-path alertname=PagerDisconnected severity=page prometheus=agent-observability
route alerting-path alertname=PagerFailing severity=page prometheus=agent-observability
route alerting-path alertname=AlertmanagerNotificationsFailing severity=ticket prometheus=agent-observability
route board alertname=SearXNGSlowSearches severity=ticket prometheus=agent-observability
route board alertname=UCGMaxPollerScrapeFailing severity=ticket prometheus=nas
route blackhole alertname=ModelSpilledToCPU severity=info prometheus=agent-observability
route blackhole alertname=AWSCostDailySpike severity=ticket prometheus=agent-observability
route board alertname=AWSCostExporterStale severity=ticket prometheus=agent-observability
route heartbeat-agent-observability alertname=Watchdog severity=none prometheus=agent-observability
route heartbeat-nas alertname=Watchdog severity=none prometheus=nas
route blackhole alertname=Watchdog severity=none
route board alertname=SomethingNew   # no severity: the default receiver, a ticket

echo "== every rule routes by its severity"
# Prints "<alertname> <severity>" per rule; works for "labels: {severity: x}" and the block form.
rules() {
  awk '
    /^[[:space:]]*- alert:/ { if (name != "") print name, sev; name = $3; sev = "" ; next }
    name != "" && sev == "" && match($0, /severity: *[a-z]+/) {
      s = substr($0, RSTART, RLENGTH); sub(/severity: */, "", s); sev = s
    }
    END { if (name != "") print name, sev }
  ' "$@"
}
count=0
while read -r name sev; do
  count=$((count + 1))
  case "$name:$sev" in
    Watchdog:none) want=heartbeat-agent-observability; extra=prometheus=agent-observability ;;
    AWSCostForecastOverBudget:*|AWSCostMonthToDateOver80:*|AWSCostDailySpike:*) want=blackhole; extra= ;;
    PagerDown:*|PagerDisconnected:*|PagerFailing:*|AlertmanagerNotificationsFailing:*) want=alerting-path; extra= ;;
    *:page)        want=pager,board; extra= ;;
    *:ticket)      want=board; extra= ;;
    *:info)        want=blackhole; extra= ;;
    *) echo "FAIL $name has severity '$sev'; use page, ticket or info"; fail=1; continue ;;
  esac
  route "$want" "alertname=$name" "severity=$sev" $extra
done < <(rules prometheus/rules/*.yml "$@")
echo "$count rules checked"

echo "== alert-issues unit tests"
docker run --rm -v "$PWD/alert-issues:/app:ro" -w /app -e PYTHONDONTWRITEBYTECODE=1 "$PY_IMAGE" \
  python -m unittest

echo "== pager unit tests (stdlib only) and the image's imports"
docker run --rm -v "$PWD/pager:/app:ro" -w /app -e PYTHONDONTWRITEBYTECODE=1 "$PY_IMAGE" \
  python -m unittest test_core
pager_image=$(docker build -q pager)
docker run --rm --entrypoint python "$pager_image" -c "import bot, core, diagnose; print('pager image imports ok')"
docker run --rm -v "$PWD/pager:/src:ro" -w /src -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python "$pager_image" \
  -m unittest test_bot
docker run --rm -v "$PWD/pager:/etc/pager:ro" --entrypoint python "$pager_image" -c \
  "import core, yaml; r = core.Rotation(yaml.safe_load(open('/etc/pager/rotation.yml'))); print('rotation.yml ok:', ', '.join(r.schedules))"

[ "$fail" = 0 ] && echo "== all checks passed" || { echo "== checks FAILED"; exit 1; }
