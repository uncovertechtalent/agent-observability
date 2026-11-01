"""Offline test of the guards in finops_public(): what stays out of the public FinOps cut.

    python3 grafana/test_build.py

No Grafana and no network. Each case plants one fault in what finops() returns and expects the
build to stop with the named assertion; the unchanged cut must pass.
"""

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("build", os.path.join(HERE, "build.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
real_finops = build.finops


def panel(d, title):
    return next(p for p in d["panels"] if p["title"] == title)


def plant(edit):
    def finops():
        d = real_finops()
        edit(d)
        return d
    return finops


def refuses(edit, needle):
    build.finops = plant(edit)
    try:
        build.finops_public()
    except AssertionError as e:
        assert needle in str(e), f"stopped for another reason: {e}"
        return
    finally:
        build.finops = real_finops
    raise SystemExit(f"FAIL: the cut passed with a planted fault ({needle})")


cut = build.finops_public()
assert cut["uid"] == "finops-public" and cut["templating"]["list"] == []
titles = {p["title"] for p in cut["panels"]}
for dropped in ("Top resources this month", "Fixed costs entered", "Resources this month", "Savings Plans and Reserved Instances"):
    assert dropped not in titles, f"FAIL: {dropped} is in the public cut"
for kept in ("One bill", "Plan price this month", "List price through the month against the plan",
             "Cache hit ratio, month to date", "Cache hit ratio per day"):
    assert kept in titles, f"FAIL: {kept} is not in the public cut"

# Only the Claude plan item may be read from the fixed-cost series.
refuses(lambda d: panel(d, "AWS, month to date")["targets"][0].update(expr="sum(fixed_cost_month_usd)"),
        "reads a fixed cost other than the Claude plan")
refuses(lambda d: panel(d, "AWS, month to date")["targets"][0].update(expr='max(fixed_cost_month_usd{item="GitHub"})'),
        "reads a fixed cost other than the Claude plan")
# A kept panel that is renamed in finops() leaves the cut loudly.
refuses(lambda d: panel(d, "One bill").update(title="One bill (renamed)"), "panels not found")
# A kept panel that shows labels without naming them.
refuses(lambda d: panel(d, "AWS, month to date")["targets"][0].update(format="table", expr="aws_cost_mtd_usd"),
        "shows labels without naming them")
# The words of the dropped panels never reach the JSON.
refuses(lambda d: panel(d, "AWS, month to date").update(description="Per resource, from the Resources row."), "in the public JSON")
refuses(lambda d: panel(d, "AWS, month to date").update(description="Entered in fixed-costs.json."), "in the public JSON")
# The plan line of the chart must not use a Grafana time variable (a public dashboard does not interpolate it).
chart = panel(cut, "List price through the month against the plan")
assert not any("__to" in t.get("expr", "") or "__from" in t.get("expr", "") for t in chart["targets"]), "FAIL: time variable in a public query"

print("ok: the public FinOps cut passes and refuses 6 planted faults")


# ---- Chain Traveler traffic (public): one site, counts only
import copy

ct = build.chaintraveler_traffic_public()
assert ct["uid"] == "chaintraveler-traffic-public" and ct["templating"]["list"] == []
ct_titles = {p["title"] for p in ct["panels"]}
for kept in ("App opens", "Timetable checks", "Timetable downloads", "Countries", "Crawlers and scripts", "Read this first"):
    assert kept in ct_titles, f"FAIL: {kept} is not in the Chain Traveler cut"
for dropped in ("Referrers", "Top pages"):
    assert dropped not in ct_titles, f"FAIL: {dropped} is in the Chain Traveler cut"


def ct_refuses(edit, needle):
    d = copy.deepcopy(ct)
    edit(d)
    try:
        build.check_site_public(d, build.CT_SITE)
    except AssertionError as e:
        assert needle in str(e), f"stopped for another reason: {e}"
        return
    raise SystemExit(f"FAIL: the Chain Traveler cut passed with a planted fault ({needle})")


ct_refuses(lambda d: panel(d, "App opens")["targets"][0].update(expr='sum(count_over_time({job="site-traffic"} [$__range]))'), "not fixed to chaintraveler.app")
ct_refuses(lambda d: panel(d, "App opens")["targets"][0].update(expr='sum(count_over_time({job="site-traffic", site=~"$site"} [$__range]))'), "not fixed to chaintraveler.app")
ct_refuses(lambda d: panel(d, "App opens")["targets"][0].update(expr='sum(count_over_time({job="site-traffic", site="chaintraveler.app", site=~".*"} [$__range]))'), "a site variable or a pattern")
ct_refuses(lambda d: panel(d, "Countries")["targets"][0].update(expr='sum by (referrer) (count_over_time({job="site-traffic", site="chaintraveler.app"} | json [$__range]))'), "reads referrer or User-Agent")
ct_refuses(lambda d: d["templating"]["list"].append({"name": "site"}), "template variables")
ct_refuses(lambda d: panel(d, "Read this first")["options"].update(content="Traffic of machinebehavior.io too."), "in the JSON")
ct_refuses(lambda d: panel(d, "Read this first")["options"].update(content="Top referrer hosts."), "in the JSON")
# Every query of the cut is the Chain Traveler stream, and none uses a Grafana time variable that a public dashboard does not fill.
for p in ct["panels"]:
    for t in p.get("targets", []):
        assert "__from" not in t["expr"] and "__to" not in t["expr"], f"FAIL: {p['title']} uses a time variable a public dashboard does not interpolate"

print("ok: the Chain Traveler public cut passes and refuses 7 planted faults")
