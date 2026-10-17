"""Tests for bot.py that need discord.py and aiohttp: run inside the pager image (scripts/check-alerting.sh).

    docker run --rm -v "$PWD/pager:/src:ro" -w /src <pager image> python -m unittest test_bot
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

import discord
import yaml
from aiohttp import web
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

    async def test_a_deleted_thread_stays_with_its_incident(self):
        b = make_bot()
        self.addCleanup(b.store.close)
        lost, _ = b.pager.on_alert(alert(), "page")
        other = dict(alert(), fingerprint="fedcba9876543210")
        other["labels"] = dict(other["labels"], alertname="SearXNGDown")
        kept, _ = b.pager.on_alert(other, "page")
        for inc, ids in ((lost, (11, 12)), (kept, (21, 22))):
            inc = b.store.get(inc.id)
            inc.message_id, inc.thread_id = ids
            b.store.save(inc)
        posted = []

        async def thread_send(inc, text):
            if inc.id == lost.id:
                raise discord.NotFound(type("R", (), {"status": 404, "reason": "Not Found"})(), "Unknown Channel")
            posted.append(inc.id)
            return True
        b.thread_send = thread_send
        await b.post_events()
        self.assertEqual(posted, [kept.id])
        self.assertEqual(b.errors, 1)
        gone = b.store.get(lost.id)
        self.assertEqual((gone.message_id, gone.thread_id), (None, None))  # posted again on the next tick
        self.assertEqual(len([r for r in b.store.unposted() if r[1] == kept.id]), 0)

    async def test_setup_refuses_public_channels_and_missing_permissions(self):
        everyone_sees = discord.Permissions(view_channel=True)
        private = discord.Permissions.none()
        full = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
                                   create_public_threads=True, send_messages_in_threads=True)
        self.assertEqual(bot.channel_problems("page", full, private), [])
        self.assertEqual(bot.channel_problems("page", full, everyone_sees), ["@everyone can see it; make it private and add the bot"])
        no_threads = discord.Permissions(view_channel=True, send_messages=True, embed_links=True, read_message_history=True)
        self.assertEqual(bot.channel_problems("ticket", no_threads, private), [])
        self.assertEqual(bot.channel_problems("page", no_threads, private),
                         ["the bot lacks create public threads, send messages in threads"])
        self.assertEqual(bot.channel_problems("watchdog", private, private),
                         ["the bot lacks view channel, send messages, embed links, read message history"])

    async def test_heartbeat_forwards_an_empty_ping_and_keeps_alertmanagers_body(self):
        seen = []

        async def ping(request):
            seen.append((request.method, len(await request.read())))
            return web.Response(text="OK" if request.path == "/up" else "nope", status=200 if request.path == "/up" else 500)
        check = TestServer(web.Application())
        check.app.router.add_route("*", "/{path}", ping)
        await check.start_server()
        self.addAsyncCleanup(check.close)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        with open(os.path.join(d.name, "heartbeat_url_agent_observability"), "w") as f:
            f.write(str(check.make_url("/up")))
        with open(os.path.join(d.name, "heartbeat_url_nas"), "w") as f:
            f.write(str(check.make_url("/down")))
        old, bot.HEARTBEAT_DIR = bot.HEARTBEAT_DIR, d.name
        self.addCleanup(setattr, bot, "HEARTBEAT_DIR", old)
        b = make_bot()
        self.addCleanup(b.store.close)
        import aiohttp
        b.session = aiohttp.ClientSession()
        self.addAsyncCleanup(b.session.close)
        client = TestClient(TestServer(bot.web_app(b)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        body = {"externalURL": "http://192.0.2.1:9093", "alerts": [{"labels": {"alertname": "Watchdog"}}]}
        self.assertEqual((await client.post("/heartbeat/agent-observability", json=body)).status, 200)
        self.assertEqual(seen, [("GET", 0)])
        self.assertEqual((await client.post("/heartbeat/nas", json=body)).status, 502)
        self.assertEqual((await client.post("/heartbeat/missing", json=body)).status, 404)
        self.assertEqual((await client.post("/heartbeat/..%2Fetc", json=body)).status, 404)
        metrics = await (await client.get("/metrics")).text()
        self.assertIn('pager_heartbeat_last_ping_timestamp_seconds{name="agent-observability"}', metrics)
        self.assertNotIn('name="nas"', metrics)

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
