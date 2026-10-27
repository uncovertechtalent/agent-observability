"""AWS spend for the stack: Cost Explorer and AWS Budgets as Prometheus metrics on :9202.

Cost Explorer bills USD 0.01 per request, so the exporter asks rarely and caches every
answer in /data/cache.json. A restart reads the cache and asks again only when the
cached part is due:
  - every POLL_SECONDS (default 12 h), and at each new month: this month's daily cost by
    service and usage type
  - every FORECAST_SECONDS (default 24 h): Cost Explorer's forecast for the rest of the month
  - every HISTORY_SECONDS (default 7 days), and at each new month: daily cost by service and
    usage type for the three months before this one
  - every RECORDS_SECONDS (default 24 h): monthly cost by record type (usage, tax, credit,
    refund) for the same span
  - every BUDGETS_SECONDS (default 6 h): AWS Budgets (no charge per request)
That is about 4.3 Cost Explorer requests a day, some USD 1.30 a month. Cost Explorer itself
refreshes a few times a day. Requests are counted per API and kept in the cache, so
aws_cost_explorer_spend_usd_total shows what the exporter itself has cost.

Virtual tags: the account carries no cost allocation tags, so allocation.json maps every
(service, usage type) pair to a cost center by rule, the way a FinOps tool's virtual tags do;
a pair no rule matches is "unallocated". fixed-costs.json lists monthly costs outside AWS
(subscriptions, seats) entered by hand; the committed file is the example with no amounts, and
compose mounts the priced copy (fixed-costs.local.json, git-ignored) at FIXED_COSTS_FILE when
there is one. Both files are read once, at start, and checked: a rule or an item that names a
cost center the allocation file does not list stops the exporter with a message.

Credentials come from the host's AWS shared config, mounted read-only (AWS_PROFILE picks
the profile). The metrics carry real spend: the dashboards are internal and never shared.
Cost Explorer data lags by up to a day and the latest days are marked estimated.
"""

import fnmatch
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
CE_PRICE = 0.01  # USD per Cost Explorer API request
METRIC = "UnblendedCost"
CACHE_VERSION = 2  # bump when the shape of a cached part changes; older parts are fetched again
UNALLOCATED = "unallocated"

lock = threading.Lock()
cache = {"requests": {}, "errors": 0}


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        log(f"no {path}; using an empty one")
        return default


ALLOCATION = load_json(ALLOCATION_FILE, {"rules": []})
FIXED = load_json(FIXED_COSTS_FILE, {"items": []})
_centers = {}


def cost_center(service, usage_type):
    """The cost center of one (service, usage type) pair: the first rule in allocation.json that matches."""
    key = (service, usage_type)
    if key not in _centers:
        found = UNALLOCATED
        for rule in ALLOCATION.get("rules", []):
            if rule.get("services") and service not in rule["services"]:
                continue
            pats = rule.get("usage_types")
            if pats and not any(fnmatch.fnmatchcase(usage_type, p) for p in pats):
                continue
            found = rule["cost_center"]
            break
        _centers[key] = found
    return _centers[key]


def validate(allocation=None, fixed=None):
    """Problems in the two config files: a rule or an item that names no cost center or one the
    allocation file does not list, a rule that would match everything, an amount that is not a number.
    The exporter refuses to start on any of them, so a typo cannot make a phantom cost center."""
    allocation = ALLOCATION if allocation is None else allocation
    fixed = FIXED if fixed is None else fixed
    known = {c.get("name") for c in allocation.get("cost_centers", [])}
    problems = []
    for i, rule in enumerate(allocation.get("rules", []), 1):
        c = rule.get("cost_center")
        if not c:
            problems.append(f"allocation.json rule {i} has no cost_center")
        elif c not in known:
            problems.append(f"allocation.json rule {i} names cost center {c!r}, which cost_centers does not list")
        if not rule.get("services") and not rule.get("usage_types"):
            problems.append(f"allocation.json rule {i} has neither services nor usage_types and would match everything")
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


