"""Unit tests for core.py and diagnose.py, stdlib only. Run: python -m unittest -v"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import core
import diagnose

ROTATION = {
    "timezone": "Europe/Berlin",
    "people": {"stefan": {"name": "Stefan Coetzee", "kind": "human"},
               "ana": {"name": "Ana", "kind": "human"},
               "responder": {"name": "Responder agent", "kind": "agent"}},
    "schedules": {
        "primary": {"layers": [{"name": "Waking hours", "members": ["stefan"], "start": "2026-10-19T08:00",
                                "turn": "7d", "restrictions": [{"from": "08:00", "to": "22:00"}]}]},
        "responder": {"layers": [{"name": "Always", "members": ["responder"]}]},
    },
    "escalation_policy": {"name": "House default", "rules": [{"after": "0m", "schedule": "primary"},
                                                              {"after": "15m", "schedule": "responder"}],
                          "renotify_after": "5m", "renotify_limit": 3},
    "overrides": [],
}
# 2026-10-17 is a Saturday; Berlin is UTC+2 (CEST) until 2026-10-25.
DAY = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)     # 14:00 Berlin
NIGHT = datetime(2026, 10, 17, 21, 0, tzinfo=timezone.utc)   # 23:00 Berlin


def page(fp="0123456789abcdef", status="firing", starts="2026-10-17T12:00:00Z", name="OllamaDown"):
    return {"status": status, "fingerprint": fp, "startsAt": starts,
            "labels": {"alertname": name, "severity": "page", "prometheus": "agent-observability", "job": "ollama-exporter"},
            "annotations": {"summary": "The exporter cannot reach Ollama."},
            "generatorURL": "http://abc:9090/graph?g0.expr=ollama_up+%3D%3D+0&g0.tab=1"}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.r = core.Rotation(ROTATION)

    def test_waking_hours_in_berlin_time(self):
        self.assertEqual(self.r.who("primary", DAY), "stefan")
        self.assertIsNone(self.r.who("primary", NIGHT))
        self.assertIsNone(self.r.who("primary", datetime(2026, 10, 17, 5, 59, tzinfo=timezone.utc)))  # 07:59
        self.assertEqual(self.r.who("primary", datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)), "stefan")  # 08:00
        self.assertEqual(self.r.who("responder", NIGHT), "responder")

    def test_overnight_restriction_belongs_to_the_day_it_starts(self):
        r = core.Restriction({4}, core.clock("22:00"), core.clock("06:00"))  # Friday night
        fri_23 = datetime(2026, 10, 16, 23, 0)
        sat_05 = datetime(2026, 10, 17, 5, 0)
        sat_23 = datetime(2026, 10, 17, 23, 0)
        self.assertTrue(r.allows(fri_23))
        self.assertTrue(r.allows(sat_05))
        self.assertFalse(r.allows(sat_23))

    def test_rotation_turns_and_later_layers_win(self):
        doc = dict(ROTATION, schedules={
            "primary": {"layers": [
                {"name": "base", "members": ["stefan", "ana"], "start": "2026-10-12T08:00", "turn": "7d"},
                {"name": "weekend", "members": ["ana"], "restrictions": [{"days": ["sat", "sun"], "from": "10:00", "to": "12:00"}]}]},
            "responder": ROTATION["schedules"]["responder"]})
        r = core.Rotation(doc)
        self.assertEqual(r.who("primary", datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)), "stefan")  # first week
        self.assertEqual(r.who("primary", datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)), "ana")     # second week
        self.assertEqual(r.who("primary", datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)), "ana")      # Sat 11:00

    def test_overrides_win_from_file_and_from_the_command(self):
        doc = dict(ROTATION, overrides=[{"person": "ana", "start": "2026-10-17T13:00", "end": "2026-10-17T15:00"}])
        r = core.Rotation(doc)
        self.assertEqual(r.who("primary", DAY), "ana")
        night = r.override({"person": "stefan", "start": NIGHT - timedelta(hours=1), "end": NIGHT + timedelta(hours=2)}, "command")
        self.assertEqual(r.who("primary", NIGHT, [night]), "stefan")

    def test_bad_files_are_refused(self):
        for bad in (dict(ROTATION, people={"x": {"kind": "robot"}}),
                    dict(ROTATION, escalation_policy={"rules": [{"after": "0m", "schedule": "nope"}]}),
                    dict(ROTATION, escalation_policy={"rules": [{"after": "15m", "schedule": "primary"},
                                                                {"after": "0m", "schedule": "responder"}]})):
            with self.assertRaises((ValueError, KeyError)):
                core.Rotation(bad)
        with self.assertRaises(ValueError):
            core.duration("soon")


class EscalationTest(unittest.TestCase):
    def make(self, t):
        self.clock = Clock(t)
        self.pager = core.Pager(core.Store(":memory:"), core.Rotation(ROTATION), clock=self.clock)
        self.addCleanup(self.pager.store.close)
        inc, change = self.pager.on_alert(page(), "page")
        self.assertEqual(change, "opened")
        return inc

    def run_due(self):
        done = []
        for inc, actions in self.pager.due():
            for a in actions:
                self.pager.record(inc, a)
                done.append((a.kind, a.person))
        return done

    def test_daytime_notify_renotify_then_responder(self):
        self.make(DAY)
        self.assertEqual(self.run_due(), [("notify", "stefan")])
        self.assertEqual(self.run_due(), [])
        self.clock.advance(minutes=5)
        self.assertEqual(self.run_due(), [("renotify", "stefan")])
        self.clock.advance(minutes=5)
        self.assertEqual(self.run_due(), [("renotify", "stefan")])
        self.clock.advance(minutes=5)  # 15 minutes: third re-notify and the responder layer
        self.assertEqual(self.run_due(), [("renotify", "stefan"), ("agent", "responder")])
        self.clock.advance(minutes=5)
        self.assertEqual(self.run_due(), [])  # re-notify limit reached, responder ran once

    def test_acknowledge_stops_escalation(self):
        inc = self.make(DAY)
        self.run_due()
        self.assertTrue(self.pager.acknowledge(self.pager.store.get(inc.id), "stefan"))
        self.clock.advance(minutes=30)
        self.assertEqual(self.run_due(), [])
        self.assertFalse(self.pager.acknowledge(self.pager.store.get(inc.id), "stefan"))

    def test_out_of_hours_the_responder_takes_it_and_the_operator_hears_at_eight(self):
        self.make(NIGHT)
        self.assertEqual(self.run_due(), [("agent", "responder")])
        self.clock.advance(hours=4)
        self.assertEqual(self.run_due(), [])
        self.clock.t = datetime(2026, 10, 18, 6, 0, 30, tzinfo=timezone.utc)  # 08:00:30 Berlin
        self.assertEqual(self.run_due(), [("notify", "stefan")])

    def test_escalate_button_reaches_the_responder_now(self):
        inc = self.make(DAY)
        self.run_due()
        self.assertTrue(self.pager.escalate(self.pager.store.get(inc.id), "stefan"))
        self.assertEqual(self.run_due(), [("agent", "responder")])
        self.assertFalse(self.pager.escalate(self.pager.store.get(inc.id), "stefan"))  # no level after the last

    def test_command_override_puts_someone_on_at_night(self):
        self.make(NIGHT)
        o = self.pager.rotation.override({"person": "stefan", "start": NIGHT - timedelta(minutes=1),
                                          "end": NIGHT + timedelta(hours=1)}, "command")
        self.pager.store.add_override(o, "test")
        self.assertEqual(self.run_due(), [("notify", "stefan")])


class IncidentTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(DAY)
        self.pager = core.Pager(core.Store(":memory:"), core.Rotation(ROTATION), clock=self.clock)
        self.addCleanup(self.pager.store.close)

    def test_dedup_by_fingerprint_and_resolve_from_alertmanager(self):
        inc, change = self.pager.on_alert(page(), "page")
        self.assertEqual(change, "opened")
        self.assertEqual(self.pager.on_alert(page(), "page")[1], "updated")
        self.clock.advance(minutes=20)
        inc, change = self.pager.on_alert(page(status="resolved"), "page")
        self.assertEqual((change, inc.status, inc.resolved_by), ("resolved", "resolved", "Alertmanager"))
        self.assertEqual(self.pager.on_alert(page(status="resolved"), "page")[1], "ignored")
        self.assertEqual(self.pager.on_alert(page(), "page")[1], "opened")  # Alertmanager resolved: a refire pages

    def test_a_person_resolving_mutes_the_rest_of_that_episode(self):
        inc, _ = self.pager.on_alert(page(), "page")
        self.pager.resolve(inc, "stefan")
        self.assertEqual(self.pager.on_alert(page(), "page")[1], "ignored")
        self.assertEqual(self.pager.on_alert(page(starts="2026-10-17T13:00:00Z"), "page")[1], "opened")

    def test_sweep_resolves_what_alertmanager_stopped_repeating(self):
        page_inc, _ = self.pager.on_alert(page(), "page")
        t = dict(page(fp="aaaaaaaaaaaaaaaa", name="SearXNGSlowSearches"))
        t["labels"] = dict(t["labels"], severity="ticket")
        self.pager.on_alert(t, "ticket")
        self.clock.advance(hours=2)
        self.assertEqual(self.pager.sweep(), [])
        self.pager.on_alert(page(), "page")  # an hourly repeat keeps the page open
        self.clock.advance(hours=2)
        self.assertEqual(self.pager.sweep(), [])
        self.clock.advance(hours=1)
        swept = self.pager.sweep()
        self.assertEqual([i.kind for i in swept], ["page"])
        self.assertEqual(self.pager.store.get(page_inc.id).resolved_by, "Alertmanager (stale)")
        self.clock.advance(hours=21)
        self.assertEqual([i.kind for i in self.pager.sweep()], ["ticket"])
        self.assertEqual(self.pager.on_alert(page(), "page")[1], "opened")  # a stale resolve does not mute the episode

    def test_tickets_are_tracked_apart_from_pages(self):
        t = dict(page(fp="aaaaaaaaaaaaaaaa", name="SearXNGSlowSearches"))
        t["labels"] = dict(t["labels"], severity="ticket")
        self.assertEqual(self.pager.on_alert(t, "ticket")[1], "opened")
        self.assertEqual(self.pager.due(), [])  # tickets never escalate
        self.assertEqual(len(self.pager.store.open_incidents("ticket")), 1)

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            self.pager.on_alert(page(fp="nope"), "page")
        with self.assertRaises(ValueError):
            self.pager.on_alert(page(status="weird"), "page")

    def test_timeline_and_public_view(self):
        inc, _ = self.pager.on_alert(page(), "page")
        self.clock.advance(minutes=3)
        self.pager.acknowledge(self.pager.store.get(inc.id), "stefan")
        self.clock.advance(minutes=17)
        self.pager.on_alert(page(status="resolved"), "page")
        inc = self.pager.store.get(inc.id)
        self.assertEqual([e["kind"] for e in self.pager.store.events(inc)], ["triggered", "acknowledged", "resolved"])
        v = core.public_view(inc, self.pager.rotation)
        self.assertEqual((v["acknowledged_by"], v["time_to_acknowledge_s"], v["time_to_resolve_s"], v["resolved_by"]),
                         ("Stefan Coetzee", 180, 1200, "Alertmanager"))
        self.assertNotIn("labels", v)
        self.assertNotIn("generator_url", v)

    def test_settings_people_and_overrides_persist(self):
        s = self.pager.store
        s.setting("channel_page", 123)
        s.link("stefan", 42)
        s.link("stefan", 43)  # relinking replaces
        self.assertEqual((s.setting("channel_page"), s.person_for(43), s.person_for(42)), ("123", "stefan", None))


class DiagnoseTest(unittest.TestCase):
    def test_read_only_diagnosis_with_runbook(self):
        pager = core.Pager(core.Store(":memory:"), core.Rotation(ROTATION), clock=lambda: DAY)
        self.addCleanup(pager.store.close)
        inc, _ = pager.on_alert(page(), "page")
        asked = []

        async def query(prometheus, promql):
            asked.append((prometheus, promql))
            if promql.startswith("ALERTS{alertstate"):
                return [{"metric": {"alertname": "OllamaDown"}}, {"metric": {"alertname": "SearXNGDown"}}]
            if promql.startswith("count_over_time"):
                return [{"metric": {}, "value": [0, "42"]}]
            if promql.startswith("up"):
                raise RuntimeError("timeout")
            return [{"metric": {"instance": "x"}, "value": [0, "0"]}]

        runbooks = [{"match": "Ollama.*", "url": "https://example.org/local-llm/"}, {"match": ".*", "url": "https://x/"}]
        text = asyncio.run(diagnose.diagnose(inc, query, runbooks))
        self.assertIn("Runbook: https://example.org/local-llm/", text)
        self.assertIn("expression now: 1 series match", text)
        self.assertIn("about 42 minutes", text)
        self.assertIn("query failed (RuntimeError)", text)
        self.assertIn("other alerts firing: SearXNGDown", text)
        self.assertIn("nothing was changed", text)
        self.assertEqual(asked[0], ("agent-observability", "ollama_up == 0"))

    def test_unsafe_labels_never_reach_promql(self):
        pager = core.Pager(core.Store(":memory:"), core.Rotation(ROTATION), clock=lambda: DAY)
        self.addCleanup(pager.store.close)
        a = page(name='X"}or vector(1)#')
        a["labels"]["job"] = 'a"} or up{'
        a["generatorURL"] = ""
        inc, _ = pager.on_alert(a, "page")
        asked = []

        async def query(prometheus, promql):
            asked.append(promql)
            return []

        asyncio.run(diagnose.diagnose(inc, query, []))
        self.assertEqual(asked, ['ALERTS{alertstate="firing"}'])


if __name__ == "__main__":
    unittest.main()
