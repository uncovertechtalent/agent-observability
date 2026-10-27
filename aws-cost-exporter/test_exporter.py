"""Offline test of the exporter's metrics on a made-up cache, in both modes. No AWS, no boto3.

    python3 aws-cost-exporter/test_exporter.py

The exporter's clock is pinned with TODAY, so the fixture and every assertion are the same on
any day the test runs. The report mode runs against a fake S3 client that serves a gzip CSV in
the shape of the cost and usage report.
"""

import csv
import gzip
import io
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["CACHE_FILE"] = os.path.join(tempfile.mkdtemp(), "cache.json")
os.environ["TODAY"] = "2026-10-27"
os.environ.pop("CUR_BUCKET", None)
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


# ---------------------------------------------------------------- the report fixture

COLUMNS = ["identity/LineItemId", "lineItem/UsageStartDate", "lineItem/LineItemType", "lineItem/ProductCode", "product/ProductName",
           "lineItem/UsageType", "lineItem/ResourceId", "lineItem/UnblendedCost", "lineItem/CurrencyCode", "resourceTags/user:CostCenter"]


def line(d, line_type, code, name, usage, rid, cost, tag=""):
    return {"identity/LineItemId": f"{d}-{code}-{usage}-{rid}", "lineItem/UsageStartDate": f"{d}T00:00:00Z", "lineItem/LineItemType": line_type,
            "lineItem/ProductCode": code, "product/ProductName": name, "lineItem/UsageType": usage, "lineItem/ResourceId": rid,
            "lineItem/UnblendedCost": f"{cost:.10f}", "lineItem/CurrencyCode": "USD", "resourceTags/user:CostCenter": tag}


def report_lines(today):
    """October 2026 as the report would list it: per day an instance (USD 1 of hours, USD 0.5 of data transfer
    out, both under AmazonEC2), two S3 buckets (0.15 and 0.05), a Bedrock model tagged model-evals through the
    cost allocation tag (0.1), the tax line on the 1st (9.5) and a credit (-2) on the 2nd; the delivery day
    (today) carries half a day."""
    out, d = [], exporter.month_start(today)
    while d <= today:
        f = 0.5 if d == today else 1.0
        s = day(d)
        out += [line(s, "Usage", "AmazonEC2", "Amazon Elastic Compute Cloud", "EUC1-BoxUsage:t3.small", "i-0abc", 1.0 * f),
                line(s, "Usage", "AmazonEC2", "Amazon Elastic Compute Cloud", "EUC1-DataTransfer-Out-Bytes", "i-0abc", 0.5 * f),
                line(s, "Usage", "AmazonS3", "Amazon Simple Storage Service", "EUC1-TimedStorage-ByteHrs", "backup-bucket", 0.15 * f),
                line(s, "Usage", "AmazonS3", "Amazon Simple Storage Service", "EUC1-TimedStorage-ByteHrs", "site-bucket", 0.05 * f),
                line(s, "Usage", "AmazonBedrock", "Amazon Bedrock", "EUC1-Claude-Output-Tokens",
                     "arn:aws:bedrock:eu-central-1::foundation-model/anthropic.claude", 0.1 * f, tag="model-evals")]
        if d.day == 1:
            out.append(line(s, "Tax", "AmazonEC2", "Amazon Elastic Compute Cloud", "", "", 9.5))
        if d.day == 2:
            out.append(line(s, "Credit", "AmazonEC2", "Amazon Elastic Compute Cloud", "EUC1-BoxUsage:t3.small", "", -2.0))
        d += timedelta(days=1)
    return out


def to_csv_gz(lines):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(lines)
    return gzip.compress(buf.getvalue().encode())


class FakeS3:
    """list_objects_v2 / head_object / get_object over a dict of key -> (bytes, etag, last modified)."""

    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def list_objects_v2(self, Bucket, Prefix, Delimiter, ContinuationToken=None):
        self.calls.append("list")
        folders = sorted({k[len(Prefix):].split(Delimiter)[0] for k in self.objects if k.startswith(Prefix)})
        return {"CommonPrefixes": [{"Prefix": f"{Prefix}{f}{Delimiter}"} for f in folders]}

    def head_object(self, Bucket, Key):
        self.calls.append(("head", Key))
        if Key not in self.objects:
            raise Exception("An error occurred (404) when calling the HeadObject operation: Not Found")
        _, etag, mod = self.objects[Key]
        return {"ETag": etag, "LastModified": mod}

    def get_object(self, Bucket, Key):
        self.calls.append(("get", Key))
        body, _, _ = self.objects[Key]
        return {"Body": io.BytesIO(body)}


