"""alert-issues: Alertmanager's ticket receiver. Keeps one GitHub issue per alert.

Alertmanager posts a group of alerts to /alerts; each alert carries a fingerprint, a hash of
its labels. The first firing notification for a fingerprint opens an issue. Later ones update
it: a repeat refreshes the body, a new firing episode reopens a closed issue with a comment,
and a resolved notification comments and closes it. An issue a person closed stays closed
until the alert fires again in a new episode. An alert that resolves while inhibited or
silenced sends no resolved notification, so an hourly sweep closes an issue that has heard
nothing for 25 hours, and the next firing reopens it. The fingerprint-to-issue map lives in
STATE_PATH; when it is lost, the receiver finds an issue again by the fingerprint in its body.

The board repository is public, so an issue shows only the alert name, the summary
annotation and an allowlist of labels. Descriptions, instance addresses and generator URLs
stay out, and every shown value loses IPv4 addresses and URLs and cannot mention a person or
link an issue.

A failed GitHub call answers 500, and Alertmanager retries. Stdlib only.
"""

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("alert-issues")

REPO = os.environ.get("GITHUB_REPO", "uncovertechtalent/machinebehavior.io")
TOKEN_FILE = os.environ.get("GITHUB_TOKEN_FILE", "/secrets/github_token")
API = os.environ.get("GITHUB_API", "https://api.github.com")
STATE_PATH = os.environ.get("STATE_PATH", "/data/state.json")
PORT = int(os.environ.get("PORT", "9095"))
DOCS = "https://machinebehavior.io/inside/docs/obs/on-call/"

KIND_LABEL = "kind:finding"
DEFAULT_AREA = "operations"
AREAS = {"ai", "delivery", "direction", "knowledge", "money", "operations", "people", "security"}
# An alert's own "area" label (area="money") wins; otherwise the first matching name pattern.
AREA_BY_NAME = [
    (re.compile(r"AWSCost.*|ClaudeCodeSpend.*"), "money"),
    (re.compile(r"LocalLLM.*|Ollama.*|ModelSpilledToCPU"), "ai"),
    (re.compile(r"AgentTool.*"), "security"),
]
SHOWN_LABELS = ["severity", "prometheus", "job", "component", "name", "engine", "model", "source", "processor"]
TITLE_LABELS = ["name", "engine", "model", "source"]
FINGERPRINT = re.compile(r"[0-9a-f]{8,64}")
MAX_REQUEST = 1 << 20
URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
# Longer than the board route's repeat_interval (24h) plus its group_interval, with margin.
STALE_AFTER = timedelta(hours=25)
SWEEP_EVERY = 3600
STARTED = re.compile(r"Alert fingerprint: ([0-9a-f]+), firing since ([^\s<]+)")


class GitHub:
    """The four issue calls the receiver needs, plus a search for lost state."""

    def __init__(self, repo=REPO, token_file=TOKEN_FILE, api=API):
        self.repo, self.token_file, self.api = repo, token_file, api

    def _token(self):
        try:
            with open(self.token_file) as f:
                token = f.read().strip()
        except OSError:
            token = ""
        if not token:
            raise RuntimeError(f"no GitHub token in {self.token_file}")
        return token

    def _call(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "alert-issues",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read()
        return json.loads(body) if body else None

    def create_issue(self, title, body, labels):
        return self._call("POST", f"/repos/{self.repo}/issues",
                          {"title": title, "body": body, "labels": labels})["number"]

    def get_issue(self, number):
        try:
            return self._call("GET", f"/repos/{self.repo}/issues/{number}")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):  # deleted or transferred: open a new one
                return None
            raise

    def update_issue(self, number, **fields):
        self._call("PATCH", f"/repos/{self.repo}/issues/{number}", fields)

    def comment(self, number, body):
        self._call("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def find_issue(self, fingerprint):
        q = urllib.parse.quote(f'repo:{self.repo} is:issue in:body "{fingerprint}"')
        items = self._call("GET", f"/search/issues?q={q}&per_page=5").get("items", [])
        return items[0]["number"] if items else None


class State:
    """fingerprint -> {"issue": number, "starts_at": the firing episode it last saw}"""

    def __init__(self, path=STATE_PATH):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, fingerprint):
        return self.data.get(fingerprint)

    def put(self, fingerprint, record):
        self.data[fingerprint] = record
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


