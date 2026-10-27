"""AWS spend for the stack as Prometheus metrics on :9202, from the cost and usage report in S3
(or from Cost Explorer) and from AWS Budgets.

Source. With CUR_BUCKET set the exporter reads the account's cost and usage report (the legacy
CUR: daily line items with resource ids, CSV gzip, one folder per billing period under
CUR_PREFIX/CUR_REPORT/, overwritten up to three times a day). Every CUR_SECONDS (default 1 h) it
lists the periods, HEADs each manifest and reads the files of a period again only when the
manifest's ETag changed. Reading costs S3 requests only. Cost Explorer (USD 0.01 per request) is
then asked for three things only: this month, while the report has no file for it yet (a new
report delivers within 24 h; a new month within a day); and, once, the history of the months
before the report existed and their record types, so a fresh install has last month to compare
against. Without CUR_BUCKET the exporter works as before: Cost Explorer every POLL_SECONDS for
this month, FORECAST_SECONDS for the forecast, HISTORY_SECONDS for the three months before,
RECORDS_SECONDS for the record types; about 4.3 requests a day, some USD 1.30 a month. AWS
Budgets (no charge) every BUDGETS_SECONDS either way. Every answer is cached in /data/cache.json;
a restart serves the cache and asks only for what is due. Requests are counted per API and kept
in the cache, so aws_cost_explorer_spend_usd_total shows what the exporter itself has cost.

Names. Report lines are given the service names Cost Explorer uses, so the allocation rules and
the dashboards read the same names from both sources: tax lines are the service "Tax", and EC2
splits into "Amazon Elastic Compute Cloud - Compute" (instance hours) and "EC2 - Other" (volumes,
addresses, data transfer). Resource ids lose their ARN prefix (distribution/E1, hostedzone/Z1,
i-0abc, a bucket name). The month-end forecast is Cost Explorer's in Cost Explorer mode and the
run rate of this month's complete days in report mode.

Virtual tags: allocation.json maps every line to a cost center by rule on (service, usage type,
resource id), the way a FinOps tool's virtual tags do, and a line no rule matches is
"unallocated". When the account activates a cost allocation tag named in allocation.json's
cost_center_tag, a report line whose tag value names a listed cost center goes there before any
rule. fixed-costs.json lists monthly costs outside AWS (subscriptions, seats) entered by hand; the
committed file is the example with no amounts, and compose mounts the priced copy
(fixed-costs.local.json, git-ignored) at FIXED_COSTS_FILE when there is one. Both files are read
once, at start, and checked: a rule or an item that names a cost center the allocation file does
not list stops the exporter with a message.

Credentials come from the host's AWS shared config, mounted read-only (AWS_PROFILE picks the
profile). The metrics carry real spend: the dashboards are internal and never shared. The days a
delivery may still change (the delivery's own day, and the day before when it came before noon
UTC) are marked estimated and left out of the complete-day series.

Rolling back to an exporter from before cache version 2 needs the cache volume removed first
(docker volume rm agent-observability_aws_cost_exporter_data; about five requests refill it): the
old code reads a day's services as numbers, and version 2 stores a dict of usage types per
service. The report part of the cache ("cur") is ignored by older code.
"""

import csv
import fnmatch
import gzip
import io
import json
import os
import statistics
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.environ.get("CACHE_FILE", "/data/cache.json")
ALLOCATION_FILE = os.environ.get("ALLOCATION_FILE", os.path.join(HERE, "allocation.json"))
FIXED_COSTS_FILE = os.environ.get("FIXED_COSTS_FILE", os.path.join(HERE, "fixed-costs.json"))
POLL = int(os.environ.get("POLL_SECONDS", str(12 * 3600)))
FORECAST = int(os.environ.get("FORECAST_SECONDS", str(24 * 3600)))
HISTORY = int(os.environ.get("HISTORY_SECONDS", str(7 * 86400)))
RECORDS = int(os.environ.get("RECORDS_SECONDS", str(24 * 3600)))
BUDGETS = int(os.environ.get("BUDGETS_SECONDS", str(6 * 3600)))
HISTORY_MONTHS = int(os.environ.get("HISTORY_MONTHS", "3"))
MEDIAN_DAYS = int(os.environ.get("MEDIAN_DAYS", "28"))
TRAILING_DAYS = int(os.environ.get("TRAILING_DAYS", "7"))
# Charges that post once a period (domain renewals, monthly tax) stay out of the spike baseline.
SPIKE_EXCLUDE = {x.strip() for x in os.environ.get("SPIKE_EXCLUDE", "Amazon Registrar,Tax").split(",") if x.strip()}
PORT = int(os.environ.get("PORT", "9202"))
# The cost and usage report in S3. Empty bucket: Cost Explorer is the source.
CUR_BUCKET = os.environ.get("CUR_BUCKET", "")
CUR_PREFIX = os.environ.get("CUR_PREFIX", "finops").strip("/")
CUR_REPORT = os.environ.get("CUR_REPORT", "finops")
CUR_REGION = os.environ.get("CUR_REGION", "us-east-1")
CUR_SECONDS = int(os.environ.get("CUR_SECONDS", "3600"))
RESOURCE_LIMIT = int(os.environ.get("RESOURCE_LIMIT", "40"))  # resources kept in the per-resource series
RESOURCE_MONTHS = int(os.environ.get("RESOURCE_MONTHS", "2"))  # months of daily cost per resource
CE_PRICE = 0.01  # USD per Cost Explorer API request
METRIC = "UnblendedCost"
CACHE_VERSION = 2  # bump when the shape of a Cost Explorer part changes; older parts are fetched again
CUR_VERSION = 1  # the same for the report part
UNALLOCATED = "unallocated"
NO_RESOURCE = "(none)"
# Usage types Cost Explorer files under "Amazon Elastic Compute Cloud - Compute"; the rest of EC2 is "EC2 - Other".
EC2_COMPUTE = ("*BoxUsage*", "*SpotUsage*", "*HeavyUsage*", "*DedicatedUsage*", "*HostUsage*", "*UnusedBox*")

