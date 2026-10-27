"""Offline test of the exporter's metrics on a made-up cache. No AWS, no boto3.

    python3 aws-cost-exporter/test_exporter.py
"""

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["CACHE_FILE"] = os.path.join(tempfile.mkdtemp(), "cache.json")
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


def main():
    today = datetime.now(timezone.utc).date()
    this_month = exporter.month_start(today)
    prior_month = exporter.month_start(today, 1)
    # two months of days: USD 1 of EC2 compute, USD 0.5 of data transfer under EC2 - Other, USD 0.2 of S3 each day;
    # a USD 12 domain renewal on the 3rd of the prior month; today is partial (half a day)
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
    exporter.cache = {
        "requests": {"ce:GetCostAndUsage": 10},
        "errors": 0,
        "current": {"v": 2, "month": day(this_month), "fetched": 1, "days": cur_days, "estimated": [day(today)], "unit": "USD"},
        "history": {"v": 2, "start": day(prior_month), "end": day(this_month), "fetched": 1, "days": hist_days, "estimated": [], "unit": "USD"},
        "records": {"start": day(prior_month), "fetched": 1, "months": {day(prior_month)[:7]: {"Usage": 50.0, "Tax": 9.5}}},
        "forecast": {"fetched": 1, "amount": 20.0, "start": day(today), "end": day(exporter.next_month(today))},
        "budgets": {"fetched": 1, "items": [{"name": "Entire budget", "type": "COST", "period": "MONTHLY", "limit": 100.0,
                                             "unit": "USD", "actual": 10.0, "forecast": 30.0}]},
    }
    metrics = parse(exporter.render())
    per_day = 1.0 + 0.5 + 0.2 + 0.1
    n_complete = today.day - 1
    mtd = per_day * n_complete + per_day / 2

    # the old metrics still add up
    assert abs(get(metrics, "aws_cost_mtd_usd") - mtd) < 1e-6, get(metrics, "aws_cost_mtd_usd")
    assert abs(get(metrics, "aws_cost_day_usd", day=day(today), service="EC2 - Other") - 0.25) < 1e-9

    # virtual tags: data transfer leaves EC2 - Other for its own cost center, the unknown service is unallocated
    assert get(metrics, "aws_cost_day_usage_usd", day=day(today), service="EC2 - Other",
               usage_type="EUC1-DataTransfer-Out-Bytes", cost_center="data-transfer") == 0.25
    cc = {r["cost_center"]: v for r, v in rows(metrics, "aws_cost_mtd_by_cost_center_usd")}
    assert abs(cc["compute"] - (1.0 * n_complete + 0.5)) < 1e-6, cc
    assert abs(cc["data-transfer"] - (0.5 * n_complete + 0.25)) < 1e-6, cc
    assert abs(cc["storage-and-backup"] - (0.2 * n_complete + 0.1)) < 1e-6, cc
    assert abs(cc["unallocated"] - (0.1 * n_complete + 0.05)) < 1e-6, cc
    assert abs(get(metrics, "aws_cost_allocated_ratio") - (1.7 / 1.8)) < 1e-6

    # month to date against the same days of the month before (complete days only)
    assert get(metrics, "aws_cost_to_date_days") == n_complete
    if n_complete:
        assert abs(get(metrics, "aws_cost_to_date_usd", period="this_month") - per_day * n_complete) < 1e-6
        prior = per_day * n_complete + (12.0 if n_complete >= 3 else 0.0)
        assert abs(get(metrics, "aws_cost_to_date_usd", period="prior_month") - prior) < 1e-6
        assert abs(get(metrics, "aws_cost_to_date_by_cost_center_usd", period="prior_month", cost_center="compute") - 1.0 * n_complete) < 1e-6
    else:
        assert not rows(metrics, "aws_cost_to_date_usd")

    # anomalies per service and per cost center: flat series, so latest equals the median
    assert get(metrics, "aws_cost_service_latest_day_usd", service="EC2 - Other") == 0.5
    assert get(metrics, "aws_cost_service_median_day_usd", service="EC2 - Other") == 0.5
    assert ("aws_cost_service_latest_day_usd", (("service", "Amazon Registrar"),)) not in metrics  # SPIKE_EXCLUDE
    assert get(metrics, "aws_cost_cost_center_latest_day_usd", cost_center="compute") == 1.0
    assert get(metrics, "aws_cost_cost_center_median_day_usd", cost_center="unallocated") == 0.1
    assert get(metrics, "aws_cost_trailing_usd", days="7") == per_day * 7
    assert get(metrics, "aws_cost_trailing_by_cost_center_usd", days="7", cost_center="data-transfer") == 0.5 * 7

    # record types, fixed costs, forecast
    assert get(metrics, "aws_cost_month_record_usd", month=day(prior_month)[:7], record_type="Tax") == 9.5
    assert get(metrics, "fixed_cost_items", state="unpriced") == 2  # the shipped file carries no amounts
    assert get(metrics, "fixed_cost_items", state="priced") == 0
    assert not rows(metrics, "fixed_cost_month_usd")
    assert abs(get(metrics, "aws_cost_month_forecast_usd") - (per_day * n_complete + 20.0)) < 1e-6

    # a cache from before version 2 (day -> service -> cost) still renders, as one usage type ""
    exporter.cache["history"]["days"] = {k: {s: sum(us.values()) for s, us in v.items()} for k, v in hist_days.items()}
    old = parse(exporter.render())
    assert abs(get(old, "aws_cost_mtd_usd") - mtd) < 1e-6
    if n_complete >= 3:
        assert get(old, "aws_cost_day_usage_usd", day=day(prior_month + timedelta(days=2)), service="Amazon Registrar",
                   usage_type="", cost_center="domains-and-dns") == 12.0
    print(f"ok: {len(metrics)} samples, {len({k[0] for k in metrics})} metrics")


if __name__ == "__main__":
    main()