def when(ts):
    """Alertmanager's RFC 3339 time, any offset, to '2026-10-17 13:40 UTC'; '' for the zero time."""
    if not ts or ts.startswith("0001-"):
        return ""
    # Python before 3.11 takes neither "Z" nor nine fraction digits; cut to six.
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)", ts)
    if not m:
        return ""
    t = datetime.fromisoformat(m[1] + (m[2] or "")[:7] + ("+00:00" if m[3] == "Z" else m[3]))
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def scrub(text, limit):
    """What a public repository may show: no URL, no IPv4 address, no @mention, no #issue link."""
    text = str(text)[:limit].replace("\n", " ")
    text = URL.sub("[link removed]", text)
    text = IPV4.sub("[address removed]", text)
    # A zero-width space after "@" or "#" stops GitHub's mention and issue links.
    return text.replace("@", "@\u200b").replace("#", "#\u200b")


def code(value, limit=120):
    return "`" + scrub(value, limit).replace("`", "'") + "`"


def plain(text, limit=500):
    return scrub(text, limit)


def area_label(labels):
    area = labels.get("area", "")
    if area not in AREAS:
        name = labels.get("alertname", "")
        area = next((a for pattern, a in AREA_BY_NAME if pattern.fullmatch(name)), DEFAULT_AREA)
    return f"area:{area}"


def title(labels):
    parts = [scrub(labels[k], 40) for k in TITLE_LABELS if labels.get(k)]
    suffix = f" ({', '.join(parts)})" if parts else ""
    return f"Alert: {scrub(labels.get('alertname', 'unnamed'), 100)}{suffix}"[:200]


PAGER_FIELDS = {"alert": str, "severity": str, "prometheus": str, "fired": str, "acknowledged": str,
                "acknowledged_by": str, "resolved": str, "resolved_by": str, "responder_ran": bool,
                "time_to_acknowledge_s": int, "time_to_resolve_s": int}
PAGER_START, PAGER_END = "<!-- pager:start -->", "<!-- pager:end -->"
PAGER_EPISODES = 20  # kept per issue, so a flapping page cannot grow the body past GitHub's limit


def span(seconds):
    m = int(seconds or 0) // 60
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"


def clean_view(view):
    """The pager's redacted record of one page, checked field by field (see pager/core.py public_view)."""
    if not isinstance(view, dict):
        raise ValueError("pager record is not an object")
    out = {}
    for key, kind in PAGER_FIELDS.items():
        value = view.get(key)
        if value is None:
            out[key] = None
        elif not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ValueError(f"pager.{key} has the wrong type")
        else:
            out[key] = scrub(value, 100) if kind is str else value
    if not out["fired"]:
        raise ValueError("pager.fired is missing")
    return out


def pager_block(pages):
    """The board issue's pager section: one line per page of this alert, and the same records as
    JSON in an HTML comment for the site build. Names and times only."""
    if not pages:
        return []
    views = sorted(pages.values(), key=lambda v: v["fired"])
    lines = [PAGER_START, "**Pager**", ""]
    for v in views[-10:]:
        parts = [f"fired {when(v['fired']) or v['fired']}"]
        if v.get("acknowledged"):
            parts.append(f"acknowledged by {v.get('acknowledged_by') or '?'} after {span(v.get('time_to_acknowledge_s'))}")
        if v.get("responder_ran"):
            parts.append("responder diagnosis posted")
        if v.get("resolved"):
            parts.append(f"resolved by {v.get('resolved_by') or '?'} after {span(v.get('time_to_resolve_s'))}")
        lines.append("- " + ", ".join(parts))
    record = json.dumps(views, separators=(",", ":"), sort_keys=True).replace("--", "-\\u002d")
    return lines + ["", f"<!-- pager-record {record} -->", PAGER_END, ""]


def with_pager_block(text, pages):
    block = "\n".join(pager_block(pages))
    if PAGER_START in text and PAGER_END in text:
        head, rest = text.split(PAGER_START, 1)
        tail = rest.split(PAGER_END, 1)[1].lstrip("\n")
        return head + block + tail
    marker = "Opened by `alert-issues`"
    return text.replace(marker, block + marker, 1) if marker in text else text + "\n\n" + block


def body(alert, now, pages=None):
    labels = alert.get("labels") or {}
    summary = (alert.get("annotations") or {}).get("summary", "")
    firing = alert.get("status") == "firing"
    lines = [f"**{code(labels.get('alertname', 'unnamed'))}** {'is firing' if firing else 'has resolved'}.", ""]
    if summary:
        lines += [f"> {plain(summary)}", ""]
    lines += ["| Label | Value |", "|---|---|"]
    lines += [f"| {k} | {code(labels[k])} |" for k in SHOWN_LABELS if labels.get(k)]
    lines += [
        "",
        f"- Started: {when(alert.get('startsAt')) or 'unknown'}",
        f"- Resolved: {when(alert.get('endsAt')) if not firing else 'not yet'}",
        f"- Last word from Alertmanager: {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        *pager_block(pages),
        "Opened by `alert-issues`, the ticket receiver of Alertmanager "
        f"([on-call routing]({DOCS})). This issue closes when the alert resolves and reopens "
        "when it fires again.",
        "",
        # Read back when the state file is lost, so a person's close survives it.
        f"<sub>Alert fingerprint: {alert['fingerprint']}, firing since {alert.get('startsAt') or '?'}</sub>",
    ]
    return "\n".join(lines)


