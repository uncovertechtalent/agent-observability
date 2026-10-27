"""Offline test of the exporter's metrics on a made-up cache. No AWS, no boto3.

    python3 aws-cost-exporter/test_exporter.py

The exporter's clock is pinned with TODAY, so the fixture and every assertion are the same on
any day the test runs.
"""

import os
import re
import sys
import tempfile
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["CACHE_FILE"] = os.path.join(tempfile.mkdtemp(), "cache.json")
os.environ["TODAY"] = "2026-10-27"
sys.path.insert(0, HERE)
import exporter  # noqa: E402

LINE = re.compile(r'^([a-z_]+)(\{([^}]*)\})? (\S+)$')


def parse(text):
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line:
            continue
        mt = LINE.match(line)
        assert mt, line
        labels = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', mt.group(3) or ""))
        out[(mt.group(1), tuple(sorted(labels.items())))] = float(mt.group(4))
    return out


def get(metrics, name, **labels):
    return metrics[(name, tuple(sorted(labels.items())))]


def rows(metrics, name):
    return [(dict(k[1]), v) for k, v in metrics.items() if k[0] == name]


def day(d):
    return d.isoformat()


PER_DAY = 1.0 + 0.5 + 0.2 + 0.1


def build_cache(today):
    """Two months of days: USD 1 of EC2 compute, USD 0.5 of data transfer under EC2 - Other, USD 0.2 of S3 and
    USD 0.1 of an unknown service each day; a USD 12 domain renewal on the 3rd of the prior month; today is
    partial (half a day)."""
    this_month, prior_month = exporter.month_start(today), exporter.month_start(today, 1)
    days = {}
    d = prior_month
    while d <= today:
        row = {"Amazon Elastic Compute Cloud - Compute": {"EUC1-BoxUsage:t3.small": 1.0},
               "EC2 - Other": {"EUC1-DataTransfer-Out-Bytes": 0.5},
               "Amazon Simple Storage Service": {"EUC1-TimedStorage-ByteHrs": 0.2},
               "Some New Service": {"EUC1-Mystery": 0.1}}
        if d == prior_month + timedelta(days=2):
            row["Amazon Registrar"] = {"Domain-Renewal": 12.0}
        if d == today:
            row = {s: {u: v / 2 for u, v in us.items()} for s, us in row.items()}
        days[day(d)] = row
        d += timedelta(days=1)
    cur_days = {k: v for k, v in days.items() if k[:7] == day(this_month)[:7]}
    hist_days = {k: v for k, v in days.items() if k[:7] != day(this_month)[:7]}
    return {
        "requests": {"ce:GetCostAndUsage": 10},
        "errors": 0,
        "current": {"v": 2, "month": day(this_month), "fetched": 1, "days": cur_days, "estimated": [day(today)], "unit": "USD"},
        "history": {"v": 2, "start": day(prior_month), "end": day(this_month), "fetched": 1, "days": hist_days, "estimated": [], "unit": "USD"},
        "records": {"start": day(prior_month), "fetched": 1, "months": {day(prior_month)[:7]: {"Usage": 50.0, "Tax": 9.5}}},
        "forecast": {"fetched": 1, "amount": 20.0, "start": day(today), "end": day(exporter.next_month(today))},
        "budgets": {"fetched": 1, "items": [{"name": "Entire budget", "type": "COST", "period": "MONTHLY", "limit": 100.0,
                                             "unit": "USD", "actual": 10.0, "forecast": 30.0}]},
    }


