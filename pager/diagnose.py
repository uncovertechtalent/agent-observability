"""The responder agent: a runbook link and a read-only diagnosis for an incident's thread.

Read-only by construction. Its only network calls are GET /api/v1/query against the Prometheus
servers named in PROMETHEUS_URLS; it writes nowhere and returns text that bot.py posts in the
incident thread. Label values go into PromQL only after a strict pattern check.
"""

import re
import urllib.parse

NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
VALUE = re.compile(r"[A-Za-z0-9_.:/-]{1,100}")
MAX_EXPR = 2000


def runbook(inc, runbooks):
    for entry in runbooks:
        if re.fullmatch(entry["match"], inc.alertname):
            return entry["url"]
    return None


def expr_from(generator_url):
    """The alert expression from Prometheus's generator URL (g0.expr), or None."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(generator_url or "").query)
    except ValueError:
        return None
    expr = (q.get("g0.expr") or [None])[0]
    return expr if expr and len(expr) <= MAX_EXPR else None


def fmt_series(result, limit=5):
    out = []
    for r in result[:limit]:
        labels = {k: v for k, v in r.get("metric", {}).items() if k != "__name__"}
        lab = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        out.append(f"  {{{lab}}} {r.get('value', [None, '?'])[1]}")
    if len(result) > limit:
        out.append(f"  ... {len(result) - limit} more")
    return out


async def diagnose(inc, query, runbooks):
    """query(prometheus_name, promql) -> Prometheus 'result' list; may raise. Returns the message."""
    name = inc.alertname if NAME.fullmatch(inc.alertname or "") else None
    lines = [f"{inc.alertname} on {inc.prometheus or 'unknown'} Prometheus, severity {inc.severity}"]

    async def ask(label, promql):
        try:
            res = await query(inc.prometheus, promql)
        except Exception as e:  # a failed query is part of the diagnosis
            lines.append(f"{label}: query failed ({type(e).__name__})")
            return None
        return res

    expr = expr_from(inc.generator_url)
    if expr:
        res = await ask("expression now", expr)
        if res is not None:
            lines.append(f"expression now: {len(res)} series match")
            lines += fmt_series(res)
    if name:
        res = await ask("firing in the last 24h", f'count_over_time(ALERTS{{alertname="{name}",alertstate="firing"}}[24h:1m])')
        if res is not None:
            minutes = max((float(r["value"][1]) for r in res), default=0)
            lines.append(f"firing in the last 24h: about {int(minutes)} minutes")
    job = (inc.labels or {}).get("job")
    if job and VALUE.fullmatch(job):
        res = await ask(f'up{{job="{job}"}}', f'up{{job="{job}"}}')
        if res is not None:
            lines.append(f'up{{job="{job}"}}:')
            lines += fmt_series(res) or ["  no series"]
    res = await ask("other alerts firing", 'ALERTS{alertstate="firing"}')
    if res is not None:
        others = sorted({r["metric"].get("alertname", "?") for r in res} - {inc.alertname, "Watchdog"})
        lines.append("other alerts firing: " + (", ".join(others) if others else "none"))

    link = runbook(inc, runbooks)
    text = "\n".join(lines)[:1500]
    return (f"**Responder agent** (read-only diagnosis)\n"
            f"Runbook: {link or 'none listed'}\n```text\n{text}\n```\n"
            f"Prometheus queries only; nothing was changed. A person still has to acknowledge.")