def report_bucket(today, delivered, lines=None):
    base = "finops/finops/20261001-20261101/"
    data_key = base + "finops-00001.csv.gz"
    manifest = {"assemblyId": "x", "reportKeys": [data_key], "reportName": "finops", "compression": "GZIP"}
    data = to_csv_gz(report_lines(today) if lines is None else lines)
    return {base + "finops-Manifest.json": (json.dumps(manifest).encode(), '"etag-1"', delivered),
            data_key: (data, '"d1"', delivered),
            "finops/finops/20260901-20261001/": (b"", '"folder"', delivered)}  # a period folder with no manifest yet


def main():
    today = date(2026, 10, 27)
    assert exporter.utc_today() == today
    prior_month = exporter.month_start(today, 1)

    # the shipped config files pass the start-up check; a bad pair is refused with one message per problem
    assert exporter.validate() == [], exporter.validate()
    bad_alloc = {"cost_centers": [{"name": "a"}], "cost_center_tag": 3,
                 "rules": [{"cost_center": "b", "services": ["x"]}, {"cost_center": "a"}]}
    bad_fixed = {"items": [{"item": "seat", "cost_center": "zzz", "amount_usd": "12"}]}
    problems = exporter.validate(bad_alloc, bad_fixed)
    assert len(problems) == 5 and all("rule" in p or "item" in p or "cost_center_tag" in p for p in problems), problems
    assert exporter.validate({"cost_centers": [{"name": "a"}], "rules": [{"cost_center": "a", "resource_ids": ["i-*"]}]}, {"items": []}) == []
    # a config path that is a directory (what Docker makes of a mount of a missing file) is reported, not raised
    assert exporter.load_json(tempfile.mkdtemp(), {"items": []}) == {"items": []}
    assert len(exporter.CONFIG_ERRORS) == 1 and "IsADirectoryError" in exporter.CONFIG_ERRORS[0], exporter.CONFIG_ERRORS
    assert exporter.validate()[0] == exporter.CONFIG_ERRORS[0]
    exporter.CONFIG_ERRORS.clear()

    # ------------------------------------------------------------ Cost Explorer mode
    exporter.cache = build_cache(today)
    metrics = parse(exporter.render())
    n = 26  # complete days of October 2026 before the 27th
    mtd = PER_DAY * n + PER_DAY / 2
    assert get(metrics, "aws_cost_source_info", source="cost_explorer") == 1

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
    assert get(metrics, "aws_cost_forecast_info", method="cost_explorer") == 1
    assert not rows(metrics, "aws_cost_mtd_by_resource_usd") and not rows(metrics, "aws_cost_report_line_items")

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
    ce_samples, ce_metrics = len(metrics), len({k[0] for k in metrics})

    # ------------------------------------------------------------ report mode
    # the report's names: tax is its own service, EC2 splits as Cost Explorer does, ARNs lose their prefix
    assert exporter.ce_service("AmazonEC2", "Amazon Elastic Compute Cloud", "EUC1-BoxUsage:t3.small", "Usage") == "Amazon Elastic Compute Cloud - Compute"
    assert exporter.ce_service("AmazonEC2", "Amazon Elastic Compute Cloud", "EUC1-DataTransfer-Out-Bytes", "Usage") == "EC2 - Other"
    assert exporter.ce_service("AmazonEC2", "Amazon Elastic Compute Cloud", "", "Tax") == "Tax"
    assert exporter.ce_service("AmazonS3", "Amazon Simple Storage Service", "x", "Usage") == "Amazon Simple Storage Service"
    assert exporter.short_resource("arn:aws:cloudfront::123456789012:distribution/E1ABC") == "distribution/E1ABC"
    assert exporter.short_resource("arn:aws:route53:::hostedzone/Z1") == "hostedzone/Z1"
    assert exporter.short_resource("i-0abc") == "i-0abc" and exporter.short_resource("") == ""

    exporter.CUR_BUCKET = "bucket"
    delivered = datetime(2026, 10, 27, 19, 22, tzinfo=timezone.utc)  # after noon: only the delivery day may change
    assert exporter.cur_cutoff(delivered.timestamp()) == today
    assert exporter.cur_cutoff(datetime(2026, 10, 27, 2, 39, tzinfo=timezone.utc).timestamp()) == today - timedelta(days=1)
    s3 = FakeS3(report_bucket(today, delivered))
    exporter.cache = build_cache(today)  # Cost Explorer's September stays as the history; its October is replaced
    exporter.fetch_cur(s3, now=1000.0, today=today)
    part = exporter.cache["cur"]
    assert set(part["periods"]) == {"2026-10"}, part["periods"].keys()  # the September folder has no manifest yet
    oct_ = part["periods"]["2026-10"]
    assert oct_["rows"] == 27 * 5 + 2 and oct_["etag"] == '"etag-1"' and oct_["delivered"] == delivered.timestamp()
    assert oct_["tags"] == {"foundation-model/anthropic.claude": "model-evals"}
    assert oct_["records"]["2026-10"]["Tax"] == 9.5 and oct_["records"]["2026-10"]["Credit"] == -2.0
    assert exporter.cache["requests"]["s3:GetObject"] == 2 and exporter.cache["requests"]["s3:HeadObject"] == 2
    # an unchanged manifest is not read again; a new ETag is
    exporter.fetch_cur(s3, now=2000.0, today=today)
    assert exporter.cache["requests"]["s3:GetObject"] == 2
    s3.objects["finops/finops/20261001-20261101/finops-Manifest.json"] = (
        s3.objects["finops/finops/20261001-20261101/finops-Manifest.json"][0], '"etag-2"', delivered)
    exporter.fetch_cur(s3, now=3000.0, today=today)
    assert exporter.cache["requests"]["s3:GetObject"] == 4 and exporter.cache["cur"]["periods"]["2026-10"]["etag"] == '"etag-2"'

    r = parse(exporter.render())
    assert get(r, "aws_cost_source_info", source="cur") == 1
    assert get(r, "aws_cost_report_line_items", period="2026-10") == 137
    assert get(r, "aws_cost_report_delivery_timestamp_seconds", period="2026-10") == round(delivered.timestamp())
    per_day = 1.0 + 0.5 + 0.15 + 0.05 + 0.1
    assert abs(get(r, "aws_cost_mtd_usd") - (per_day * 26 + per_day / 2 + 9.5 - 2.0)) < 1e-6, get(r, "aws_cost_mtd_usd")
    assert get(r, "aws_cost_month_total_usd", month="2026-09") == PER_DAY * 30 + 12.0  # Cost Explorer's September kept
    assert get(r, "aws_cost_day_usd", day="2026-10-05", service="EC2 - Other") == 0.5
    assert get(r, "aws_cost_day_usd", day="2026-10-01", service="Tax") == 9.5
    assert get(r, "aws_cost_month_record_usd", month="2026-10", record_type="Credit") == -2.0
    assert get(r, "aws_cost_month_record_usd", month="2026-09", record_type="Tax") == 9.5
    # the delivery day is estimated and left out of the complete days; the latest complete day is the 26th
    assert get(r, "aws_cost_day_total_usd", day="2026-10-27", estimated="true") == per_day / 2
    assert get(r, "aws_cost_day_total_usd", day="2026-10-26", estimated="false") == per_day
    assert get(r, "aws_cost_latest_day_info", day="2026-10-26") == 1
    assert get(r, "aws_cost_latest_day_estimated") == 0
    # cost centers: the tag wins for the Bedrock model, the rules take the rest, the credit's usage type is compute
    cc = {x["cost_center"]: v for x, v in rows(r, "aws_cost_mtd_by_cost_center_usd")}
    assert abs(cc["model-evals"] - (0.1 * 26 + 0.05)) < 1e-6, cc
    assert abs(cc["compute"] - (1.0 * 26 + 0.5 - 2.0)) < 1e-6, cc
    assert abs(cc["tax"] - 9.5) < 1e-6 and "unallocated" not in cc, cc
    # resources: per (service, resource, cost center); the instance appears under compute and data-transfer
    assert abs(get(r, "aws_cost_mtd_by_resource_usd", service="Amazon Elastic Compute Cloud - Compute", resource_id="i-0abc", cost_center="compute") - 26.5) < 1e-6
    assert abs(get(r, "aws_cost_mtd_by_resource_usd", service="EC2 - Other", resource_id="i-0abc", cost_center="data-transfer") - 13.25) < 1e-6
    assert abs(get(r, "aws_cost_mtd_by_resource_usd", service="Amazon Bedrock", resource_id="foundation-model/anthropic.claude", cost_center="model-evals") - 2.65) < 1e-6
    assert get(r, "aws_cost_mtd_by_resource_usd", service="Tax", resource_id="(none)", cost_center="tax") == 9.5
    assert get(r, "aws_cost_resources", period="this_month") == 7, rows(r, "aws_cost_resources")
    assert get(r, "aws_cost_mtd_resource_rest_usd") == 0  # everything fits under RESOURCE_LIMIT
    assert get(r, "aws_cost_day_by_resource_usd", day="2026-10-10", service="Amazon Simple Storage Service", resource_id="site-bucket") == 0.05
    assert get(r, "aws_cost_resource_latest_day_usd", service="Amazon Simple Storage Service", resource_id="backup-bucket") == 0.15
    assert get(r, "aws_cost_resource_median_day_usd", service="Amazon Simple Storage Service", resource_id="backup-bucket") == 0.15
    assert not [x for x, _ in rows(r, "aws_cost_resource_latest_day_usd") if x["service"] == "Tax"]
    # the forecast is the run rate of the complete days
    done = per_day * 26 + 9.5 - 2.0  # days 1 to 26, with the tax and the credit
    assert get(r, "aws_cost_forecast_info", method="run_rate") == 1
    assert abs(get(r, "aws_cost_month_forecast_usd") - (done + done / 26 * 5)) < 1e-6, get(r, "aws_cost_month_forecast_usd")
    assert get(r, "aws_cost_exporter_fetch_timestamp_seconds", part="cur") == 3000.0
    # a rule on resource ids: the site bucket moves to its own cost center
    exporter.ALLOCATION["rules"].insert(0, {"cost_center": "sites", "services": ["Amazon Simple Storage Service"], "resource_ids": ["site-*"]})
    exporter._centers.clear()
    r2 = parse(exporter.render())
    assert abs(get(r2, "aws_cost_mtd_by_cost_center_usd", cost_center="sites") - (0.05 * 26 + 0.025)) < 1e-6
    assert abs(get(r2, "aws_cost_mtd_by_cost_center_usd", cost_center="storage-and-backup") - (0.15 * 26 + 0.075)) < 1e-6
    exporter.ALLOCATION["rules"].pop(0)
    exporter._centers.clear()
    # the top-N cap: with room for one resource, the rest is summed and the instance (the costliest) stays
    exporter.RESOURCE_LIMIT = 1
    r3 = parse(exporter.render())
    assert [x["resource_id"] for x, _ in rows(r3, "aws_cost_mtd_by_resource_usd")] == ["i-0abc"]
    assert abs(get(r3, "aws_cost_mtd_resource_rest_usd") - (13.25 + 0.15 * 26.5 + 0.05 * 26.5 + 2.65 + 9.5 - 2.0)) < 1e-6
    exporter.RESOURCE_LIMIT = 40

    # the poll in report mode: no Cost Explorer call while the report covers this month and the history is cached
    class NoCE:
        def __getattr__(self, name):
            raise AssertionError(f"Cost Explorer called: {name}")

    class Budgets:
        def describe_budgets(self, **kw):
            return {"Budgets": []}

    exporter.fetch(NoCE(), Budgets(), "123", s3)
    assert exporter.cache["budgets"]["items"] == []
    # without a file for this month yet, this month comes from Cost Explorer; nothing else does
    calls = []

    class CE:
        class exceptions:
            class DataUnavailableException(Exception):
                pass

        def get_cost_and_usage(self, **kw):
            calls.append(kw["GroupBy"][0]["Key"] + ":" + kw["TimePeriod"]["Start"])
            return {"ResultsByTime": []}

        def get_cost_forecast(self, **kw):
            raise AssertionError("forecast asked in report mode")

    exporter.cache["cur"]["periods"] = {}
    exporter.cache["current"]["fetched"] = 0
    exporter.cache["cur"]["fetched"] = 1e12  # not due
    exporter.fetch(CE(), Budgets(), "123", s3)
    assert calls == ["SERVICE:2026-10-01"], calls
    exporter.CUR_BUCKET = ""
    print(f"ok: {ce_samples} samples, {ce_metrics} metrics in Cost Explorer mode; {len(r)} samples, {len({k[0] for k in r})} metrics in report mode")


if __name__ == "__main__":
    main()
