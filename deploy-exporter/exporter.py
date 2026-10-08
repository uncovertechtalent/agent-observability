"""Watch the GitHub Actions deploys of the three sites and feed them to the stack.

Every completed deploy run becomes one line in Loki (stream job="site-deploys"),
each of its steps one line in job="site-deploy-steps" and each gate check one line
in job="site-gate-checks", stamped with the time it finished. Grafana draws the history and annotates deploys from those streams.
The latest state of each site (last run, gate result per check, map size, the
decision-probe fold rates) is served as Prometheus metrics on :9201.

Nothing in the site repos changes: the exporter only reads the GitHub API and the
sites' public JSON, so it also sees runs that died before any step could report.
Standard library only.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("GITHUB_TOKEN", "")
LOKI = os.environ.get("LOKI_URL", "http://loki:3100")
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
POLL = int(os.environ.get("POLL_SECONDS", "60"))
PORT = int(os.environ.get("PORT", "9201"))
# Loki rejects samples older than a week (reject_old_samples_max_age); older runs are counted, not pushed.
MAX_AGE = 7 * 86400 - 3600

# public=False: the repo is private, so run titles, commit SHAs and URLs stay out of Loki.
SITES = json.loads(os.environ.get("SITES_JSON", "null")) or [
    {"site": "machinebehavior.io", "repo": "uncovertechtalent/machinebehavior.io", "workflow": "Conformity", "public": True},
    {"site": "tychat.io", "repo": "uncovertechtalent/tychat.io", "workflow": "Conformity", "public": True},
    {"site": "uncovertechtalent.com", "repo": "uncovertechtalent-git/uncovertechtalent",
     "workflow": "Deploy Hugo site to Pages", "public": False},
]
PROBE_URL = os.environ.get("PROBE_URL", "https://machinebehavior.io/conformity/probes/latest.json")
CHECK_STATE = {"fail": 0, "pending": 1, "partial": 2, "pass": 3}
NOISE_STEPS = re.compile(r"^(Set up job|Complete job|Post )")

state = {"seen": {}, "counts": {}, "latest": {}, "probe": None, "errors": 0, "last_poll": 0}
lock = threading.Lock()


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if s else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def gh(path, raw=False):
    req = urllib.request.Request("https://api.github.com/" + path.lstrip("/"), headers={
        "Accept": "application/vnd.github+json", "User-Agent": "deploy-exporter",
        **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})})
    if not raw:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    # Job logs answer with a redirect to signed blob storage, which must not see the token.
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=30)
        return ""
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        loc = e.headers["Location"]
    with urllib.request.urlopen(urllib.request.Request(loc, headers={"User-Agent": "deploy-exporter"}), timeout=60) as r:
        return r.read().decode("utf-8", "ignore")


def parse_log(text):
    """Pull the gate verdict and the map refresh numbers out of a job log."""
    out = {}
    m = re.search(r"run\.py: site=\S+ trigger=(\S+) sha=\S+ overall=(\w+) findings_open=(\d+) warnings=(\d+) checks=(.*)", text)
    if m:
        out["trigger"], out["gate"] = m.group(1), m.group(2)
        out["findings_open"], out["warnings"] = int(m.group(3)), int(m.group(4))
        out["checks"] = dict(c.strip().split(":", 1) for c in m.group(5).strip().split(",") if ":" in c)
    m = re.search(r"refreshed nodes (\d+) links (\d+) \| committed nodes (\d+) links (\d+)", text)
    if m:
        out["map"] = {"nodes": int(m.group(1)), "links": int(m.group(2)),
                      "committed_nodes": int(m.group(3)), "committed_links": int(m.group(4))}
        out["map"]["kept"] = "map refreshed and deployed" in text
        c = re.search(r"carried forward (\d+) links from (\d+) unreachable pages", text)
        d = re.search(r"dropped (\d+) non-html pages", text)
        out["map"]["carried_links"] = int(c.group(1)) if c else 0
        out["map"]["unreachable_pages"] = int(c.group(2)) if c else 0
        out["map"]["dropped_pages"] = int(d.group(1)) if d else 0
        # the deploy ships the refresh only when it lost nothing; otherwise the committed snapshot
        shipped = "" if out["map"]["kept"] else "committed_"
        out["map"]["shipped_nodes"] = out["map"][shipped + "nodes"]
        out["map"]["shipped_links"] = out["map"][shipped + "links"]
    return out


def build_record(cfg, run):
    jobs = gh(f"repos/{cfg['repo']}/actions/runs/{run['id']}/jobs?per_page=50")["jobs"]
    rec = {
        "site": cfg["site"], "run_id": run["id"], "run_number": run["run_number"], "event": run["event"],
        "conclusion": run["conclusion"], "started": run.get("run_started_at"), "finished": run["updated_at"],
        "duration_s": round(ts(run["updated_at"]) - ts(run.get("run_started_at") or run["created_at"])),
        "jobs": [], "steps": [],
    }
    if cfg["public"]:
        rec.update(title=run["display_title"], sha=run["head_sha"][:7], url=run["html_url"])
    gate_failed = False
    for j in jobs:
        jd = round(ts(j["completed_at"]) - ts(j["started_at"])) if j.get("completed_at") and j.get("started_at") else 0
        rec["jobs"].append({"name": j["name"], "conclusion": j["conclusion"], "duration_s": jd})
        for s in j.get("steps", []):
            if NOISE_STEPS.match(s["name"]) or not s.get("completed_at") or not s.get("started_at"):
                continue
            rec["steps"].append({"job": j["name"], "step": s["name"], "conclusion": s["conclusion"],
                                 "duration_s": round(ts(s["completed_at"]) - ts(s["started_at"])),
                                 "finished": s["completed_at"]})
            if s["name"] == "Gate" and s["conclusion"] == "failure":
                gate_failed = True
        if j["conclusion"] in ("success", "failure"):
            try:
                rec.update({k: v for k, v in parse_log(gh(f"repos/{cfg['repo']}/actions/jobs/{j['id']}/logs", raw=True)).items()
                            if k not in rec or k == "map"})
            except Exception as e:
                log("log fetch failed", cfg["site"], j["id"], e)
    rec["outcome"] = ("deployed" if run["conclusion"] == "success" else "blocked" if gate_failed
                      else "cancelled" if run["conclusion"] == "cancelled" else "failed")
    return rec


def push_loki(rec):
    if not LOKI or time.time() - ts(rec["finished"]) > MAX_AGE:
        return
    labels = {"job": "site-deploys", "site": rec["site"], "outcome": rec["outcome"]}
    line = {k: v for k, v in rec.items() if k != "steps"}
    streams = [{"stream": labels, "values": [[str(int(ts(rec["finished"]) * 1e9)), json.dumps(line)]]}]
    for s in rec["steps"]:
        streams.append({"stream": {"job": "site-deploy-steps", "site": rec["site"], "gh_job": s["job"], "step": s["step"]},
                        "values": [[str(int(ts(s["finished"]) * 1e9)),
                                    json.dumps({"run_id": rec["run_id"], "conclusion": s["conclusion"], "duration_s": s["duration_s"]})]]})
    for check, result in rec.get("checks", {}).items():
        streams.append({"stream": {"job": "site-gate-checks", "site": rec["site"], "check": check},
                        "values": [[str(int(ts(rec["finished"]) * 1e9)),
                                    json.dumps({"run_id": rec["run_id"], "result": result, "state": CHECK_STATE.get(result, -1)})]]})
    req = urllib.request.Request(LOKI + "/loki/api/v1/push", data=json.dumps({"streams": streams}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=15).read()


def flush_loki():
    """Loki serves lines older than its ingester window only once their chunk is flushed; after
    a backfill that takes up to 30 minutes, so ask for the flush right away."""
    urllib.request.urlopen(urllib.request.Request(LOKI + "/flush", method="POST"), timeout=15).read()


def poll_site(cfg):
    """Read new completed runs; return how many were older than three hours."""
    old = 0
    runs = gh(f"repos/{cfg['repo']}/actions/runs?per_page=100")["workflow_runs"]
    runs = [r for r in runs if r["name"] == cfg["workflow"] and r["status"] == "completed"]
    seen = state["seen"].setdefault(cfg["site"], [])
    for run in sorted(runs, key=lambda r: r["updated_at"]):
        if run["id"] in seen:
            continue
        rec = build_record(cfg, run)
        push_loki(rec)
        old += time.time() - ts(rec["finished"]) > 3 * 3600
        with lock:
            seen.append(run["id"])
            del seen[:-500]
            key = f"{cfg['site']}|{rec['outcome']}"
            state["counts"][key] = state["counts"].get(key, 0) + 1
            prev = state["latest"].get(cfg["site"])
            if not prev or ts(rec["finished"]) >= ts(prev["finished"]):
                state["latest"][cfg["site"]] = rec
        log(f"{cfg['site']} run {rec['run_number']} {rec['outcome']} {rec['duration_s']}s gate={rec.get('gate')}")
    return old


def poll_probe():
    with urllib.request.urlopen(urllib.request.Request(PROBE_URL, headers={"User-Agent": "deploy-exporter"}), timeout=20) as r:
        d = json.load(r)
    with lock:
        state["probe"] = {"timestamp": d.get("timestamp"), "stance": d.get("fold_rate"),
                          "reference": d.get("reference_fold_rate"), "cost_usd": d.get("cost_usd")}


def save():
    tmp = STATE_FILE + ".tmp"
    with lock:
        json.dump(state, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)


def loop():
    while True:
        ok, old = True, 0
        for cfg in SITES:
            try:
                old += poll_site(cfg)
            except Exception as e:
                ok = False
                state["errors"] += 1
                log("poll failed", cfg["site"], e)
        if old and LOKI:
            try:
                flush_loki()
            except Exception as e:
                log("loki flush failed", e)
        try:
            poll_probe()
        except Exception as e:
            log("probe fetch failed", e)
        if ok:
            state["last_poll"] = time.time()
        try:
            save()
        except Exception as e:
            log("state save failed", e)
        time.sleep(POLL)


def metrics():
    out = []

    def m(name, kind, help_, rows):
        out.append(f"# HELP {name} {help_}\n# TYPE {name} {kind}")
        for labels, v in rows:
            lab = ",".join(f'{k}="{str(val)}"' for k, val in labels.items())
            out.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")

    with lock:
        latest, counts, probe = dict(state["latest"]), dict(state["counts"]), state["probe"]
        m("site_deploy_runs_total", "counter", "Completed deploy runs seen, by outcome (deployed, blocked by the gate, failed, cancelled).",
          [({"site": k.split("|")[0], "outcome": k.split("|")[1]}, v) for k, v in sorted(counts.items())])
        m("site_deploy_last_finished_timestamp_seconds", "gauge", "When the latest deploy run finished.",
          [({"site": s}, ts(r["finished"])) for s, r in latest.items()])
        m("site_deploy_last_duration_seconds", "gauge", "Wall time of the latest deploy run, start to finish.",
          [({"site": s}, r["duration_s"]) for s, r in latest.items()])
        m("site_deploy_last_deployed", "gauge", "1 if the latest run deployed, 0 if it was blocked, failed or cancelled.",
          [({"site": s}, int(r["outcome"] == "deployed")) for s, r in latest.items()])
        m("site_deploy_last_job_duration_seconds", "gauge", "Job durations in the latest run.",
          [({"site": s, "gh_job": j["name"]}, j["duration_s"]) for s, r in latest.items() for j in r["jobs"]])
        m("site_conformity_check_state", "gauge", "Gate check in the latest run: 0 fail, 1 pending, 2 partial, 3 pass.",
          [({"site": s, "check": c}, CHECK_STATE.get(v, -1)) for s, r in latest.items() for c, v in sorted(r.get("checks", {}).items())])
        m("site_conformity_findings_open", "gauge", "Open findings after the latest gate run.",
          [({"site": s}, r["findings_open"]) for s, r in latest.items() if "findings_open" in r])
        mp = [(s, r["map"]) for s, r in latest.items() if r.get("map")]
        for key, src, help_ in (
                ("nodes", "shipped_nodes", "Nodes in the map snapshot the latest deploy shipped."),
                ("links", "shipped_links", "Links in the map snapshot the latest deploy shipped."),
                ("carried_links", "carried_links", "Links carried forward from the previous snapshot because their pages were unreachable from the runner."),
                ("dropped_pages", "dropped_pages", "Pages the crawler dropped because they did not answer with HTML.")):
            m(f"site_map_{key}", "gauge", help_, [({"site": s}, v[src]) for s, v in mp if src in v])
        if probe:
            m("site_probe_fold_ratio", "gauge", "Decision-layer probe: share of WAIT runs that folded under pushback, by arm.",
              [({"arm": a}, probe[a]) for a in ("stance", "reference") if probe.get(a) is not None])
            m("site_probe_last_timestamp_seconds", "gauge", "When the latest decision-layer probe ran.",
              [({}, ts(probe["timestamp"]))] if probe.get("timestamp") else [])
        m("site_deploy_exporter_last_poll_timestamp_seconds", "gauge", "Last poll in which every repo answered.", [({}, state["last_poll"])])
        m("site_deploy_exporter_errors_total", "counter", "Failed polls since start.", [({}, state["errors"])])
    return "\n".join(out) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = metrics().encode() if self.path.startswith("/metrics") else b"deploy-exporter: see /metrics\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    try:
        saved = json.load(open(STATE_FILE))
        state.update({k: saved[k] for k in ("seen", "counts", "latest", "probe") if k in saved})
        log(f"state loaded: {sum(len(v) for v in state['seen'].values())} runs seen")
    except FileNotFoundError:
        log("no state yet; backfilling from the GitHub API")
    if not TOKEN:
        log("GITHUB_TOKEN not set: private repos and job logs will fail")
    threading.Thread(target=loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
