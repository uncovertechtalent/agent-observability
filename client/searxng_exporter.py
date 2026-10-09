#!/usr/bin/env python3
"""SearXNG exporter: pushes the health of a self-hosted SearXNG to the stack as OTLP metrics.

Runs on the machine that hosts SearXNG (a laptop here, so push instead of scrape: its
address changes and it sleeps). Every 30 s it reads four sources and posts one OTLP/HTTP
JSON batch to Alloy, which forwards it to Prometheus:

  1. GET /           -> searxng_up, probe latency
  2. GET /metrics    -> per-engine upstream requests from every client, reliability
     GET /stats/errors  per-engine error share by error class (since SearXNG started)
  3. the proxy       -> whether the second egress path answers with the expected exit IP
  4. events.jsonl    -> one line per call of the search wrapper (no query text): outcome,
                        cache hit, latency, results, pacing skips, per-engine status

Standard library only; Python 3.9+. Configuration by environment; install steps in the README.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode

OTLP = os.environ.get("OTLP_ENDPOINT", "http://localhost:4318").rstrip("/") + "/v1/metrics"
SEARXNG = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8080").rstrip("/")
EVENTS = os.path.expanduser(os.environ.get("SEARXNG_EVENTS", "~/.cache/searxng-search/events.jsonl"))
PROXY = os.environ.get("SEARXNG_PROXY", "")              # e.g. http://proxy-host:port; empty = no proxy probe
PROXY_EXIT_IP = os.environ.get("SEARXNG_PROXY_EXIT_IP", "")
CONTAINER = os.environ.get("SEARXNG_CONTAINER", "searxng-local")
INTERVAL = float(os.environ.get("INTERVAL", "30"))
ENV_FILE = os.path.expanduser(os.environ.get("SEARXNG_EXPORTER_ENV", "~/.config/searxng-exporter/env"))
UTILITY = {"currency", "wikidata", "lingva", "dictzone", "mymemory translated"}
STATE_TTL = 1800  # drop an engine's last-seen state after 30 min without a call

DURATION_BOUNDS = [0.25, 0.5, 1, 2, 3, 4, 6, 8, 10, 15, 20, 30]
RESULT_BOUNDS = [0, 1, 5, 10, 15, 20, 30, 50]
STATE_CODE = {"ok": 0, "empty": 1, "rate_limited": 2, "blocked": 3, "timeout": 4, "error": 5}


def load_env_file():
    if os.environ.get("SEARXNG_METRICS_PASSWORD") or not os.path.exists(ENV_FILE):
        return
    for line in open(ENV_FILE):
        k, _, v = line.strip().partition("=")
        if k and not k.startswith("#"):
            os.environ.setdefault(k, v)


def http(url, timeout=5, auth=None, proxy=None):
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + b64encode(auth.encode()).decode())
    t0 = time.time()
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, "", time.time() - t0
    except Exception:
        return 0, "", time.time() - t0


def classify(reason):
    """SearXNG's unresponsive-engine reason -> (status, suspended)."""
    r = reason.lower()
    if "too many" in r:
        s = "rate_limited"
    elif "captcha" in r or "denied" in r or "403" in r:
        s = "blocked"
    elif "timeout" in r:
        s = "timeout"
    else:
        s = "error"
    return s, r.startswith("suspended")


def error_class(err):
    """One /stats/errors entry -> a short label such as 'TooManyRequests' or 'HTTPStatusError 500'."""
    cls = err.get("exception_classname") or "message"
    cls = cls.rsplit(".", 1)[-1].replace("SearxEngine", "").replace("Exception", "") or "Error"
    params = err.get("log_parameters") or []
    if cls == "HTTPStatusError" and params:
        cls += f" {params[0]}"
    elif cls == "AccessDenied" and params:
        m = re.search(r"HTTP error (\d+)", str(params[0]))
        if m:
            cls += f" {m.group(1)}"
    return cls


class Histogram:
    def __init__(self, bounds):
        self.bounds, self.counts, self.sum, self.n = bounds, [0] * (len(bounds) + 1), 0.0, 0

    def observe(self, v):
        i = next((i for i, b in enumerate(self.bounds) if v <= b), len(self.bounds))
        self.counts[i] += 1
        self.sum += v
        self.n += 1