lock = threading.Lock()
cache = {"requests": {}, "errors": 0}


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


CONFIG_ERRORS = []  # a config file that could not be read; validate() reports it and main() stops


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        log(f"no {path}; using an empty one")
        return default
    except (OSError, ValueError) as e:  # a directory where a file was expected (a mount of a path that did not exist), or bad JSON
        CONFIG_ERRORS.append(f"cannot read {path}: {type(e).__name__}: {e}")
        return default


def utc_today():
    """Today in UTC. TODAY=YYYY-MM-DD overrides it, for the offline test."""
    t = os.environ.get("TODAY")
    return date.fromisoformat(t) if t else datetime.now(timezone.utc).date()


ALLOCATION = load_json(ALLOCATION_FILE, {"rules": []})
FIXED = load_json(FIXED_COSTS_FILE, {"items": []})
_centers = {}


def known_centers(allocation=None):
    return {c.get("name") for c in (ALLOCATION if allocation is None else allocation).get("cost_centers", [])}


def tag_column(allocation=None):
    """The report column of the cost allocation tag named in allocation.json, or None."""
    key = (ALLOCATION if allocation is None else allocation).get("cost_center_tag")
    return f"resourceTags/user:{key}" if key else None


def cost_center(service, usage_type, resource_id="", tag=""):
    """The cost center of one line: the value of its cost allocation tag when that names a listed
    cost center, else the first rule in allocation.json that matches (service, usage type, resource id)."""
    key = (service, usage_type, resource_id, tag)
    if key not in _centers:
        found = UNALLOCATED
        if tag and tag in known_centers():
            found = tag
        else:
            for rule in ALLOCATION.get("rules", []):
                if rule.get("services") and service not in rule["services"]:
                    continue
                pats = rule.get("usage_types")
                if pats and not any(fnmatch.fnmatchcase(usage_type, p) for p in pats):
                    continue
                pats = rule.get("resource_ids")
                if pats and not any(fnmatch.fnmatchcase(resource_id, p) for p in pats):
                    continue
                found = rule["cost_center"]
                break
        _centers[key] = found
    return _centers[key]


def validate(allocation=None, fixed=None):
    """Problems in the two config files: a rule or an item that names no cost center or one the
    allocation file does not list, a rule that would match everything, an amount that is not a number,
    a cost_center_tag that is not a string. The exporter refuses to start on any of them, so a typo
    cannot make a phantom cost center."""
    problems = list(CONFIG_ERRORS) if allocation is None and fixed is None else []
    allocation = ALLOCATION if allocation is None else allocation
    fixed = FIXED if fixed is None else fixed
    known = known_centers(allocation)
    tag = allocation.get("cost_center_tag")
    if tag is not None and not isinstance(tag, str):
        problems.append(f"allocation.json cost_center_tag must be a string, not {tag!r}")
    for i, rule in enumerate(allocation.get("rules", []), 1):
        c = rule.get("cost_center")
        if not c:
            problems.append(f"allocation.json rule {i} has no cost_center")
        elif c not in known:
            problems.append(f"allocation.json rule {i} names cost center {c!r}, which cost_centers does not list")
        if not rule.get("services") and not rule.get("usage_types") and not rule.get("resource_ids"):
            problems.append(f"allocation.json rule {i} has neither services, usage_types nor resource_ids and would match everything")
    for i, item in enumerate(fixed.get("items", []), 1):
        if not item.get("item"):
            problems.append(f"fixed-costs.json item {i} has no name")
        if item.get("cost_center") not in known:
            problems.append(f"fixed-costs.json item {i} names cost center {item.get('cost_center')!r}, which allocation.json does not list")
        amount = item.get("amount_usd")
        if amount is not None and not isinstance(amount, (int, float)):
            problems.append(f"fixed-costs.json item {i}: amount_usd must be a number or null, not {amount!r}")
    return problems


def count(api, n=1):
    cache["requests"][api] = cache["requests"].get(api, 0) + n


def month_start(d, back=0):
    y, m = d.year, d.month - back
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def next_month(d):
    return month_start(date(d.year + d.month // 12, d.month % 12 + 1, 1))


# ---------------------------------------------------------------- Cost Explorer

def daily_by_service_usage(ce, start, end):
    """Daily cost by service and usage type for [start, end), all pages.

    Returns days (day -> service -> usage type -> cost), the estimated days and the unit.
    """
    days, estimated, unit, token = {}, [], "USD", None
    while True:
        args = dict(TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="DAILY",
                    Metrics=[METRIC], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}])
        if token:
            args["NextPageToken"] = token
        r = ce.get_cost_and_usage(**args)
        count("ce:GetCostAndUsage")
        for res in r["ResultsByTime"]:
            day = res["TimePeriod"]["Start"]
            row = days.setdefault(day, {})
            if res.get("Estimated"):
                estimated.append(day)
            for g in res.get("Groups", []):
                m = g["Metrics"][METRIC]
                unit = m.get("Unit", unit)
                service, usage = g["Keys"][0], g["Keys"][1]
                svc = row.setdefault(service, {})
                svc[usage] = svc.get(usage, 0.0) + float(m["Amount"])
        token = r.get("NextPageToken")
        if not token:
            return days, sorted(set(estimated)), unit