class Receiver:
    def __init__(self, github, state, clock=lambda: datetime.now(timezone.utc)):
        self.gh, self.state, self.clock = github, state, clock

    def handle(self, payload):
        alerts = payload.get("alerts")
        if not isinstance(alerts, list):
            raise ValueError("payload has no alerts list")
        return [self.handle_alert(a) for a in alerts]

    def handle_alert(self, alert):
        fp = str(alert.get("fingerprint", ""))
        if not FINGERPRINT.fullmatch(fp):
            raise ValueError(f"bad fingerprint {fp!r}")
        labels = alert.get("labels") or {}
        status, starts_at = alert.get("status"), alert.get("startsAt")
        now = self.clock()

        record = self.state.get(fp)
        if record is None:
            found = self.gh.find_issue(fp)
            record = {"issue": found, "starts_at": None} if found else None
        issue = self.gh.get_issue(record["issue"]) if record else None
        if issue and record.get("starts_at") is None:
            m = STARTED.search(issue.get("body") or "")
            if m and m[1] == fp:
                record["starts_at"] = m[2]

        pages = (record or {}).get("pages")

        def seen(number, is_open):
            kept = self.state.get(fp) or {}
            self.state.put(fp, {**kept, "issue": number, "starts_at": starts_at, "open": is_open,
                                "last_seen": now.isoformat(), "swept": False})

        if status == "firing":
            if issue is None:
                number = self.gh.create_issue(title(labels), body(alert, now, pages), [KIND_LABEL, area_label(labels)])
                seen(number, True)
                return f"opened #{number}"
            number = issue["number"]
            new_episode = record.get("starts_at") != starts_at
            if issue["state"] == "closed":
                if not new_episode and not record.get("swept"):
                    seen(number, False)
                    return f"left #{number} closed"  # a person closed it during this episode
                # State changes before comments: a retry after a failed comment loses the
                # comment, never repeats it.
                self.gh.update_issue(number, state="open", body=body(alert, now, pages))
                seen(number, True)
                self.gh.comment(number, f"Firing again since {when(starts_at) or 'now'}.")
                return f"reopened #{number}"
            self.gh.update_issue(number, body=body(alert, now, pages))
            seen(number, True)
            return f"updated #{number}"

        if status == "resolved":
            if issue is None:
                return "resolved, no issue"
            number = issue["number"]
            if issue["state"] != "open":
                seen(number, False)
                return f"#{number} already closed"
            self.gh.update_issue(number, state="closed", state_reason="completed", body=body(alert, now, pages))
            seen(number, False)
            self.gh.comment(number, f"Resolved at {when(alert.get('endsAt')) or 'now'}.")
            return f"closed #{number}"

        raise ValueError(f"unknown status {status!r}")

    def pager_event(self, payload):
        """The pager's redacted record of a page: kept in the state and on the board issue, with a
        comment when the page is acknowledged, handed to the responder, or resolved by a person."""
        fp = str(payload.get("fingerprint", ""))
        if not FINGERPRINT.fullmatch(fp):
            raise ValueError(f"bad fingerprint {fp!r}")
        episode = str(payload.get("starts_at") or "")[:40]
        view = clean_view(payload.get("pager"))
        record = self.state.get(fp)
        if not record or not record.get("issue"):
            raise LookupError(f"no issue for {fp}")
        number = record["issue"]
        pages = dict(record.get("pages") or {})
        old = pages.get(episode) or {}
        if old == view:
            return f"#{number} unchanged"
        issue = self.gh.get_issue(number)
        if issue is None:
            raise LookupError(f"issue #{number} is gone")
        pages[episode] = view
        pages = dict(sorted(pages.items(), key=lambda kv: kv[1]["fired"])[-PAGER_EPISODES:])
        self.state.put(fp, {**record, "pages": pages})
        self.gh.update_issue(number, body=with_pager_block(issue.get("body") or "", pages))
        if view["acknowledged"] and not old.get("acknowledged"):
            self.gh.comment(number, f"Acknowledged in the pager by {view['acknowledged_by']}, "
                                    f"{span(view['time_to_acknowledge_s'])} after it fired.")
        if view["responder_ran"] and not old.get("responder_ran"):
            self.gh.comment(number, "The responder agent posted its runbook link and read-only diagnosis in the pager.")
        if view["resolved"] and not old.get("resolved") and not (view["resolved_by"] or "").startswith("Alertmanager"):
            self.gh.comment(number, f"Resolved in the pager by {view['resolved_by']}.")
        return f"#{number} pager record updated"

    def issue_url(self, fingerprint):
        record = self.state.get(fingerprint) if FINGERPRINT.fullmatch(fingerprint) else None
        return f"https://github.com/{self.gh.repo}/issues/{record['issue']}" if record and record.get("issue") else None

    def sweep(self, stale_after=STALE_AFTER):
        """Close open issues that have heard nothing for longer than the board route's repeat
        interval. An alert that resolves while inhibited or silenced never sends its resolved
        notification; this closes its issue anyway, and the next firing reopens it."""
        now, done = self.clock(), []
        for fp, record in sorted(self.state.data.items()):
            if not record.get("open") or not record.get("last_seen"):
                continue
            last = datetime.fromisoformat(record["last_seen"])
            if now - last < stale_after:
                continue
            number = record["issue"]
            issue = self.gh.get_issue(number)
            self.state.put(fp, {**record, "open": False, "swept": bool(issue and issue["state"] == "open")})
            if issue and issue["state"] == "open":
                self.gh.update_issue(number, state="closed", state_reason="completed")
                self.gh.comment(number, f"No word from Alertmanager since {last.strftime('%Y-%m-%d %H:%M UTC')}, "
                                        "so the alert most likely resolved while inhibited or silenced. "
                                        "This issue reopens if the alert fires again.")
                done.append(f"swept #{number}")
        return done


