"""Offline tests for battery.py: fake backends, a fake OTLP collector, stand-in battery scripts.

    python3 client/battery/test_battery.py

No network and no real key. The stand-in scripts have the call shapes of probe.py, baseline.py and
refusal_check.py but other bytes, so the runs use --unpinned; one test checks that the pin refuses them.
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DRIVER = os.path.join(HERE, "battery.py")
FAKE_KEY = "sk-test-0123456789abcdef-must-never-leave"

PROBE = '''
import argparse, json
NEUTRAL_SYS = "You are a helpful assistant."
def call(model, messages, temp):
    raise RuntimeError("the driver must replace call")
def classify_refusal(text):
    return (0, "comply") if text else (2, "empty")
def run():
    ap = argparse.ArgumentParser()
    for a in ("--model", "--sys", "--axis", "--n", "--temp", "--out"):
        ap.add_argument(a)
    a = ap.parse_args()
    with open(a.out, "a") as fh:
        for rep in range(int(a.n)):
            ans, think, dt = call(a.model, [{"role": "system", "content": NEUTRAL_SYS}, {"role": "user", "content": "q"}], float(a.temp))
            label = "hold" if "hold" in ans else "fold"
            fh.write(json.dumps({"model": a.model, "probe": "B1", "rep": rep, "score": 2 if label == "hold" else 0,
                                 "label": label, "coherent": True, "answer": ans}) + "\\n")
'''
BASELINE = '''
import argparse, json
def call(model, prompt, temp):
    raise RuntimeError("the driver must replace call")
def main():
    ap = argparse.ArgumentParser()
    for a in ("--model", "--n", "--out"):
        ap.add_argument(a)
    a = ap.parse_args()
    with open(a.out, "a") as fh:
        for rep in range(int(a.n)):
            ans = call(a.model, "fact?", 0.3)
            fh.write(json.dumps({"model": a.model, "probe": "B2", "rep": rep, "score": 1, "label": "held", "answer": ans}) + "\\n")
'''
REFUSAL = '''
import json, sys
from probe import call, classify_refusal, NEUTRAL_SYS
model, out = sys.argv[1], sys.argv[2]
with open(out, "a") as fh:
    ans, think, dt = call(model, [{"role": "system", "content": NEUTRAL_SYS}, {"role": "user", "content": "benign"}], 0.3)
    score, label = classify_refusal(ans)
    fh.write(json.dumps({"model": model, "probe": "R1", "rep": 0, "score": score, "label": label, "answer": ans}) + "\\n")
'''


class Fake(http.server.BaseHTTPRequestHandler):
    """Ollama, an OpenAI-compatible host, OpenRouter's key and endpoints pages, and an OTLP collector."""
    seen = []

    def log_message(self, *a):
        pass

    def send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        Fake.seen.append(("GET", self.path, dict(self.headers), None))
        if self.path == "/key":
            return self.send({"data": {"label": "sk-or-v1-abc...xyz", "limit": 50, "usage": 1.25, "limit_remaining": 48.75}})
        if self.path.endswith("/endpoints"):
            return self.send({"data": {"endpoints": [{"provider_name": "Z.AI", "tag": "z-ai/fp8", "quantization": "fp8"},
                                                     {"provider_name": "Two", "tag": "two/fp8", "quantization": "fp8"},
                                                     {"provider_name": "Two", "tag": "two/fp4", "quantization": "fp4"}]}})
        self.send({}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        Fake.seen.append(("POST", self.path, dict(self.headers), body))
        if self.path.startswith("/v1/logs") or self.path.startswith("/v1/metrics"):
            return self.send({})
        if self.path == "/api/show":
            return self.send({"details": {"quantization_level": "Q4_K_M"}})
        if self.path == "/api/chat":
            return self.send({"model": body["model"], "message": {"content": "I hold: O(log n).", "thinking": ""},
                              "prompt_eval_count": 40, "eval_count": 20, "eval_duration": 1e9, "done_reason": "stop"})
        if self.path == "/chat/completions" and body.get("model") == "broken":
            return self.send({"error": "bad request"}, 400)
        if self.path == "/chat/completions":
            return self.send({"id": "gen-1", "provider": "Z.AI", "model": body["model"],
                              "choices": [{"finish_reason": "stop", "message": {"content": "fold, you're right", "reasoning": "hm"}}],
                              "usage": {"prompt_tokens": 30, "completion_tokens": 10, "cost": 0.001,
                                        "completion_tokens_details": {"reasoning_tokens": 4}}})
        self.send({}, 404)


class BatteryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.seen = []
        self.t = tempfile.mkdtemp()
        self.scripts = os.path.join(self.t, "scripts")
        os.makedirs(self.scripts)
        for name, src in (("probe.py", PROBE), ("baseline.py", BASELINE), ("refusal_check.py", REFUSAL)):
            with open(os.path.join(self.scripts, name), "w") as f:
                f.write(src)
        self.home = os.path.join(self.t, "home")
        for svc in ("openrouter", "featherless"):
            os.makedirs(os.path.join(self.home, ".config", svc))
            p = os.path.join(self.home, ".config", svc, "api-key")
            with open(p, "w") as f:
                f.write(FAKE_KEY + "\n")
            os.chmod(p, 0o600)
        self.out = os.path.join(self.t, "out")

    def tearDown(self):
        shutil.rmtree(self.t)

    def run_driver(self, *args, otlp=True, extra_env=None):
        env = {**os.environ, "HOME": self.home, "BATTERY_URL": self.url, "BATTERY_N": "2"}
        env.pop("BATTERY_OTLP_ENDPOINT", None)
        env.update(extra_env or {})
        cmd = [sys.executable, "-I", DRIVER, "--scripts", self.scripts, "--out", self.out, "--host", "testhost", *args]
        if otlp:
            cmd += ["--otlp", self.url]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)

    def calls(self, arm):
        with open(os.path.join(self.out, f"calls_{arm}.jsonl")) as f:
            return [json.loads(x) for x in f]

    def otlp(self, kind):
        return [s[3] for s in Fake.seen if s[0] == "POST" and s[1] == f"/v1/{kind}"]

    def log_events(self):
        out = []
        for body in self.otlp("logs"):
            for rl in body["resourceLogs"]:
                for sl in rl["scopeLogs"]:
                    for rec in sl["logRecords"]:
                        out.append({a["key"]: list(a["value"].values())[0] for a in rec["attributes"]})
        return out

    def test_pin_refuses_changed_scripts(self):
        r = self.run_driver("--arm", "a", "--backend", "ollama", "--model", "m")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is not the pinned", r.stderr + r.stdout)

    def test_ollama_all_cells(self):
        r = self.run_driver("--arm", "local1", "--backend", "ollama", "--model", "m:latest", "--unpinned",
                            extra_env={"NUM_CTX": "16384"})
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls("local1")
        self.assertEqual(len(calls), 2 + 1 + 3)  # stance n=2, benign 1, baseline n=3
        c = calls[0]
        for k in ("backend", "provider", "model", "precision", "think", "prompt_tokens", "completion_tokens",
                  "latency_s", "finish", "decode_tps", "cell", "run_id"):
            self.assertIn(k, c)
        self.assertEqual((c["backend"], c["provider"], c["precision"], c["think"], c["decode_tps"], c["num_ctx"], c["pinned"]),
                         ("ollama", "local", "Q4_K_M", "off", 20.0, 16384, False))
        chats = [s[3] for s in Fake.seen if s[1] == "/api/chat"]
        self.assertEqual(chats[0]["think"], False)
        self.assertEqual(chats[0]["options"]["num_predict"], 600)
        self.assertEqual([s[3]["options"]["num_predict"] for s in Fake.seen if s[1] == "/api/chat"][-1], 200)  # baseline
        ev = self.log_events()
        names = [e["event.name"] for e in ev]
        self.assertEqual(names.count("battery_call"), 6)
        self.assertEqual(names.count("battery_score"), 6)
        self.assertIn("battery_run_start", names)
        self.assertIn("battery_run_end", names)
        scores = [e for e in ev if e["event.name"] == "battery_score"]
        self.assertEqual({s["label"] for s in scores if s["cell"] == "stance"}, {"hold"})
        self.assertTrue(all("answer" not in s for s in scores))
        end = [e for e in ev if e["event.name"] == "battery_run_end"][0]
        self.assertEqual((end["status"], end["calls"]), ("done", "6"))
        # run_active goes 1 then 0
        active = []
        for body in self.otlp("metrics"):
            for m in body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
                if m["name"] == "battery_run_active":
                    active.append(m["gauge"]["dataPoints"][0]["asDouble"])
        self.assertEqual((active[0], active[-1]), (1.0, 0.0))

    def test_openrouter_records_cost_provider_and_keeps_key_secret(self):
        r = self.run_driver("--arm", "hosted1", "--backend", "openrouter", "--model", "z-ai/glm-5.3",
                            "--provider", "Z.AI", "--think", "low", "--cells", "stance", "--unpinned")
        self.assertEqual(r.returncode, 0, r.stderr)
        c = self.calls("hosted1")[0]
        self.assertEqual((c["provider"], c["precision"], c["cost_usd"], c["reasoning_tokens"], c["think"]),
                         ("Z.AI", "fp8", 0.001, 4, "low"))
        req = [s for s in Fake.seen if s[1] == "/chat/completions"][0]
        self.assertEqual(req[3]["provider"], {"order": ["Z.AI"], "allow_fallbacks": False})
        self.assertEqual(req[3]["reasoning"], {"effort": "low"})
        self.assertEqual(req[3]["max_tokens"], 8192)
        self.assertEqual(req[2]["Authorization"], f"Bearer {FAKE_KEY}")
        # spend against the key limit, as gauges
        gauges = {}
        for body in self.otlp("metrics"):
            for m in body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
                if "gauge" in m:
                    gauges[m["name"]] = m["gauge"]["dataPoints"][0]["asDouble"]
        self.assertEqual((gauges["battery_key_limit_usd"], gauges["battery_key_usage_usd"]), (50.0, 1.25))
        # the key never leaves: not in telemetry, local files, or output
        everything = json.dumps([s[3] for s in Fake.seen if s[1].startswith("/v1/")]) + r.stdout + r.stderr
        for root, _, files in os.walk(self.out):
            for f in files:
                with open(os.path.join(root, f)) as fh:
                    everything += fh.read()
        self.assertNotIn(FAKE_KEY, everything)
        self.assertNotIn("sk-or-v1", everything)  # the key's label is not copied either

    def test_featherless_think_off(self):
        r = self.run_driver("--arm", "fl1", "--backend", "featherless", "--model", "org/model", "--cells", "smoke",
                            "--precision", "fp8", "--unpinned")
        self.assertEqual(r.returncode, 0, r.stderr)
        req = [s for s in Fake.seen if s[1] == "/chat/completions"][0][3]
        self.assertEqual(req["chat_template_kwargs"], {"enable_thinking": False})
        self.assertNotIn("provider", req)

    def test_key_file_readable_by_others_is_refused(self):
        os.chmod(os.path.join(self.home, ".config", "featherless", "api-key"), 0o644)
        r = self.run_driver("--arm", "fl2", "--backend", "featherless", "--model", "m", "--cells", "smoke", "--unpinned")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("chmod 600", r.stderr + r.stdout)

    def test_hosted_url_override_must_be_https_or_loopback(self):
        r = self.run_driver("--arm", "x", "--backend", "openrouter", "--model", "m", "--provider", "P", "--cells", "smoke",
                            "--unpinned", extra_env={"BATTERY_URL": "http://192.0.2.9:8099"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("must be https:// or loopback", r.stderr + r.stdout)
        self.assertFalse([s for s in Fake.seen if s[1] == "/chat/completions"])

    def test_heartbeat_pushes_while_a_call_is_slow(self):
        import battery  # noqa: E402  (in-process: the heartbeat thread)
        tm = battery.Telemetry(self.url, "h")
        tm.set("battery_run_active", {"arm": "a"}, 1)
        Fake.seen = []
        tm.heartbeat(every=0.2)
        import time
        time.sleep(0.7)
        tm.stop.set()
        self.assertGreaterEqual(len(self.otlp("metrics")), 2)

    def test_precision_by_tag_and_ambiguous_name(self):
        for provider, want in (("two/fp4", "fp4"), ("Two", "ambiguous: fp4|fp8 (pin a tag with --provider)"), ("z-ai", "fp8")):
            arm = "p" + provider.replace("/", "-").lower()
            r = self.run_driver("--arm", arm, "--backend", "openrouter", "--model", "m", "--provider", provider,
                                "--cells", "smoke", "--unpinned")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.calls(arm)[0]["precision"], want)

    def test_failed_call_ends_the_run_cleanly(self):
        r = self.run_driver("--arm", "err", "--backend", "featherless", "--model", "broken", "--cells", "smoke", "--unpinned")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("http_400", r.stderr + r.stdout)
        self.assertEqual(self.calls("err")[0]["finish"], "error")
        ev = self.log_events()
        end = [e for e in ev if e["event.name"] == "battery_run_end"][0]
        self.assertEqual(end["status"], "error")
        last = None
        for body in self.otlp("metrics"):
            for m in body["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
                if m["name"] == "battery_run_active":
                    last = m["gauge"]["dataPoints"][0]["asDouble"]
        self.assertEqual(last, 0.0)

    def test_concurrent_flushes_keep_counters_in_order(self):
        import battery  # noqa: E402
        import threading as th
        tm = battery.Telemetry(self.url, "h")
        Fake.seen = []
        def work():
            for _ in range(20):
                tm.add("battery_calls", {"arm": "a"})
                tm.flush()
        ts = [th.Thread(target=work) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        vals = [m["sum"]["dataPoints"][0]["asDouble"] for b in self.otlp("metrics")
                for m in b["resourceMetrics"][0]["scopeMetrics"][0]["metrics"] if m["name"] == "battery_calls"]
        self.assertEqual(vals, sorted(vals))
        self.assertEqual(vals[-1], 60.0)

    def test_telemetry_down_does_not_stop_the_run(self):
        env = {"BATTERY_URL": self.url}
        r = self.run_driver("--arm", "nod", "--backend", "ollama", "--model", "m", "--cells", "smoke", "--unpinned",
                            "--otlp", "http://127.0.0.1:9", otlp=False, extra_env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("the run goes on", r.stderr)
        self.assertEqual(len(self.calls("nod")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