def monthly_by_record_type(ce, start, end):
    """Monthly cost by record type (Usage, Tax, Credit, Refund, ...) for [start, end)."""
    months, token = {}, None
    while True:
        args = dict(TimePeriod={"Start": start.isoformat(), "End": end.isoformat()}, Granularity="MONTHLY",
                    Metrics=[METRIC], GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}])
        if token:
            args["NextPageToken"] = token
        r = ce.get_cost_and_usage(**args)
        count("ce:GetCostAndUsage")
        for res in r["ResultsByTime"]:
            month = res["TimePeriod"]["Start"][:7]
            row = months.setdefault(month, {})
            for g in res.get("Groups", []):
                row[g["Keys"][0]] = row.get(g["Keys"][0], 0.0) + float(g["Metrics"][METRIC]["Amount"])
        token = r.get("NextPageToken")
        if not token:
            return months


# ---------------------------------------------------------------- the cost and usage report

def ce_service(code, name, usage_type, line_type):
    """The Cost Explorer SERVICE name of one report line (see the module docstring, Names)."""
    if line_type == "Tax":
        return "Tax"
    if code == "AmazonEC2":
        return "Amazon Elastic Compute Cloud - Compute" if any(fnmatch.fnmatchcase(usage_type, p) for p in EC2_COMPUTE) else "EC2 - Other"
    return name or code or "unknown"


def short_resource(rid):
    """A resource id without its ARN prefix: i-0abc stays, arn:aws:cloudfront::123:distribution/E1 is distribution/E1."""
    if rid.startswith("arn:"):
        parts = rid.split(":", 5)
        return parts[5] if len(parts) == 6 and parts[5] else rid
    return rid


def cur_aggregate(rows):
    """One billing period's line items as the cache keeps them: days (day -> service -> usage type ->
    resource id -> cost), records (month -> line item type -> cost), tags (resource id -> the cost
    allocation tag's value, when allocation.json names one), the currency and the row count."""
    days, records, tags = {}, {}, {}
    unit, n = None, 0
    col = tag_column()
    for r in rows:
        n += 1
        day = (r.get("lineItem/UsageStartDate") or "")[:10]
        if len(day) != 10:
            continue
        cost = float(r.get("lineItem/UnblendedCost") or 0)
        line_type = r.get("lineItem/LineItemType") or ""
        usage = r.get("lineItem/UsageType") or ""
        service = ce_service(r.get("lineItem/ProductCode") or "", r.get("product/ProductName") or "", usage, line_type)
        rid = short_resource(r.get("lineItem/ResourceId") or "")
        unit = r.get("lineItem/CurrencyCode") or unit
        res = days.setdefault(day, {}).setdefault(service, {}).setdefault(usage, {})
        res[rid] = res.get(rid, 0.0) + cost
        rec = records.setdefault(day[:7], {})
        rec[line_type] = rec.get(line_type, 0.0) + cost
        if col and rid and r.get(col):
            tags[rid] = r[col]
    return {"days": days, "records": records, "tags": tags, "unit": unit or "USD", "rows": n}


def fetch_cur(s3, now, today):
    """Read the report's billing periods whose manifest changed. The periods are the folders under
    CUR_PREFIX/CUR_REPORT/ (YYYYMMDD-YYYYMMDD); each holds <report>-Manifest.json, whose reportKeys
    name the data files of the latest delivery (both versioning modes). Periods older than
    HISTORY_MONTHS, or gone from the bucket, leave the cache."""
    report = f"{CUR_BUCKET}/{CUR_PREFIX}/{CUR_REPORT}"
    part = cache.get("cur", {})
    if part.get("v") != CUR_VERSION or part.get("report") != report:
        part = {"v": CUR_VERSION, "report": report, "periods": {}}
    base = f"{CUR_PREFIX}/{CUR_REPORT}/"
    keep = month_start(today, HISTORY_MONTHS).isoformat()[:7]
    periods, token = {}, None
    while True:
        args = dict(Bucket=CUR_BUCKET, Prefix=base, Delimiter="/")
        if token:
            args["ContinuationToken"] = token
        r = s3.list_objects_v2(**args)
        count("s3:ListObjectsV2")
        for cp in r.get("CommonPrefixes", []):
            folder = cp["Prefix"][len(base):].strip("/")
            if len(folder) == 17 and folder[8] == "-" and folder[:8].isdigit():
                month = f"{folder[:4]}-{folder[4:6]}"
                if month >= keep:
                    periods[month] = cp["Prefix"]
        token = r.get("NextContinuationToken")
        if not token:
            break
    for month, prefix in sorted(periods.items()):
        key = f"{prefix}{CUR_REPORT}-Manifest.json"
        try:
            head = s3.head_object(Bucket=CUR_BUCKET, Key=key)
        except Exception as e:  # a period folder before its first manifest; botocore's 404 is a ClientError
            if "404" in str(e) or "Not Found" in str(e) or "NoSuchKey" in str(e):
                log(f"report {month}: no manifest yet at {key}")
                continue
            raise
        finally:
            count("s3:HeadObject")
        if part["periods"].get(month, {}).get("etag") == head["ETag"]:
            continue
        manifest = json.loads(s3.get_object(Bucket=CUR_BUCKET, Key=key)["Body"].read())
        count("s3:GetObject")
        rows = []
        for k in manifest.get("reportKeys", []):
            body = s3.get_object(Bucket=CUR_BUCKET, Key=k)["Body"].read()
            count("s3:GetObject")
            text = gzip.decompress(body).decode() if k.endswith(".gz") else body.decode()
            rows.extend(csv.DictReader(io.StringIO(text)))
        agg = cur_aggregate(rows)
        delivered = head["LastModified"].timestamp()
        part["periods"][month] = {**agg, "etag": head["ETag"], "delivered": delivered, "fetched": now,
                                  "keys": manifest.get("reportKeys", [])}
        log(f"report {month}: {agg['rows']} line items in {len(agg['days'])} days from {len(manifest.get('reportKeys', []))} file(s), "
            f"delivered {datetime.fromtimestamp(delivered, timezone.utc).isoformat(timespec='minutes')}")
    for month in list(part["periods"]):
        if month < keep or month not in periods:
            del part["periods"][month]
    part["fetched"] = now
    cache["cur"] = part