def main():
    today = date(2026, 10, 27)
    assert exporter.utc_today() == today
    prior_month = exporter.month_start(today, 1)

    # the shipped config files pass the start-up check; a bad pair is refused with one message per problem
    assert exporter.validate() == [], exporter.validate()
    bad_alloc = {"cost_centers": [{"name": "a"}], "rules": [{"cost_center": "b", "services": ["x"]}, {"cost_center": "a"}]}
    bad_fixed = {"items": [{"item": "seat", "cost_center": "zzz", "amount_usd": "12"}]}
    problems = exporter.validate(bad_alloc, bad_fixed)
    assert len(problems) == 4 and all("rule" in p or "item" in p for p in problems), problems
    # a config path that is a directory (what Docker makes of a mount of a missing file) is reported, not raised
    assert exporter.load_json(tempfile.mkdtemp(), {"items": []}) == {"items": []}
    assert len(exporter.CONFIG_ERRORS) == 1 and "IsADirectoryError" in exporter.CONFIG_ERRORS[0], exporter.CONFIG_ERRORS
    assert exporter.validate()[0] == exporter.CONFIG_ERRORS[0]
    exporter.CONFIG_ERRORS.clear()

    exporter.cache = build_cache(today)
    metrics = parse(exporter.render())
    n = 26  # complete days of October 2026 before the 27th
    mtd = PER_DAY * n + PER_DAY / 2

    # the old metrics still add up
    assert abs(get(metrics, "aws_cost_mtd_usd") - mtd) < 1e-6, get(metrics, "aws_cost_mtd_usd")
    assert abs(get(metrics, "aws_cost_day_usd", day=day(today), service="EC2 - Other") - 0.25) < 1e-9
    assert get(metrics, "aws_cost_month_total_usd", month="2026-09") == PER_DAY * 30 + 12.0

    # virtual tags: data transfer leaves EC2 - Other for its own cost center, the unknown service is unallocated
    assert get(metrics, "aws_cost_mtd_by_usage_type_usd", service="EC2 - Other", usage_type="EUC1-DataTransfer-Out-Bytes",
               cost_center="data-transfer") == 0.5 * n + 0.25
    cc = {r["cost_center"]: v for r, v in rows(metrics, "aws_cost_mtd_by_cost_center_usd")}
    assert abs(cc["compute"] - (1.0 * n + 0.5)) < 1e-6, cc
    assert abs(cc["data-transfer"] - (0.5 * n + 0.25)) < 1e-6, cc
    assert abs(cc["storage-and-backup"] - (0.2 * n + 0.1)) < 1e-6, cc
    assert abs(cc["unallocated"] - (0.1 * n + 0.05)) < 1e-6, cc
    assert get(metrics, "aws_cost_month_by_cost_center_usd", month="2026-09", cost_center="domains-and-dns") == 12.0
    assert abs(get(metrics, "aws_cost_allocated_ratio") - (1.7 / 1.8)) < 1e-6

    # month to date against the same days of the month before (complete days only)
    assert get(metrics, "aws_cost_to_date_days") == n
    assert abs(get(metrics, "aws_cost_to_date_usd", period="this_month") - PER_DAY * n) < 1e-6
    assert abs(get(metrics, "aws_cost_to_date_usd", period="prior_month") - (PER_DAY * n + 12.0)) < 1e-6
    assert abs(get(metrics, "aws_cost_to_date_by_cost_center_usd", period="prior_month", cost_center="compute") - 1.0 * n) < 1e-6

    # anomalies per service and per cost center: flat series, so latest equals the median
    assert get(metrics, "aws_cost_latest_day_info", day="2026-10-26") == 1
    assert get(metrics, "aws_cost_service_latest_day_usd", service="EC2 - Other") == 0.5
    assert get(metrics, "aws_cost_service_median_day_usd", service="EC2 - Other") == 0.5
    assert ("aws_cost_service_latest_day_usd", (("service", "Amazon Registrar"),)) not in metrics  # SPIKE_EXCLUDE
    assert get(metrics, "aws_cost_cost_center_latest_day_usd", cost_center="compute") == 1.0
    assert get(metrics, "aws_cost_cost_center_median_day_usd", cost_center="unallocated") == 0.1
    assert get(metrics, "aws_cost_trailing_by_cost_center_usd", days="7", cost_center="data-transfer") == 0.5 * 7

    # record types, fixed costs, forecast
    assert get(metrics, "aws_cost_month_record_usd", month="2026-09", record_type="Tax") == 9.5
    assert get(metrics, "fixed_cost_items", state="unpriced") == 2  # the shipped file carries no amounts
    assert get(metrics, "fixed_cost_items", state="priced") == 0
    assert not rows(metrics, "fixed_cost_month_usd")
    assert abs(get(metrics, "aws_cost_month_forecast_usd") - (PER_DAY * n + 20.0)) < 1e-6

    # a cache from before version 2 (day -> service -> cost) still renders, as one usage type ""
    hist = exporter.cache["history"]
    hist["days"] = {k: {s: sum(us.values()) for s, us in v.items()} for k, v in hist["days"].items()}
    old = parse(exporter.render())
    assert abs(get(old, "aws_cost_mtd_usd") - mtd) < 1e-6
    assert get(old, "aws_cost_month_by_cost_center_usd", month="2026-09", cost_center="domains-and-dns") == 12.0

    # the end of March compares at most February's 28 days, on both sides
    os.environ["TODAY"] = "2026-03-30"
    exporter.cache = build_cache(date(2026, 3, 30))
    march = parse(exporter.render())
    assert get(march, "aws_cost_to_date_days") == 28
    assert abs(get(march, "aws_cost_to_date_usd", period="this_month") - PER_DAY * 28) < 1e-6
    assert abs(get(march, "aws_cost_to_date_usd", period="prior_month") - (PER_DAY * 28 + 12.0)) < 1e-6
    os.environ["TODAY"] = "2026-10-27"
    print(f"ok: {len(metrics)} samples, {len({k[0] for k in metrics})} metrics")


if __name__ == "__main__":
    main()
