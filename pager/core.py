"""Pager core: the schedule, the escalation policy and the incident store. No Discord in here.

bot.py (the Discord side) and the Alertmanager webhook call into this module; the tests drive it
with a fixed clock. Every time is an aware datetime in UTC; the schedule is read in its own zone.

The public view of an incident (public_view) is the one redacted shape that leaves the pager: the
board issue's pager record uses it today, and a public feed channel could use it later. It holds
names from rotation.yml and times, never a Discord ID, a channel or a token. It is stream v1, listed
in the README: fields are only added; removing or renaming one makes v2.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# No firing notification for this long means the alert resolved unseen: the page route repeats every
# hour and the board route every 24 hours (alertmanager.yml), with margin.
STALE_AFTER = {"page": timedelta(hours=2, minutes=30), "ticket": timedelta(hours=25)}
KINDS = {"human", "agent"}


def utc(dt):
    return dt.astimezone(timezone.utc)


def iso(dt):
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def duration(s):
    m = re.fullmatch(r"(\d+)([smhd])", str(s).strip())
    if not m:
        raise ValueError(f"bad duration {s!r}; use 30s, 5m, 2h or 7d")
    unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m[2]]
    return timedelta(**{unit: int(m[1])})


def clock(s):
    h, m = str(s).split(":")
    return time(int(h), int(m))


# ---------------------------------------------------------------- schedule and policy

@dataclass
class Restriction:
    days: set
    start: time
    end: time  # end <= start runs past midnight, and belongs to the day it started

    def allows(self, local):
        t, d = local.time(), local.weekday()
        if self.start < self.end:
            return d in self.days and self.start <= t < self.end
        if t >= self.start:
            return d in self.days
        return t < self.end and (d - 1) % 7 in self.days


@dataclass
class Layer:
    name: str
    members: list
    start: datetime
    turn: timedelta
    restrictions: list

    def on_call(self, at, tz):
        if self.restrictions and not any(r.allows(at.astimezone(tz)) for r in self.restrictions):
            return None
        turns = max(0, (at - self.start) // self.turn)
        return self.members[int(turns) % len(self.members)]


@dataclass
class Rule:
    after: timedelta
    schedule: str


@dataclass
class Override:
    person: str
    start: datetime
    end: datetime
    schedule: str
    source: str = "file"


class Rotation:
    """rotation.yml, parsed and checked. Later layers win over earlier ones; overrides win over all."""

    def __init__(self, doc):
        self.tz = ZoneInfo(doc.get("timezone", "UTC"))
        self.people = {}
        for key, p in (doc.get("people") or {}).items():
            kind = p.get("kind", "human")
            if kind not in KINDS:
                raise ValueError(f"person {key}: kind must be human or agent")
            self.people[key] = {"name": p.get("name", key), "kind": kind}
        self.schedules = {}
        for name, s in (doc.get("schedules") or {}).items():
            layers = []
            for lay in s.get("layers") or []:
                members = lay.get("members") or []
                if not members or any(m not in self.people for m in members):
                    raise ValueError(f"schedule {name}, layer {lay.get('name')}: members must be people from the list")
                restrictions = []
                for r in lay.get("restrictions") or []:
                    days = {DAYS.index(d) for d in r.get("days", DAYS)}
                    restrictions.append(Restriction(days, clock(r["from"]), clock(r["to"])))
                start = datetime.fromisoformat(str(lay.get("start", "2026-01-05T00:00"))).replace(tzinfo=self.tz)
                layers.append(Layer(lay.get("name", name), members, utc(start), duration(lay.get("turn", "7d")), restrictions))
            self.schedules[name] = {"description": s.get("description", ""), "layers": layers}
        pol = doc.get("escalation_policy") or {}
        self.policy_name = pol.get("name", "default")
        self.rules = [Rule(duration(r["after"]), r["schedule"]) for r in pol.get("rules") or []]
        if not self.rules or any(r.schedule not in self.schedules for r in self.rules):
            raise ValueError("escalation_policy.rules must name schedules from the list")
        if [r.after for r in self.rules] != sorted(r.after for r in self.rules):
            raise ValueError("escalation_policy.rules must be in order of their delay")
        self.renotify_after = duration(pol.get("renotify_after", "5m"))
        self.renotify_limit = int(pol.get("renotify_limit", 3))
        self.overrides = [self.override(o, "file") for o in doc.get("overrides") or []]

    def override(self, o, source):
        person, schedule = o["person"], o.get("schedule", self.rules[0].schedule)
        if person not in self.people or schedule not in self.schedules:
            raise ValueError(f"override: unknown person {person!r} or schedule {schedule!r}")
        start = o["start"] if isinstance(o["start"], datetime) else datetime.fromisoformat(str(o["start"])).replace(tzinfo=self.tz)
        end = o["end"] if isinstance(o["end"], datetime) else datetime.fromisoformat(str(o["end"])).replace(tzinfo=self.tz)
        if end <= start:
            raise ValueError("override: end must be after start")
        return Override(person, utc(start), utc(end), schedule, source)

    def who(self, schedule, at, extra=()):
        for o in sorted([*self.overrides, *extra], key=lambda o: o.start, reverse=True):
            if o.schedule == schedule and o.start <= at < o.end:
                return o.person
        for layer in reversed(self.schedules[schedule]["layers"]):
            person = layer.on_call(at, self.tz)
            if person:
                return person
        return None

    def kind(self, person):
        return self.people[person]["kind"]

    def name(self, person):
        return self.people[person]["name"] if person in self.people else person


# ---------------------------------------------------------------- escalation

@dataclass
class Action:
    kind: str      # notify, renotify, agent
    person: str
    level: int
    reason: str = ""


def plan(inc, rotation, now, extra_overrides=()):
    """What the pager must do now for one incident. Pure: the caller performs the actions and
    records them (Store.record_action), so the next call does not repeat them.

    A rule is reached when its delay has passed, when a person pressed Escalate past it, or when
    every rule before it that is reached has nobody on call: out of hours the first layer is empty,
    so the page goes to the next one at once. A person who comes on call later while the page is
    still unacknowledged is notified then."""
    if inc.status != "triggered":
        return []
    elapsed = now - inc.created_at
    actions, nobody_so_far, handled = [], True, set()
    for i, rule in enumerate(rotation.rules):
        reached = elapsed >= rule.after or i <= inc.forced_level or (i > 0 and nobody_so_far)
        if not reached:
            break
        person = rotation.who(rule.schedule, now, extra_overrides)
        if person is None or person in handled:
            continue
        handled.add(person)
        if rotation.kind(person) == "human":
            nobody_so_far = False
            seen = inc.notified.get(person)
            if not seen:
                actions.append(Action("notify", person, i, "on call" if i == 0 else f"level {i + 1}"))
            elif seen["count"] < rotation.renotify_limit and now - parse_iso(seen["last"]) >= rotation.renotify_after:
                actions.append(Action("renotify", person, i, "not acknowledged"))
        else:
            if not inc.agent_done:
                actions.append(Action("agent", person, i, "escalated"))
    return actions


def level_reached(inc, rotation, now, extra_overrides=()):
    """Highest rule index that is reached, for the message ("level 2 of 2")."""
    elapsed, top, nobody_so_far = now - inc.created_at, 0, True
    for i, rule in enumerate(rotation.rules):
        if elapsed >= rule.after or i <= inc.forced_level or (i > 0 and nobody_so_far):
            top = i
            person = rotation.who(rule.schedule, now, extra_overrides)
            if person and rotation.kind(person) == "human":
                nobody_so_far = False
        else:
            break
    return top


# ---------------------------------------------------------------- incidents

@dataclass
class Incident:
    id: int
    kind: str              # page or ticket
    fingerprint: str
    starts_at: str
    alertname: str
    severity: str
    prometheus: str
    summary: str
    labels: dict
    generator_url: str
    status: str            # triggered, acknowledged, resolved
    created_at: datetime
    acked_at: datetime = None
    acked_by: str = None
    resolved_at: datetime = None
    resolved_by: str = None
    forced_level: int = -1
    agent_done: bool = False
    notified: dict = field(default_factory=dict)
    message_id: int = None
    thread_id: int = None
    last_seen: datetime = None  # the last firing notification from Alertmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY, kind TEXT, fingerprint TEXT, starts_at TEXT, alertname TEXT, severity TEXT,
  prometheus TEXT, summary TEXT, labels TEXT, generator_url TEXT, status TEXT, created_at TEXT,
  acked_at TEXT, acked_by TEXT, resolved_at TEXT, resolved_by TEXT, forced_level INTEGER DEFAULT -1,
  agent_done INTEGER DEFAULT 0, notified TEXT DEFAULT '{}', message_id INTEGER, thread_id INTEGER, last_seen TEXT);
CREATE INDEX IF NOT EXISTS incidents_fp ON incidents (fingerprint, kind);
CREATE TABLE IF NOT EXISTS events (incident_id INTEGER, at TEXT, kind TEXT, actor TEXT, detail TEXT, posted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS people (person TEXT PRIMARY KEY, discord_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS overrides (id INTEGER PRIMARY KEY, person TEXT, schedule TEXT, start TEXT, "end" TEXT, created_by TEXT);
"""
COLUMNS = ["id", "kind", "fingerprint", "starts_at", "alertname", "severity", "prometheus", "summary", "labels",
           "generator_url", "status", "created_at", "acked_at", "acked_by", "resolved_at", "resolved_by",
           "forced_level", "agent_done", "notified", "message_id", "thread_id", "last_seen"]


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)
        if "last_seen" not in {r[1] for r in self.db.execute("PRAGMA table_info(incidents)")}:
            self.db.execute("ALTER TABLE incidents ADD COLUMN last_seen TEXT")  # databases from before the sweep

    def close(self):
        self.db.close()

    # incidents
    def _row(self, row):
        if not row:
            return None
        d = dict(zip(COLUMNS, row))
        for k in ("created_at", "acked_at", "resolved_at", "last_seen"):
            d[k] = parse_iso(d[k])
        d["labels"], d["notified"] = json.loads(d["labels"] or "{}"), json.loads(d["notified"] or "{}")
        d["agent_done"] = bool(d["agent_done"])
        return Incident(**d)

    def get(self, incident_id):
        return self._row(self.db.execute(f"SELECT {','.join(COLUMNS)} FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def latest(self, fingerprint, kind):
        return self._row(self.db.execute(
            f"SELECT {','.join(COLUMNS)} FROM incidents WHERE fingerprint=? AND kind=? ORDER BY id DESC LIMIT 1",
            (fingerprint, kind)).fetchone())

    def open_incidents(self, kind="page"):
        rows = self.db.execute(f"SELECT {','.join(COLUMNS)} FROM incidents WHERE kind=? AND status!='resolved' ORDER BY id",
                               (kind,)).fetchall()
        return [self._row(r) for r in rows]

    def recent(self, limit=50):
        rows = self.db.execute(f"SELECT {','.join(COLUMNS)} FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def save(self, inc):
        self.db.execute(
            "UPDATE incidents SET summary=?, labels=?, status=?, acked_at=?, acked_by=?, resolved_at=?, resolved_by=?, "
            "forced_level=?, agent_done=?, notified=?, message_id=?, thread_id=?, last_seen=? WHERE id=?",
            (inc.summary, json.dumps(inc.labels), inc.status, iso(inc.acked_at), inc.acked_by, iso(inc.resolved_at),
             inc.resolved_by, inc.forced_level, int(inc.agent_done), json.dumps(inc.notified), inc.message_id,
             inc.thread_id, iso(inc.last_seen), inc.id))

    def create(self, kind, alert, now):
        labels = alert.get("labels") or {}
        cur = self.db.execute(
            "INSERT INTO incidents (kind, fingerprint, starts_at, alertname, severity, prometheus, summary, labels, "
            "generator_url, status, created_at, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, alert["fingerprint"], alert.get("startsAt", ""), labels.get("alertname", "unnamed"),
             labels.get("severity", ""), labels.get("prometheus", ""),
             (alert.get("annotations") or {}).get("summary", ""), json.dumps(labels), alert.get("generatorURL", ""),
             "triggered", iso(now), iso(now)))
        return self.get(cur.lastrowid)

    # timeline
    def event(self, inc, now, kind, actor, detail=""):
        self.db.execute("INSERT INTO events (incident_id, at, kind, actor, detail) VALUES (?,?,?,?,?)",
                        (inc.id, iso(now), kind, actor, detail))

    def events(self, inc):
        return [dict(zip(("at", "kind", "actor", "detail"), r)) for r in
                self.db.execute("SELECT at, kind, actor, detail FROM events WHERE incident_id=? ORDER BY rowid", (inc.id,))]

    def unposted(self):
        return self.db.execute("SELECT rowid, incident_id, at, kind, actor, detail FROM events WHERE posted=0 ORDER BY rowid").fetchall()

    def mark_posted(self, rowid):
        self.db.execute("UPDATE events SET posted=1 WHERE rowid=?", (rowid,))

    # settings, people, overrides
    def setting(self, key, value=None):
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
            return str(value)
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def link(self, person, discord_id):
        self.db.execute("DELETE FROM people WHERE person=? OR discord_id=?", (person, discord_id))
        self.db.execute("INSERT INTO people VALUES (?,?)", (person, discord_id))

    def discord_id(self, person):
        row = self.db.execute("SELECT discord_id FROM people WHERE person=?", (person,)).fetchone()
        return row[0] if row else None

    def person_for(self, discord_id):
        row = self.db.execute("SELECT person FROM people WHERE discord_id=?", (discord_id,)).fetchone()
        return row[0] if row else None

    def add_override(self, o, created_by):
        self.db.execute('INSERT INTO overrides (person, schedule, start, "end", created_by) VALUES (?,?,?,?,?)',
                        (o.person, o.schedule, iso(o.start), iso(o.end), created_by))

    def overrides(self, now):
        rows = self.db.execute('SELECT person, schedule, start, "end" FROM overrides WHERE "end" > ?', (iso(now),)).fetchall()
        return [Override(p, parse_iso(s), parse_iso(e), sch, "command") for p, sch, s, e in rows]


# ---------------------------------------------------------------- what happens to an alert

class Pager:
    """Incident logic between Alertmanager, the store and the schedule. Returns what changed;
    bot.py turns that into Discord messages."""

    def __init__(self, store, rotation, clock=lambda: datetime.now(timezone.utc)):
        self.store, self.rotation, self.clock = store, rotation, clock

    def overrides(self):
        return self.store.overrides(self.clock())

    def on_alert(self, alert, kind):
        """One alert from an Alertmanager notification. Returns (incident, change) where change is
        opened, updated, resolved or ignored."""
        fp = str(alert.get("fingerprint", ""))
        if not re.fullmatch(r"[0-9a-f]{8,64}", fp):
            raise ValueError(f"bad fingerprint {fp!r}")
        now, status = self.clock(), alert.get("status")
        inc = self.store.latest(fp, kind)
        if status == "firing":
            if inc and inc.status != "resolved":
                inc.summary = (alert.get("annotations") or {}).get("summary", inc.summary)
                inc.last_seen = now
                self.store.save(inc)
                return inc, "updated"
            if inc and inc.starts_at == alert.get("startsAt") and not inc.resolved_by.startswith("Alertmanager"):
                return inc, "ignored"  # a person resolved this firing episode; the next one pages again
            inc = self.store.create(kind, alert, now)
            self.store.event(inc, now, "triggered", "Alertmanager", inc.summary)
            return inc, "opened"
        if status == "resolved":
            if not inc or inc.status == "resolved":
                return inc, "ignored"
            self.resolve(inc, "Alertmanager")
            return inc, "resolved"
        raise ValueError(f"unknown status {status!r}")

    def acknowledge(self, inc, person):
        if inc.status != "triggered":
            return False
        now = self.clock()
        inc.status, inc.acked_at, inc.acked_by = "acknowledged", now, person
        self.store.save(inc)
        self.store.event(inc, now, "acknowledged", self.rotation.name(person))
        return True

    def resolve(self, inc, by):
        if inc.status == "resolved":
            return False
        now = self.clock()
        inc.status, inc.resolved_at, inc.resolved_by = "resolved", now, by
        self.store.save(inc)
        self.store.event(inc, now, "resolved", self.rotation.name(by))
        return True

    def escalate(self, inc, person):
        """Escalate button: reach the next rule now. False when there is no next rule."""
        if inc.status == "resolved":
            return False
        now = self.clock()
        current = level_reached(inc, self.rotation, now, self.overrides())
        if current + 1 >= len(self.rotation.rules):
            return False
        inc.forced_level = current + 1
        if inc.status == "acknowledged":
            inc.status = "triggered"  # escalating hands it on
        self.store.save(inc)
        self.store.event(inc, now, "escalated", self.rotation.name(person), f"to level {current + 2}")
        return True

    def sweep(self):
        """Resolve open incidents Alertmanager stopped talking about. A firing alert repeats (pages every
        hour, tickets every 24 hours); an alert that resolves while inhibited or silenced sends no
        resolve, and its incident would stay open and keep paging."""
        now, done = self.clock(), []
        for kind, after in STALE_AFTER.items():
            for inc in self.store.open_incidents(kind):
                if inc.last_seen and now - inc.last_seen > after:
                    self.resolve(inc, "Alertmanager (stale)")
                    done.append(inc)
        return done

    def due(self):
        """(incident, actions) for every open page that needs something done now."""
        now, extra = self.clock(), self.overrides()
        out = []
        for inc in self.store.open_incidents("page"):
            actions = plan(inc, self.rotation, now, extra)
            if actions:
                out.append((inc, actions))
        return out

    def record(self, inc, action):
        now = self.clock()
        if action.kind == "agent":
            inc.agent_done = True
            self.store.event(inc, now, "responder", self.rotation.name(action.person), "runbook and read-only diagnosis")
        else:
            seen = inc.notified.get(action.person)
            inc.notified[action.person] = {"first": seen["first"] if seen else iso(now), "last": iso(now),
                                           "count": (seen["count"] + 1) if seen else 0}
            self.store.event(inc, now, "notified" if action.kind == "notify" else "re-notified",
                             self.rotation.name(action.person), action.reason)
        self.store.save(inc)

    def on_call_now(self):
        now, extra = self.clock(), self.overrides()
        return {name: self.rotation.who(name, now, extra) for name in self.rotation.schedules}


def public_view(inc, rotation):
    """The redacted record of an incident: what may leave the pager (board issue, a future
    public feed). Names from rotation.yml and times only."""
    def secs(a, b):
        return int((b - a).total_seconds()) if a and b else None
    return {
        "alert": inc.alertname,
        "severity": inc.severity,
        "prometheus": inc.prometheus,
        "fired": iso(inc.created_at),
        "acknowledged": iso(inc.acked_at),
        "acknowledged_by": rotation.name(inc.acked_by) if inc.acked_by else None,
        "resolved": iso(inc.resolved_at),
        "resolved_by": rotation.name(inc.resolved_by) if inc.resolved_by else None,
        "responder_ran": inc.agent_done,
        "time_to_acknowledge_s": secs(inc.created_at, inc.acked_at),
        "time_to_resolve_s": secs(inc.created_at, inc.resolved_at),
    }