def cur_cutoff(delivered):
    """The first UTC day a delivery may still change: the delivery's day, or the day before when the
    delivery came before noon UTC (the report lags its usage by hours)."""
    t = datetime.fromtimestamp(delivered, timezone.utc)
    return t.date() if t.hour >= 12 else t.date() - timedelta(days=1)


# ---------------------------------------------------------------- the poll

def fetch(ce, budgets, account, s3=None):
    now = time.time()
    today = utc_today()
    this_month, following = month_start(today), next_month(today)
    hist_start = month_start(today, HISTORY_MONTHS)
    report = bool(CUR_BUCKET)

    if report and now - cache.get("cur", {}).get("fetched", 0) >= CUR_SECONDS:
        fetch_cur(s3, now, today)
    report_has_month = this_month.isoformat()[:7] in cache.get("cur", {}).get("periods", {}) if report else False

    cur = cache.get("current", {})
    if not report_has_month and (cur.get("v") != CACHE_VERSION or cur.get("month") != this_month.isoformat()
                                 or now - cur.get("fetched", 0) >= POLL):
        days, est, unit = daily_by_service_usage(ce, this_month, today + timedelta(days=1))
        cache["current"] = {"v": CACHE_VERSION, "month": this_month.isoformat(), "fetched": now, "days": days,
                            "estimated": est, "unit": unit}
        log(f"current month from Cost Explorer: {len(days)} days, estimated {est}")

    fc = cache.get("forecast", {})
    if not report and (fc.get("end") != following.isoformat() or now - fc.get("fetched", 0) >= FORECAST):
        fc = {"fetched": now, "amount": None, "start": today.isoformat(), "end": following.isoformat()}
        try:
            r = ce.get_cost_forecast(TimePeriod={"Start": today.isoformat(), "End": following.isoformat()},
                                     Metric="UNBLENDED_COST", Granularity="MONTHLY")
            fc["amount"] = float(r["Total"]["Amount"])
        except ce.exceptions.DataUnavailableException:
            log("forecast: no data yet")
        finally:
            count("ce:GetCostForecast")
        cache["forecast"] = fc

    # In report mode the history is fetched once (the months before the report) and kept as it is;
    # the report's own months replace it at render time and age it out.
    hist = cache.get("history", {})
    if (not report and (hist.get("v") != CACHE_VERSION or hist.get("start") != hist_start.isoformat()
                        or hist.get("end") != this_month.isoformat() or now - hist.get("fetched", 0) >= HISTORY)) \
            or (report and hist.get("v") != CACHE_VERSION):
        days, est, unit = daily_by_service_usage(ce, hist_start, this_month)
        cache["history"] = {"v": CACHE_VERSION, "start": hist_start.isoformat(), "end": this_month.isoformat(),
                            "fetched": now, "days": days, "estimated": est, "unit": unit}
        log(f"history from Cost Explorer: {len(days)} days from {hist_start}")

    rec = cache.get("records", {})
    if (not report and (rec.get("start") != hist_start.isoformat() or now - rec.get("fetched", 0) >= RECORDS)) \
            or (report and not rec):
        months = monthly_by_record_type(ce, hist_start, today + timedelta(days=1))
        cache["records"] = {"start": hist_start.isoformat(), "fetched": now, "months": months}
        log(f"record types from Cost Explorer: {len(months)} months")

    if now - cache.get("budgets", {}).get("fetched", 0) >= BUDGETS:
        out, token = [], None
        while True:
            args = {"AccountId": account}
            if token:
                args["NextToken"] = token
            r = budgets.describe_budgets(**args)
            count("budgets:DescribeBudgets")
            for b in r.get("Budgets", []):
                spend = b.get("CalculatedSpend", {})
                out.append({"name": b["BudgetName"], "type": b.get("BudgetType"), "period": b.get("TimeUnit"),
                            "limit": float(b.get("BudgetLimit", {}).get("Amount", "nan")),
                            "unit": b.get("BudgetLimit", {}).get("Unit", ""),
                            "actual": float(spend.get("ActualSpend", {}).get("Amount", "nan")),
                            "forecast": float(spend.get("ForecastedSpend", {}).get("Amount", "nan"))})
            token = r.get("NextToken")
            if not token:
                break
        cache["budgets"] = {"fetched": now, "items": out}


