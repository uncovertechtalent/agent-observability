"""Tests for bot.py that need discord.py and aiohttp: run inside the pager image (scripts/check-alerting.sh).

    docker run --rm -v "$PWD/pager:/src:ro" -w /src <pager image> python -m unittest test_bot
"""

import unittest
from datetime import datetime, timezone

import discord
import yaml
from aiohttp.test_utils import TestClient, TestServer

import bot
import core

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)  # 14:00 Berlin, a Saturday


def make_bot():
    with open("rotation.yml") as f:
        rotation = core.Rotation(yaml.safe_load(f))
    pager = core.Pager(core.Store(":memory:"), rotation, clock=lambda: NOW)
    b = bot.PagerBot(pager, [{"match": ".*", "url": "https://example.org/runbook/"}])
    b.kick = lambda: None  # no background tick in these tests: no Discord, no alert-issues
    return b


def alert(status="firing"):
    return {"status": status, "fingerprint": "0123456789abcdef", "startsAt": "2026-10-17T11:59:00Z",
            "labels": {"alertname": "OllamaDown", "severity": "page", "prometheus": "agent-observability"},
            "annotations": {"summary": "The exporter cannot reach Ollama."}, "generatorURL": ""}


class BotTest(unittest.IsolatedAsyncioTestCase):
    async def test_discord_keeps_its_own_http_client(self):
        b = make_bot()
        self.assertIsInstance(b.http, discord.http.HTTPClient)
        self.assertIsNone(b.session)
        self.addCleanup(b.store.close)

    async def test_embed_and_buttons_follow_the_state(self):
        b = make_bot()
        self.addCleanup(b.store.close)
        inc, _ = b.pager.on_alert(alert(), "page")
        e = b.embed(inc)
        self.assertIn("PAGE #1 TRIGGERED: OllamaDown", e.title)
        self.assertEqual([c.item.disabled for c in b.view(inc).children], [False, False, False])
        b.pager.acknowledge(inc, "stefan")
        inc = b.store.get(inc.id)
        self.assertIn("Stefan Coetzee", " ".join(f.value for f in b.embed(inc).fields))
        self.assertEqual([c.item.disabled for c in b.view(inc).children], [True, False, False])
        self.assertEqual([c.item.custom_id for c in b.view(inc).children], ["pager:ack:1", "pager:resolve:1", "pager:escalate:1"])

    async def test_webhook_opens_and_resolves_and_metrics_count(self):
        b = make_bot()
        self.addCleanup(b.store.close)
        client = TestClient(TestServer(bot.web_app(b)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        r = await client.post("/alerts/page", json={"groupKey": "g", "alerts": [alert()]})
        self.assertEqual(r.status, 200)
        r = await client.post("/alerts/ticket", json={"alerts": [alert()]})  # a page on the ticket route is skipped
        self.assertEqual(r.status, 200)
        metrics = await (await client.get("/metrics")).text()
        self.assertIn('pager_incidents_open{kind="page"} 1', metrics)
        self.assertIn('pager_incidents_open{kind="ticket"} 0', metrics)
        self.assertIn("pager_discord_connected 0", metrics)
        await client.post("/alerts/page", json={"alerts": [alert("resolved")]})
        self.assertIn('pager_incidents_open{kind="page"} 0', await (await client.get("/metrics")).text())
        self.assertEqual((await client.post("/alerts/page", data="nope")).status, 400)


if __name__ == "__main__":
    unittest.main()