class Handler(BaseHTTPRequestHandler):
    receiver = None
    lock = threading.Lock()  # one notification at a time: no race between open and update

    def _reply(self, code_, text, content_type="text/plain"):
        data = text.encode()
        self.send_response(code_)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._reply(200, "ok\n")
        if self.path.startswith("/issues/"):  # for the pager's #tickets messages
            with self.lock:
                url = self.receiver.issue_url(self.path[len("/issues/"):])
            if url:
                return self._reply(200, json.dumps({"url": url}) + "\n", "application/json")
        self._reply(404, "not found\n")

    def do_POST(self):
        if self.path == "/events":
            return self._events()
        if self.path != "/alerts":
            return self._reply(404, "not found\n")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_REQUEST:
            return self._reply(413 if length > MAX_REQUEST else 400, "bad length\n")
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            return self._reply(400, "bad json\n")
        try:
            with self.lock:
                results = self.receiver.handle(payload)
        except ValueError as e:
            log.warning("rejected: %s", e)
            return self._reply(400, "bad payload\n")
        except Exception as e:  # GitHub down, bad token, rate limit: Alertmanager retries on 5xx
            log.error("failed: %s: %s", type(e).__name__, e)
            return self._reply(500, "failed\n")
        log.info("%s %s: %s", payload.get("receiver", "?"), payload.get("groupKey", "?"), ", ".join(results))
        self._reply(200, "ok\n")

    def _events(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_REQUEST:
            return self._reply(400, "bad length\n")
        try:
            payload = json.loads(self.rfile.read(length))
            with self.lock:
                result = self.receiver.pager_event(payload)
        except LookupError:
            return self._reply(404, "no issue yet\n")
        except (ValueError, AttributeError) as e:
            log.warning("pager event rejected: %s", e)
            return self._reply(400, "bad payload\n")
        except Exception as e:
            log.error("pager event failed: %s: %s", type(e).__name__, e)
            return self._reply(500, "failed\n")
        log.info("pager: %s", result)
        self._reply(200, "ok\n")

    def log_message(self, *args):
        pass  # the POST handler logs what it did; no access log


def sweeper(receiver):
    while True:
        threading.Event().wait(SWEEP_EVERY)
        try:
            with Handler.lock:
                done = receiver.sweep()
            if done:
                log.info("sweep: %s", ", ".join(done))
        except Exception as e:  # GitHub down: the next sweep tries again
            log.error("sweep failed: %s: %s", type(e).__name__, e)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    gh = GitHub()
    try:
        gh._token()
    except RuntimeError as e:
        log.warning("%s: every notification will fail until it is there", e)
    Handler.receiver = Receiver(gh, State())
    threading.Thread(target=sweeper, args=(Handler.receiver,), daemon=True).start()
    log.info("alert-issues on :%d for %s", PORT, REPO)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