def save():
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def poller():
    import boto3  # here so the render path and its test run without it
    session = boto3.Session()
    ce = session.client("ce", region_name="us-east-1")
    budgets = session.client("budgets", region_name="us-east-1")
    s3 = session.client("s3", region_name=CUR_REGION) if CUR_BUCKET else None
    account = os.environ.get("AWS_ACCOUNT_ID")
    while True:
        try:
            if not account:
                account = session.client("sts", region_name="us-east-1").get_caller_identity()["Account"]
            with lock:
                fetch(ce, budgets, account, s3)
                cache["last_success"] = time.time()
                save()
        except Exception as e:  # keep serving the cache; the error count shows on the dashboard
            with lock:
                cache["errors"] = cache.get("errors", 0) + 1
                save()
            log(f"fetch failed: {type(e).__name__}: {e}")
        time.sleep(300)  # each part checks its own age, so a short loop asks the APIs no more often


# ---------------------------------------------------------------- metrics

def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def usage_rows(part):
    """The cached days of one Cost Explorer part as day -> service -> usage type -> cost.

    A cache written before CACHE_VERSION 2 holds day -> service -> cost; it reads as one
    usage type "" per service until the poller fetches the part again.
    """
    out = {}
    for d, row in part.get("days", {}).items():
        out[d] = {s: (u if isinstance(u, dict) else {"": u}) for s, u in row.items()}
    return out


def median(values):
    return statistics.median(values) if values else None


