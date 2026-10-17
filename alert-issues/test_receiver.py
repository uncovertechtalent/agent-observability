"""Unit tests for receiver.py against a fake GitHub. Run: python -m unittest -v"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import receiver

NOW = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc)
receiver.log.disabled = True  # the failure cases log on purpose
FP = "0123456789abcdef"


class FakeGitHub:
    def __init__(self):
        self.issues, self.comments, self.calls = {}, [], []
        self.fail = False

    def _log(self, *call):
        if self.fail:
            raise urllib.error.URLError("github down")
        self.calls.append(call)

    def create_issue(self, title, body, labels):
        self._log("create", title)
        number = len(self.issues) + 1
        self.issues[number] = {"number": number, "state": "open", "title": title, "body": body, "labels": labels}
        return number

    def get_issue(self, number):
        self._log("get", number)
        return dict(self.issues[number]) if number in self.issues else None

    def update_issue(self, number, **fields):
        self._log("update", number, tuple(sorted(fields)))
        self.issues[number].update(fields)

    def comment(self, number, body):
        self._log("comment", number)
        self.comments.append((number, body))

    def find_issue(self, fingerprint):
        self._log("find", fingerprint)
        return next((n for n, i in self.issues.items() if fingerprint in i["body"]), None)


def alert(status="firing", starts="2026-10-17T13:40:00.123Z", ends="0001-01-01T00:00:00Z", fp=FP, **labels):
    labels = {"alertname": "OllamaDown", "severity": "page", "prometheus": "agent-observability", **labels}
    return {
        "status": status, "fingerprint": fp, "startsAt": starts, "endsAt": ends, "labels": labels,
        "annotations": {"summary": "The exporter cannot reach Ollama's /api/ps.",
                        "description": "secret detail on 192.168.1.161"},
        "generatorURL": "http://abc123:9090/graph?g0.expr=ollama_up",
    }


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "state.json")
        self.gh = FakeGitHub()
        self.rx = receiver.Receiver(self.gh, receiver.State(self.path), clock=lambda: NOW)

    def tearDown(self):
        self.dir.cleanup()

    def test_firing_opens_one_issue_with_kind_and_area_labels(self):
        self.assertEqual(self.rx.handle({"alerts": [alert()]}), ["opened #1"])
        issue = self.gh.issues[1]
        self.assertEqual(issue["title"], "Alert: OllamaDown")
        self.assertEqual(issue["labels"], ["kind:finding", "area:ai"])
        self.assertIn("The exporter cannot reach Ollama", issue["body"])
        self.assertIn(FP, issue["body"])
        self.assertIn("2026-10-17 13:40 UTC", issue["body"])

    def test_public_body_leaves_out_description_instance_and_generator_url(self):
        self.rx.handle({"alerts": [alert(instance="192.168.1.161:9100")]})
        b = self.gh.issues[1]["body"]
        for private in ("192.168.1.161", "secret detail", "abc123:9090"):
            self.assertNotIn(private, b)

    def test_repeat_updates_the_body_without_a_comment(self):
        self.rx.handle({"alerts": [alert()]})
        self.assertEqual(self.rx.handle({"alerts": [alert()]}), ["updated #1"])
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual(self.gh.comments, [])

    def test_resolved_comments_and_closes(self):
        self.rx.handle({"alerts": [alert()]})
        out = self.rx.handle({"alerts": [alert("resolved", ends="2026-10-17T13:55:00Z")]})
        self.assertEqual(out, ["closed #1"])
        self.assertEqual(self.gh.issues[1]["state"], "closed")
        self.assertEqual(self.gh.issues[1]["state_reason"], "completed")
        self.assertEqual(self.gh.comments, [(1, "Resolved at 2026-10-17 13:55 UTC.")])
        self.assertIn("has resolved", self.gh.issues[1]["body"])

    def test_new_episode_reopens_the_same_issue(self):
        self.rx.handle({"alerts": [alert()]})
        self.rx.handle({"alerts": [alert("resolved", ends="2026-10-17T13:55:00Z")]})
        out = self.rx.handle({"alerts": [alert(starts="2026-10-17T15:00:00Z")]})
        self.assertEqual(out, ["reopened #1"])
        self.assertEqual(self.gh.issues[1]["state"], "open")
        self.assertEqual(self.gh.comments[-1], (1, "Firing again since 2026-10-17 15:00 UTC."))
        self.assertEqual(len(self.gh.issues), 1)

    def test_issue_a_person_closed_stays_closed_in_the_same_episode(self):
        self.rx.handle({"alerts": [alert()]})
        self.gh.issues[1]["state"] = "closed"
        self.assertEqual(self.rx.handle({"alerts": [alert()]}), ["left #1 closed"])
        self.assertEqual(self.gh.issues[1]["state"], "closed")

    def test_lost_state_finds_the_issue_by_fingerprint(self):
        self.rx.handle({"alerts": [alert()]})
        os.remove(self.path)
        fresh = receiver.Receiver(self.gh, receiver.State(self.path), clock=lambda: NOW)
        self.assertEqual(fresh.handle({"alerts": [alert()]}), ["updated #1"])
        self.assertEqual(len(self.gh.issues), 1)

    def test_deleted_issue_gets_a_new_one(self):
        self.rx.handle({"alerts": [alert()]})
        del self.gh.issues[1]
        self.assertEqual(self.rx.handle({"alerts": [alert()]}), ["opened #1"])

    def test_resolved_without_an_issue_does_nothing(self):
        self.assertEqual(self.rx.handle({"alerts": [alert("resolved")]}), ["resolved, no issue"])
        self.assertEqual(self.gh.issues, {})

    def test_one_issue_per_fingerprint_in_a_group(self):
        group = [alert(fp="aaaaaaaaaaaaaaaa", alertname="SearXNGEngineFailing", severity="ticket", engine="google"),
                 alert(fp="bbbbbbbbbbbbbbbb", alertname="SearXNGEngineFailing", severity="ticket", engine="brave")]
        self.assertEqual(self.rx.handle({"alerts": group}), ["opened #1", "opened #2"])
        self.assertEqual(self.gh.issues[1]["title"], "Alert: SearXNGEngineFailing (google)")
        self.assertEqual(self.gh.issues[2]["labels"], ["kind:finding", "area:operations"])

    def test_area_label_on_the_alert_wins_and_unknown_areas_fall_back(self):
        self.assertEqual(receiver.area_label({"alertname": "X", "area": "money"}), "area:money")
        self.assertEqual(receiver.area_label({"alertname": "X", "area": "bogus"}), "area:operations")
        self.assertEqual(receiver.area_label({"alertname": "AWSCostDailySpike"}), "area:money")
        self.assertEqual(receiver.area_label({"alertname": "AgentToolDenialsSpike"}), "area:security")
        self.assertEqual(receiver.area_label({"alertname": "UCGMaxPollerScrapeFailing"}), "area:operations")

    def test_summary_cannot_mention_anyone(self):
        a = alert()
        a["annotations"]["summary"] = "ping @skjoldar now"
        self.rx.handle({"alerts": [a]})
        self.assertNotIn("@skjoldar", self.gh.issues[1]["body"])

    def test_bad_input_is_rejected(self):
        with self.assertRaises(ValueError):
            self.rx.handle({"alerts": [alert(fp="../../etc")]})
        with self.assertRaises(ValueError):
            self.rx.handle({"nope": 1})
        with self.assertRaises(ValueError):
            self.rx.handle({"alerts": [alert(status="weird")]})


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.gh = FakeGitHub()
        receiver.Handler.receiver = receiver.Receiver(
            self.gh, receiver.State(os.path.join(self.dir.name, "s.json")), clock=lambda: NOW)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.dir.cleanup()

    def post(self, payload):
        req = urllib.request.Request(self.url + "/alerts", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_ok_bad_and_failing(self):
        self.assertEqual(self.post({"receiver": "board", "alerts": [alert()]}), 200)
        self.assertEqual(self.post({"alerts": [alert(fp="x")]}), 400)
        self.gh.fail = True
        self.assertEqual(self.post({"alerts": [alert(fp="cccccccccccccccc")]}), 500)
        with urllib.request.urlopen(self.url + "/healthz", timeout=5) as r:
            self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