class Exporter:
    def __init__(self):
        self.start_ns = time.time_ns()
        self.counters = {}       # (name, unit, labels-tuple) -> value
        self.hists = {}          # (name, unit, labels-tuple) -> Histogram
        self.engine_state = {}   # engine -> (ts, status)
        self.events_pos = None   # (inode, offset)
        self.upstream = {}       # engine -> (value, start_ns), SearXNG's own counter
        self.egress = {}
        self.egress_at = 0
        self.proxy = None
        self.proxy_at = 0

    # --- sources -------------------------------------------------------------------------

    def inc(self, name, labels, v=1.0, unit=""):
        key = (name, unit, tuple(sorted(labels.items())))
        self.counters[key] = self.counters.get(key, 0.0) + v

    def zero_series(self, engines):
        """Start the counters alerts read at 0, so increase() also counts their first event."""
        for cache in ("hit", "miss"):
            for outcome in ("ok", "empty", "degraded"):
                self.inc("searxng_searches", {"kind": "search", "cache": cache, "outcome": outcome}, 0)
        for e in engines:
            for status in STATE_CODE:
                for suspended in (("false",) if status in ("ok", "empty") else ("false", "true")):
                    self.inc("searxng_engine_calls", {"engine": e, "status": status, "suspended": suspended}, 0)

    def hist(self, name, bounds, labels, v, unit=""):
        key = (name, unit, tuple(sorted(labels.items())))
        self.hists.setdefault(key, Histogram(bounds)).observe(v)

    def read_events(self):
        try:
            st = os.stat(EVENTS)
        except OSError:
            return
        if self.events_pos is None:              # start at the end: no replay of old history
            self.events_pos = (st.st_ino, st.st_size)
            return
        ino, off = self.events_pos
        if st.st_ino != ino or st.st_size < off:  # rotated or truncated
            off = 0
        with open(EVENTS, "rb") as f:
            f.seek(off)
            data = f.read()
        end = data.rfind(b"\n") + 1               # only complete lines
        self.events_pos = (st.st_ino, off + end)
        for line in data[:end].splitlines():
            try:
                self.event(json.loads(line))
            except (ValueError, KeyError, TypeError):
                continue

    def event(self, ev):
        kind, cache, outcome = ev.get("kind", "search"), ev.get("cache", "miss"), ev.get("outcome", "ok")
        self.inc("searxng_searches", {"kind": kind, "cache": cache, "outcome": outcome})
        if cache != "miss" or outcome == "down":
            return
        if kind == "search":
            if "latency_s" in ev:
                self.hist("searxng_search_duration", DURATION_BOUNDS, {}, float(ev["latency_s"]), unit="s")
            self.hist("searxng_search_results", RESULT_BOUNDS, {}, float(ev.get("results", 0)))
            self.inc("searxng_search_wait", {}, float(ev.get("wait_s", 0)), unit="s")
            for e in ev.get("skipped", []):
                self.inc("searxng_engine_skipped", {"engine": e})
        unresp = {u[0]: u[1] for u in ev.get("unresponsive", [])}
        per_engine = ev.get("engine_results", {})
        for e, n in per_engine.items():
            self.inc("searxng_engine_results", {"engine": e}, float(n))
        ts = float(ev.get("ts", time.time()))
        for e in ev.get("engines", []):
            if e in unresp:
                status, suspended = classify(unresp[e])
            else:
                status, suspended = ("ok" if per_engine.get(e) else "empty"), False
            self.inc("searxng_engine_calls", {"engine": e, "status": status, "suspended": str(suspended).lower()})
            if ts >= self.engine_state.get(e, (0, ""))[0]:
                self.engine_state[e] = (ts, status)

    def read_egress(self):
        """Which engines go through the proxy, from the live settings.yml (every 5 min)."""
        if time.time() - self.egress_at < 300:
            return
        self.egress_at = time.time()
        try:
            text = subprocess.run(["docker", "exec", CONTAINER, "cat", "/etc/searxng/settings.yml"],
                                  capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return
        egress, name = {}, None
        for line in text.splitlines():
            m = re.match(r"\s*-\s*name:\s*(.+?)\s*$", line)
            if m:
                name = m.group(1).strip("'\"")
                egress[name] = "direct"
            elif name and re.match(r"\s+proxies:", line):
                egress[name] = "proxy"
        if egress:
            self.egress = egress

    def probe_proxy(self):
        if not PROXY or time.time() - self.proxy_at < 60:
            return
        self.proxy_at = time.time()
        code, body, took = http("https://checkip.amazonaws.com", timeout=10, proxy=PROXY)
        ok = code == 200 and (not PROXY_EXIT_IP or body.strip() == PROXY_EXIT_IP)
        self.proxy = (1.0 if ok else 0.0, took)

    # --- OTLP ----------------------------------------------------------------------------

    @staticmethod
    def attrs(labels):
        return [{"key": k, "value": {"stringValue": str(v)}} for k, v in sorted(dict(labels).items())]

    def collect(self):
        now = str(time.time_ns())
        start = str(self.start_ns)
        metrics = []

        def gauge(name, points, unit=""):
            if points:
                metrics.append({"name": name, "unit": unit, "gauge": {"dataPoints": [
                    {"attributes": self.attrs(l), "timeUnixNano": now, "asDouble": float(v)} for l, v in points]}})

        def counter(name, points, unit=""):
            if points:
                metrics.append({"name": name, "unit": unit, "sum": {"aggregationTemporality": 2, "isMonotonic": True,
                                "dataPoints": [{"attributes": self.attrs(l), "startTimeUnixNano": s,
                                                "timeUnixNano": now, "asDouble": float(v)} for l, v, s in points]}})

        code, _, took = http(SEARXNG + "/", timeout=4)
        up = code == 200
        gauge("searxng_up", [({}, 1 if up else 0)])
        gauge("searxng_probe_duration", [({}, took)], unit="s")

        if up:
            code, body, _ = http(SEARXNG + "/config", timeout=5)
            engines = []
            if code == 200:
                engines = [e["name"] for e in json.loads(body)["engines"]
                           if e.get("enabled") and "general" in e.get("categories", []) and e["name"] not in UTILITY]
            gauge("searxng_engine_info", [({"engine": e, "egress": self.egress.get(e, "direct")}, 1) for e in engines])
            self.zero_series(engines)

            pw = os.environ.get("SEARXNG_METRICS_PASSWORD", "")
            code, body, _ = http(SEARXNG + "/metrics", timeout=5, auth=f"exporter:{pw}") if pw else (0, "", 0)
            reqs, rel = [], []
            for line in body.splitlines():
                m = re.match(r'(searxng_engines_\w+)\{engine_name="([^"]+)"\}\s+(\S+)', line)
                if not m:
                    continue
                metric, e, v = m.group(1), m.group(2), float(m.group(3))
                if metric == "searxng_engines_request_count_total":
                    prev = self.upstream.get(e)
                    s = prev[1] if prev and v >= prev[0] else str(time.time_ns())
                    self.upstream[e] = (v, s)
                    reqs.append(({"engine": e}, v, s))
                elif metric == "searxng_engines_reliability_total":
                    rel.append(({"engine": e}, v / 100))
            counter("searxng_engine_upstream_requests", reqs)
            gauge("searxng_engine_reliability", rel)

            code, body, _ = http(SEARXNG + "/stats/errors", timeout=5)
            errs = {}
            if code == 200:
                for e, items in json.loads(body).items():
                    for it in items:
                        if it.get("secondary"):
                            continue
                        key = (e, error_class(it))
                        errs[key] = errs.get(key, 0) + it.get("percentage", 0) / 100
            gauge("searxng_engine_error_ratio", [({"engine": e, "error": c}, v) for (e, c), v in errs.items()])

        if self.proxy is not None:
            gauge("searxng_proxy_up", [({}, self.proxy[0])])
            gauge("searxng_proxy_probe_duration", [({}, self.proxy[1])], unit="s")

        cutoff = time.time() - STATE_TTL
        live = {e: v for e, v in self.engine_state.items() if v[0] >= cutoff}
        gauge("searxng_engine_state", [({"engine": e}, STATE_CODE[s]) for e, (_, s) in live.items()])
        gauge("searxng_engine_state_age", [({"engine": e}, time.time() - ts) for e, (ts, _) in live.items()], unit="s")

        by_name = {}
        for (name, unit, labels), v in self.counters.items():
            by_name.setdefault((name, unit), []).append((labels, v, start))
        for (name, unit), pts in by_name.items():
            counter(name, pts, unit)
        for (name, unit, labels), h in self.hists.items():
            metrics.append({"name": name, "unit": unit, "histogram": {"aggregationTemporality": 2, "dataPoints": [{
                "attributes": self.attrs(labels), "startTimeUnixNano": start, "timeUnixNano": now,
                "count": str(h.n), "sum": h.sum, "bucketCounts": [str(c) for c in h.counts],
                "explicitBounds": h.bounds}]}})
        return metrics

    def push(self, metrics):
        body = json.dumps({"resourceMetrics": [{
            "resource": {"attributes": self.attrs({"service.name": "searxng-exporter", "service.instance.id": CONTAINER})},
            "scopeMetrics": [{"scope": {"name": "searxng-exporter", "version": "1"}, "metrics": metrics}]}]}).encode()
        req = urllib.request.Request(OTLP, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    def run(self):
        print(f"searxng-exporter: {SEARXNG} -> {OTLP} every {INTERVAL:.0f}s", flush=True)
        failing = False
        while True:
            t0 = time.time()
            try:
                self.read_events()
                self.read_egress()
                self.probe_proxy()
                self.push(self.collect())
                if failing:
                    print("push recovered", flush=True)
                failing = False
            except Exception as ex:  # keep running through sleep, network loss, SearXNG restarts
                if not failing:
                    print(f"push failed: {type(ex).__name__}: {ex}", file=sys.stderr, flush=True)
                failing = True
            time.sleep(max(1.0, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    load_env_file()
    if "--once" in sys.argv:
        x = Exporter()
        x.read_egress()
        x.probe_proxy()
        print(json.dumps(x.collect(), indent=1)[:4000])
    else:
        Exporter().run()