def render():
    out = []

    def m(name, kind, help_, samples):
        out.append(f"# HELP {name} {help_}\n# TYPE {name} {kind}")
        for labels, value in samples:
            if value is None or value != value:
                continue
            lab = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
            out.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    def add(table, key, v):
        table[key] = table.get(key, 0.0) + v

    with lock:
        cur, hist = cache.get("current", {}), cache.get("history", {})
        periods = cache.get("cur", {}).get("periods", {}) if CUR_BUCKET else {}
        today_d = utc_today()
        today = today_d.isoformat()
        this_month = today[:7]
        prior_month = month_start(today_d, 1).isoformat()[:7]

        # Cost Explorer's days, then the report's months in their place: days (day -> service -> usage
        # type -> cost) for both, lines (day -> service -> usage type -> resource -> cost) for the report
        days = {**usage_rows(hist), **usage_rows(cur)}
        estimated = {d for d in set(hist.get("estimated", [])) | set(cur.get("estimated", [])) if d[:7] not in periods}
        records = dict(cache.get("records", {}).get("months", {}))
        lines, tags, delivered = {}, {}, {}
        for month, p in periods.items():
            days = {d: v for d, v in days.items() if d[:7] != month}
            for d, row in p["days"].items():
                lines[d] = row
                days[d] = {s: {u: sum(rs.values()) for u, rs in us.items()} for s, us in row.items()}
            records[month] = p.get("records", {}).get(month, {})
            tags.update(p.get("tags", {}))
            delivered[month] = p["delivered"]
        # Cost Explorer's estimate flag is informational (it marks the whole current month); only the days a
        # report delivery may still change leave the complete-day series
        open_days = set()
        if delivered:
            cutoff = cur_cutoff(max(delivered.values())).isoformat()
            open_days = {d for d in lines if d >= cutoff}
            estimated |= open_days

        # the same cost three ways: by service, by cost center, and per (service, usage type) pair
        svc = {d: {s: sum(u.values()) for s, u in row.items()} for d, row in days.items()}
        centers = {}
        for d, row in days.items():
            cc = centers.setdefault(d, {})
            if d in lines:
                for s, us in lines[d].items():
                    for u, rs in us.items():
                        for rid, v in rs.items():
                            add(cc, cost_center(s, u, rid, tags.get(rid, "")), v)
            else:
                for s, us in row.items():
                    for u, v in us.items():
                        add(cc, cost_center(s, u), v)
        totals = {d: sum(s.values()) for d, s in svc.items()}
        months, month_tot, cc_months = {}, {}, {}
        for d in days:
            for s, v in svc[d].items():
                add(months, (d[:7], s), v)
            for c, v in centers[d].items():
                add(cc_months, (d[:7], c), v)
            add(month_tot, d[:7], totals[d])
        mtd_svc = {s: v for (mo, s), v in months.items() if mo == this_month}
        mtd_cc = {c: v for (mo, c), v in cc_months.items() if mo == this_month}
        mtd_usage = {}
        for d, row in days.items():
            if d[:7] == this_month:
                for s, us in row.items():
                    for u, v in us.items():
                        add(mtd_usage, (s, u), v)
        mtd_total = month_tot.get(this_month, 0.0)
        allocated = sum(v for c, v in mtd_cc.items() if c != UNALLOCATED)
        has_month = any(d[:7] == this_month for d in days)

        # the spike baseline: complete days, without the once-a-period services
        usage = {d: sum(v for s, v in row.items() if s not in SPIKE_EXCLUDE) for d, row in svc.items()}
        complete = sorted(d for d in usage if d < today and d not in open_days)
        latest = complete[-1] if complete else None
        baseline_days = complete[-1 - MEDIAN_DAYS:-1]
        window = [usage[d] for d in baseline_days]
        trailing = complete[-TRAILING_DAYS:]

        def latest_and_median(rows, skip=()):
            """Per key: the latest complete day's cost and the median of the MEDIAN_DAYS days before it, zero-filled."""
            keys = {k for d in baseline_days + ([latest] if latest else []) for k in rows.get(d, {})} - set(skip)
            out = [(k, rows.get(latest, {}).get(k, 0.0), median([rows.get(d, {}).get(k, 0.0) for d in baseline_days]))
                   for k in sorted(keys)]
            return [x for x in out if x[1] or x[2]]  # a key that is zero on every day (a zero-amount group) says nothing

        # the forecast: Cost Explorer's from its start, or the run rate of this month's complete days
        fc = cache.get("forecast", {})
        forecast, method = None, None
        if CUR_BUCKET:
            month_days = (next_month(today_d) - month_start(today_d)).days
            done = [totals[d] for d in complete if d[:7] == this_month]
            prior = [totals[d] for d in sorted(totals) if d[:7] == prior_month]
            if done:
                forecast, method = sum(done) + statistics.mean(done) * (month_days - len(done)), "run_rate"
            elif prior:
                forecast, method = statistics.mean(prior) * month_days, "prior_month_rate"
        elif fc.get("amount") is not None and fc.get("start", "")[:7] == this_month:
            # the forecast covers [fc.start, month end); actual days before its start complete the month
            forecast = sum(t for d, t in totals.items() if d.startswith(this_month) and d < fc["start"]) + fc["amount"]
            method = "cost_explorer"

        # month to date against the same calendar days of the month before: complete days 1..n of each,
        # n capped at the length of the month before (28 or 29 at the end of March)
        n = min(today_d.day - 1, (month_start(today_d) - timedelta(days=1)).day)
        to_date = {}
        for d in days:
            if d[:7] in (this_month, prior_month) and int(d[8:10]) <= n:
                period = "this_month" if d[:7] == this_month else "prior_month"
                add(to_date, (period, "", ""), totals[d])
                for s, v in svc[d].items():
                    add(to_date, (period, "service", s), v)
                for c, v in centers[d].items():
                    add(to_date, (period, "cost_center", c), v)

        # resources: the report's months only, the top RESOURCE_LIMIT of the last RESOURCE_MONTHS months
        res_day, res_mtd, res_window = {}, {}, {}
        window_start = month_start(today_d, RESOURCE_MONTHS - 1).isoformat()[:7]
        for d, row in lines.items():
            for s, us in row.items():
                for u, rs in us.items():
                    for rid, v in rs.items():
                        name = rid or NO_RESOURCE
                        if d[:7] >= window_start:
                            add(res_window, (s, name), v)
                            add(res_day, (d, s, name), v)
                        if d[:7] == this_month:
                            add(res_mtd, (s, name, cost_center(s, u, rid, tags.get(rid, ""))), v)
        top = {k for k, _ in sorted(res_window.items(), key=lambda x: -abs(x[1]))[:RESOURCE_LIMIT]}
        res_rows = {}
        for (d, s, name), v in res_day.items():
            res_rows.setdefault(d, {})[(s, name)] = v
        mtd_resources = {(s, name) for (s, name, _) in res_mtd}
        rest = sum(v for (s, name, _), v in res_mtd.items() if (s, name) not in top)

        m("aws_cost_source_info", "gauge", "Where the AWS figures come from: the cost and usage report in S3, or Cost Explorer.",
          [({"source": "cur" if CUR_BUCKET else "cost_explorer"}, 1)])
        m("aws_cost_day_usd", "gauge", "Unblended cost of one UTC day by service.",
          [({"day": d, "service": s}, round(v, 6)) for d, row in sorted(svc.items()) for s, v in row.items() if abs(v) >= 1e-6])
        m("aws_cost_day_by_cost_center_usd", "gauge", "Unblended cost of one UTC day by cost center (virtual tags from allocation.json).",
          [({"day": d, "cost_center": c}, round(v, 6)) for d, row in sorted(centers.items()) for c, v in row.items() if abs(v) >= 1e-6])
        m("aws_cost_day_total_usd", "gauge", "Unblended cost of one UTC day, all services; estimated = the day may still change.",
          [({"day": d, "estimated": str(d in estimated).lower()}, round(t, 6)) for d, t in sorted(totals.items())])
        m("aws_cost_month_usd", "gauge", "Unblended cost of a calendar month by service (this month: to date).",
          [({"month": mo, "service": s}, round(v, 6)) for (mo, s), v in sorted(months.items()) if abs(v) >= 1e-6])
        m("aws_cost_month_by_cost_center_usd", "gauge", "Unblended cost of a calendar month by cost center (this month: to date).",
          [({"month": mo, "cost_center": c}, round(v, 6)) for (mo, c), v in sorted(cc_months.items()) if abs(v) >= 1e-6])
        m("aws_cost_month_total_usd", "gauge", "Unblended cost of a calendar month, all services (this month: to date).",
          [({"month": mo}, round(v, 6)) for mo, v in sorted(month_tot.items())])
        m("aws_cost_month_record_usd", "gauge", "Unblended cost of a calendar month by record type: Usage, Tax, Credit, Refund and the like.",
          [({"month": mo, "record_type": t}, round(v, 6)) for mo, row in sorted(records.items()) for t, v in sorted(row.items()) if abs(v) >= 1e-6])
        m("aws_cost_mtd_usd", "gauge", "This month to date, today's partial cost included.",
          [({}, round(mtd_total, 6))] if has_month else [])
        m("aws_cost_mtd_by_service_usd", "gauge", "This month to date by service.",
          [({"service": s}, round(v, 6)) for s, v in sorted(mtd_svc.items()) if abs(v) >= 1e-6])
        m("aws_cost_mtd_by_cost_center_usd", "gauge", "This month to date by cost center.",
          [({"cost_center": c}, round(v, 6)) for c, v in sorted(mtd_cc.items()) if abs(v) >= 1e-6])
        m("aws_cost_mtd_by_usage_type_usd", "gauge", "This month to date per service and usage type.",
          [({"service": s, "usage_type": u, "cost_center": cost_center(s, u)}, round(v, 6))
           for (s, u), v in sorted(mtd_usage.items()) if abs(v) >= 1e-6])
        m("aws_cost_mtd_by_resource_usd", "gauge",
          f"This month to date per resource and cost center, the {RESOURCE_LIMIT} costliest resources of the last {RESOURCE_MONTHS} months (report mode).",
          [({"service": s, "resource_id": name, "cost_center": c}, round(v, 6))
           for (s, name, c), v in sorted(res_mtd.items()) if (s, name) in top and abs(v) >= 1e-6])
        m("aws_cost_mtd_resource_rest_usd", "gauge", "This month to date of the resources beyond the costliest ones.",
          [({}, round(rest, 6))] if lines else [])
        m("aws_cost_resources", "gauge", "Distinct (service, resource id) pairs with a line this month; '(none)' counts as one.",
          [({"period": "this_month"}, len(mtd_resources))] if lines else [])
        m("aws_cost_day_by_resource_usd", "gauge", f"Unblended cost of one UTC day per resource, the {RESOURCE_LIMIT} costliest resources of the last {RESOURCE_MONTHS} months.",
          [({"day": d, "service": s, "resource_id": name}, round(v, 6))
           for (d, s, name), v in sorted(res_day.items()) if (s, name) in top and abs(v) >= 1e-6])
        m("aws_cost_allocated_ratio", "gauge", "Share of this month's cost that a rule in allocation.json (or the cost allocation tag) maps to a cost center.",
          [({}, round(allocated / mtd_total, 6))] if mtd_total > 0 else [])
        m("aws_cost_to_date_days", "gauge", "Complete days of the month compared in the to-date series: 1 to the day before today, capped at the length of the month before.",
          [({}, n)] if has_month else [])
        m("aws_cost_to_date_usd", "gauge", "Cost of the complete days 1 to N of this month, and of the same days of the month before.",
          [({"period": p}, round(v, 6)) for (p, kind, k), v in sorted(to_date.items()) if kind == ""])
        m("aws_cost_to_date_by_service_usd", "gauge", "The to-date comparison by service.",
          [({"period": p, "service": k}, round(v, 6)) for (p, kind, k), v in sorted(to_date.items()) if kind == "service" and abs(v) >= 1e-6])
        m("aws_cost_to_date_by_cost_center_usd", "gauge", "The to-date comparison by cost center.",
          [({"period": p, "cost_center": k}, round(v, 6)) for (p, kind, k), v in sorted(to_date.items()) if kind == "cost_center" and abs(v) >= 1e-6])
        m("aws_cost_trailing_by_cost_center_usd", "gauge", f"Cost of the last {TRAILING_DAYS} complete UTC days by cost center (for unit costs).",
          [({"days": str(len(trailing)), "cost_center": c}, round(v, 6)) for c, v in sorted(
              {c: sum(centers[d].get(c, 0.0) for d in trailing) for d2 in trailing for c in centers[d2]}.items()) if abs(v) >= 1e-6])
        m("aws_cost_month_forecast_usd", "gauge",
          "Month-end forecast: Cost Explorer's (plus the actual days before its start), or in report mode the run rate of this "
          "month's complete days (the month before's when this month has none yet); aws_cost_forecast_info says which.",
          [({}, round(forecast, 6))] if forecast is not None else [])
        m("aws_cost_forecast_info", "gauge", "The method behind aws_cost_month_forecast_usd.",
          [({"method": method}, 1)] if method else [])
        m("aws_cost_latest_day_usd", "gauge", "Cost of the latest complete UTC day without SPIKE_EXCLUDE services.",
          [({}, round(usage[latest], 6))] if latest else [])
        m("aws_cost_latest_day_estimated", "gauge", "1 when Cost Explorer still marks the latest complete day as an estimate (it marks the whole current month); "
          "a day the report's latest delivery may still change is never the latest complete day.",
          [({}, int(latest in estimated))] if latest else [])
        m("aws_cost_latest_day_info", "gauge", "The latest complete UTC day the anomaly series describe.",
          [({"day": latest}, 1)] if latest else [])
        m("aws_cost_trailing_median_day_usd", "gauge", f"Median daily cost of the {MEDIAN_DAYS} days before the latest day, same services.",
          [({}, round(median(window), 6))] if window else [])
        by_service = latest_and_median(svc, skip=SPIKE_EXCLUDE)
        m("aws_cost_service_latest_day_usd", "gauge", "Cost of the latest complete UTC day by service, SPIKE_EXCLUDE services left out.",
          [({"service": s}, round(v, 6)) for s, v, _ in by_service])
        m("aws_cost_service_median_day_usd", "gauge", f"Median daily cost by service over the {MEDIAN_DAYS} days before the latest day, days without the service counted as zero.",
          [({"service": s}, round(v, 6)) for s, _, v in by_service if v is not None])
        by_center = latest_and_median(centers)
        m("aws_cost_cost_center_latest_day_usd", "gauge", "Cost of the latest complete UTC day by cost center.",
          [({"cost_center": c}, round(v, 6)) for c, v, _ in by_center])
        m("aws_cost_cost_center_median_day_usd", "gauge", f"Median daily cost by cost center over the {MEDIAN_DAYS} days before the latest day, zero-filled.",
          [({"cost_center": c}, round(v, 6)) for c, _, v in by_center if v is not None])
        by_resource = [x for x in latest_and_median(res_rows) if x[0][0] not in SPIKE_EXCLUDE and x[0] in top]
        m("aws_cost_resource_latest_day_usd", "gauge", "Cost of the latest complete UTC day by resource (report mode), SPIKE_EXCLUDE services left out.",
          [({"service": s, "resource_id": name}, round(v, 6)) for (s, name), v, _ in by_resource])
        m("aws_cost_resource_median_day_usd", "gauge", f"Median daily cost by resource over the {MEDIAN_DAYS} days before the latest day, zero-filled.",
          [({"service": s, "resource_id": name}, round(v, 6)) for (s, name), _, v in by_resource if v is not None])
        b = cache.get("budgets", {}).get("items", [])
        m("aws_budget_limit_usd", "gauge", "Budget amount from AWS Budgets.",
          [({"budget": x["name"], "period": x["period"], "unit": x["unit"]}, x["limit"]) for x in b])
        m("aws_budget_actual_usd", "gauge", "Actual spend in the budget period, as AWS Budgets calculates it.",
          [({"budget": x["name"], "period": x["period"], "unit": x["unit"]}, x["actual"]) for x in b])
        m("aws_budget_forecast_usd", "gauge", "Forecast spend for the budget period, as AWS Budgets calculates it.",
          [({"budget": x["name"], "period": x["period"], "unit": x["unit"]}, x["forecast"]) for x in b])
        items = FIXED.get("items", [])
        priced = [x for x in items if isinstance(x.get("amount_usd"), (int, float))]
        m("fixed_cost_month_usd", "gauge", "Monthly cost outside AWS entered by hand in fixed-costs.json (subscriptions, seats).",
          [({"item": x["item"], "provider": x.get("provider", ""), "cost_center": x.get("cost_center", UNALLOCATED)}, round(float(x["amount_usd"]), 6))
           for x in priced])
        m("fixed_cost_items", "gauge", "Items in fixed-costs.json, priced (an amount entered) or unpriced (amount null).",
          [({"state": "priced"}, len(priced)), ({"state": "unpriced"}, len(items) - len(priced))])
        m("aws_cost_report_delivery_timestamp_seconds", "gauge", "When the report's latest delivery of each billing period landed in S3 (its manifest's LastModified).",
          [({"period": mo}, round(t)) for mo, t in sorted(delivered.items())])
        m("aws_cost_report_line_items", "gauge", "Line items read from the report's latest delivery of each billing period.",
          [({"period": mo}, p.get("rows", 0)) for mo, p in sorted(periods.items())])
        m("aws_cost_exporter_api_requests_total", "counter", "AWS API requests made by this exporter, kept across restarts.",
          [({"api": k}, v) for k, v in sorted(cache["requests"].items())])
        ce_n = sum(v for k, v in cache["requests"].items() if k.startswith("ce:"))
        m("aws_cost_explorer_spend_usd_total", "counter", "What the exporter's Cost Explorer requests cost, at USD 0.01 each.",
          [({}, round(ce_n * CE_PRICE, 2))])
        m("aws_cost_exporter_fetch_timestamp_seconds", "gauge", "When each part was last fetched.",
          [({"part": k}, cache[k]["fetched"]) for k in ("current", "forecast", "history", "records", "budgets", "cur") if k in cache and "fetched" in cache[k]])
        m("aws_cost_exporter_last_success_timestamp_seconds", "gauge", "Last poll loop (every 5 min) that ended without an error.",
          [({}, cache.get("last_success"))])
        m("aws_cost_exporter_errors_total", "counter", "Failed polls, kept across restarts.", [({}, cache.get("errors", 0))])
        unit = next((p["unit"] for p in periods.values() if p.get("unit")), None) or cur.get("unit") or hist.get("unit")
        m("aws_cost_exporter_unit_info", "gauge", "Currency unit of the source.", [({"unit": unit}, 1)] if unit else [])
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    global cache
    try:
        with open(CACHE_FILE) as f:
            cache = {**cache, **json.load(f)}
        log(f"cache loaded, requests so far {cache['requests']}")
    except FileNotFoundError:
        log("no cache yet")
    problems = validate()
    if problems:
        for p in problems:
            log(f"config error: {p}")
        return 2
    log(f"allocation: {len(ALLOCATION.get('rules', []))} rules; fixed costs: {len(FIXED.get('items', []))} items from {FIXED_COSTS_FILE}")
    log(f"source: {'cost and usage report ' + CUR_PREFIX + '/' + CUR_REPORT + ' in S3, checked every ' + str(CUR_SECONDS) + ' s' if CUR_BUCKET else 'Cost Explorer'}")
    threading.Thread(target=poller, daemon=True).start()
    log(f"serving :{PORT}/metrics")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