def fetch(ce, budgets, account):
    now = time.time()
    today = datetime.now(timezone.utc).date()
    this_month, following = month_start(today), next_month(today)
    hist_start = month_start(today, HISTORY_MONTHS)

    cur = cache.get("current", {})
    if cur.get("v") != CACHE_VERSION or cur.get("month") != this_month.isoformat() or now - cur.get("fetched", 0) >= POLL:
        days, est, unit = daily_by_service_usage(ce, this_month, today + timedelta(days=1))
        cache["current"] = {"v": CACHE_VERSION, "month": this_month.isoformat(), "fetched": now, "days": days,
                            "estimated": est, "unit": unit}
        log(f"current month: {len(days)} days, estimated {est}")

    fc = cache.get("forecast", {})
    if fc.get("end") != following.isoformat() or now - fc.get("fetched", 0) >= FORECAST:
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

    hist = cache.get("history", {})
    if hist.get("v") != CACHE_VERSION or hist.get("start") != hist_start.isoformat() \
            or hist.get("end") != this_month.isoformat() or now - hist.get("fetched", 0) >= HISTORY:
        days, est, unit = daily_by_service_usage(ce, hist_start, this_month)
        cache["history"] = {"v": CACHE_VERSION, "start": hist_start.isoformat(), "end": this_month.isoformat(),
                            "fetched": now, "days": days, "estimated": est, "unit": unit}
        log(f"history: {len(days)} days from {hist_start}")

    rec = cache.get("records", {})
    if rec.get("start") != hist_start.isoformat() or now - rec.get("fetched", 0) >= RECORDS:
        months = monthly_by_record_type(ce, hist_start, today + timedelta(days=1))
        cache["records"] = {"start": hist_start.isoformat(), "fetched": now, "months": months}
        log(f"record types: {len(months)} months")

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
    account = os.environ.get("AWS_ACCOUNT_ID")
    while True:
        try:
            if not account:
                account = session.client("sts", region_name="us-east-1").get_caller_identity()["Account"]
            with lock:
                fetch(ce, budgets, account)
                cache["last_success"] = time.time()
                save()
        except Exception as e:  # keep serving the cache; the error count shows on the dashboard
            with lock:
                cache["errors"] = cache.get("errors", 0) + 1
                save()
            log(f"fetch failed: {type(e).__name__}: {e}")
        time.sleep(300)  # each part checks its own age, so a short loop asks the API no more often


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def usage_rows(part):
    """The cached days of one part as day -> service -> usage type -> cost.

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
        days = {**usage_rows(hist), **usage_rows(cur)}  # day -> service -> usage type -> cost
        estimated = set(hist.get("estimated", [])) | set(cur.get("estimated", []))
        today_d = datetime.now(timezone.utc).date()
        today = today_d.isoformat()
        this_month = cur.get("month", "")[:7]
        prior_month = month_start(today_d, 1).isoformat()[:7]

        # the same cost three ways: by service, by cost center, and per (service, usage type) pair
        svc = {d: {s: sum(u.values()) for s, u in row.items()} for d, row in days.items()}
        centers = {}
        for d, row in days.items():
            cc = centers.setdefault(d, {})
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

        fc = cache.get("forecast", {})
        # the forecast covers [fc.start, month end); actual days before its start complete the month
        before_forecast = sum(t for d, t in totals.items() if d.startswith(this_month) and d < fc.get("start", today))

        # the spike baseline: complete days, without the once-a-period services
        usage = {d: sum(v for s, v in row.items() if s not in SPIKE_EXCLUDE) for d, row in svc.items()}
        complete = sorted(d for d in usage if d < today)
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

        # month to date against the same calendar days of the month before: complete days 1..n of each
        n = today_d.day - 1
        to_date = {}
        for d in days:
            if d[:7] in (this_month, prior_month) and int(d[8:10]) <= n:
                period = "this_month" if d[:7] == this_month else "prior_month"
                add(to_date, (period, "", ""), totals[d])
                for s, v in svc[d].items():
                    add(to_date, (period, "service", s), v)
                for c, v in centers[d].items():
                    add(to_date, (period, "cost_center", c), v)

        m("aws_cost_day_usd", "gauge", "Unblended cost of one UTC day by service, from Cost Explorer.",
          [({"day": d, "service": s}, round(v, 6)) for d, row in sorted(svc.items()) for s, v in row.items() if abs(v) >= 1e-6])
        m("aws_cost_day_usage_usd", "gauge", "Unblended cost of one UTC day per service and usage type, with the cost center allocation.json gives the pair.",
          [({"day": d, "service": s, "usage_type": u, "cost_center": cost_center(s, u)}, round(v, 6))
           for d, row in sorted(days.items()) for s, us in row.items() for u, v in us.items() if abs(v) >= 1e-6])
        m("aws_cost_day_by_cost_center_usd", "gauge", "Unblended cost of one UTC day by cost center (virtual tags from allocation.json).",
          [({"day": d, "cost_center": c}, round(v, 6)) for d, row in sorted(centers.items()) for c, v in row.items() if abs(v) >= 1e-6])
        m("aws_cost_day_total_usd", "gauge", "Unblended cost of one UTC day, all services.",
          [({"day": d, "estimated": str(d in estimated).lower()}, round(t, 6)) for d, t in sorted(totals.items())])
        m("aws_cost_month_usd", "gauge", "Unblended cost of a calendar month by service (this month: to date).",
          [({"month": mo, "service": s}, round(v, 6)) for (mo, s), v in sorted(months.items()) if abs(v) >= 1e-6])
        m("aws_cost_month_by_cost_center_usd", "gauge", "Unblended cost of a calendar month by cost center (this month: to date).",
          [({"month": mo, "cost_center": c}, round(v, 6)) for (mo, c), v in sorted(cc_months.items()) if abs(v) >= 1e-6])
        m("aws_cost_month_total_usd", "gauge", "Unblended cost of a calendar month, all services (this month: to date).",
          [({"month": mo}, round(v, 6)) for mo, v in sorted(month_tot.items())])
        m("aws_cost_month_record_usd", "gauge", "Unblended cost of a calendar month by record type: Usage, Tax, Credit, Refund and the like.",
          [({"month": mo, "record_type": t}, round(v, 6))
           for mo, row in sorted(cache.get("records", {}).get("months", {}).items()) for t, v in sorted(row.items())])
        m("aws_cost_mtd_usd", "gauge", "This month to date, today's partial cost included.",
          [({}, round(mtd_total, 6))] if cur else [])
        m("aws_cost_mtd_by_service_usd", "gauge", "This month to date by service.",
          [({"service": s}, round(v, 6)) for s, v in sorted(mtd_svc.items()) if abs(v) >= 1e-6])
        m("aws_cost_mtd_by_cost_center_usd", "gauge", "This month to date by cost center.",
          [({"cost_center": c}, round(v, 6)) for c, v in sorted(mtd_cc.items()) if abs(v) >= 1e-6])
        m("aws_cost_mtd_by_usage_type_usd", "gauge", "This month to date per service and usage type.",
          [({"service": s, "usage_type": u, "cost_center": cost_center(s, u)}, round(v, 6))
           for (s, u), v in sorted(mtd_usage.items()) if abs(v) >= 1e-6])
        m("aws_cost_allocated_ratio", "gauge", "Share of this month's cost that a rule in allocation.json maps to a cost center.",
          [({}, round(allocated / mtd_total, 6))] if mtd_total > 0 else [])
        m("aws_cost_to_date_days", "gauge", "Complete days of the month compared in the to-date series: 1 to the day before today.",
          [({}, n)] if cur else [])
        m("aws_cost_to_date_usd", "gauge", "Cost of the complete days 1 to N of this month, and of the same days of the month before.",
          [({"period": p}, round(v, 6)) for (p, kind, k), v in sorted(to_date.items()) if kind == ""])
        m("aws_cost_to_date_by_service_usd", "gauge", "The to-date comparison by service.",
          [({"period": p, "service": k}, round(v, 6)) for (p, kind, k), v in sorted(to_date.items()) if kind == "service" and abs(v) >= 1e-6])
        m("aws_cost_to_date_by_cost_center_usd", "gauge", "The to-date comparison by cost center.",
          [({"period": p, "cost_center": k}, round(v, 6)) for (p, kind, k), v in sorted(to_date.items()) if kind == "cost_center" and abs(v) >= 1e-6])
        m("aws_cost_trailing_usd", "gauge", f"Cost of the last {TRAILING_DAYS} complete UTC days, all services (for unit costs).",
          [({"days": str(len(trailing))}, round(sum(totals[d] for d in trailing), 6))] if trailing else [])
        m("aws_cost_trailing_by_cost_center_usd", "gauge", f"Cost of the last {TRAILING_DAYS} complete UTC days by cost center (for unit costs).",
          [({"days": str(len(trailing)), "cost_center": c}, round(v, 6)) for c, v in sorted(
              {c: sum(centers[d].get(c, 0.0) for d in trailing) for d2 in trailing for c in centers[d2]}.items()) if abs(v) >= 1e-6])
        m("aws_cost_month_forecast_usd", "gauge",
          "Month-end forecast: actual days before the forecast's start plus Cost Explorer's forecast to month end.",
          [({}, round(before_forecast + fc["amount"], 6))] if fc.get("amount") is not None and fc.get("start", "")[:7] == this_month else [])
        m("aws_cost_latest_day_usd", "gauge", "Cost of the latest UTC day before today without SPIKE_EXCLUDE services (may be partial).",
          [({}, round(usage[latest], 6))] if latest else [])
        m("aws_cost_latest_day_estimated", "gauge", "1 when Cost Explorer marks the latest day as estimated.",
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
        m("aws_cost_exporter_api_requests_total", "counter", "AWS API requests made by this exporter, kept across restarts.",
          [({"api": k}, v) for k, v in sorted(cache["requests"].items())])
        ce_n = sum(v for k, v in cache["requests"].items() if k.startswith("ce:"))
        m("aws_cost_explorer_spend_usd_total", "counter", "What the exporter's Cost Explorer requests cost, at USD 0.01 each.",
          [({}, round(ce_n * CE_PRICE, 2))])
        m("aws_cost_exporter_fetch_timestamp_seconds", "gauge", "When each part was last fetched.",
          [({"part": k}, cache[k]["fetched"]) for k in ("current", "forecast", "history", "records", "budgets") if k in cache])
        m("aws_cost_exporter_last_success_timestamp_seconds", "gauge", "Last poll loop (every 5 min) that ended without an error.",
          [({}, cache.get("last_success"))])
        m("aws_cost_exporter_errors_total", "counter", "Failed polls, kept across restarts.", [({}, cache.get("errors", 0))])
        unit = cur.get("unit") or hist.get("unit")
        m("aws_cost_exporter_unit_info", "gauge", "Currency unit Cost Explorer reported.", [({"unit": unit}, 1)] if unit else [])
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
    threading.Thread(target=poller, daemon=True).start()
    log(f"serving :{PORT}/metrics")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
