"""Pager on Discord: an incident per page with Acknowledge, Resolve and Escalate buttons, a thread per
incident for its timeline, the escalation policy from rotation.yml, and the read-only responder.

In: Alertmanager webhooks on :9096 (/alerts/page, /alerts/ticket), and button presses and slash
commands over the Discord gateway, which the bot opens outward: no public endpoint. Out: messages
in the pager server's private channels, the redacted public record of each page to alert-issues
(which keeps it on the page's board issue), and metrics on /metrics.

Secrets: the bot token file only. Channel IDs and the links between people in rotation.yml and
Discord accounts live in the state database, set with /pager setup and /pager iam. The bot asks
for no privileged intent and reads no message text.
"""

import asyncio
import logging
import os
import re
import time
from datetime import timedelta

import aiohttp
import discord
import yaml
from aiohttp import web
from discord import app_commands

from core import Pager, Rotation, Store, level_reached, public_view
from diagnose import diagnose

log = logging.getLogger("pager")

TOKEN_FILE = os.environ.get("DISCORD_TOKEN_FILE", "/secrets/discord_bot_token")
ROTATION_FILE = os.environ.get("ROTATION_FILE", "/etc/pager/rotation.yml")
RUNBOOKS_FILE = os.environ.get("RUNBOOKS_FILE", "/etc/pager/runbooks.yml")
STATE_DB = os.environ.get("STATE_DB", "/data/pager.db")
PORT = int(os.environ.get("PORT", "9096"))
ALERT_ISSUES = os.environ.get("ALERT_ISSUES_URL", "http://alert-issues:9095")
# name=url pairs, comma-separated; the name is the Prometheus's external label "prometheus".
PROMETHEUS = dict(pair.split("=", 1) for pair in
                  os.environ.get("PROMETHEUS_URLS", "agent-observability=http://prometheus:9090").split(",") if "=" in pair)
TICK_SECONDS = 15
# The outside heartbeat checks' ping URLs, one file per Prometheus: heartbeat_url_<name with _ for ->.
HEARTBEAT_DIR = os.environ.get("HEARTBEAT_DIR", "/etc/pager/secrets")
HEARTBEAT_NAME = re.compile(r"[a-z0-9-]{1,40}")
COLOURS = {"triggered": 0xD93F3F, "acknowledged": 0xE8A33D, "resolved": 0x3BA55C}
CHANNELS = ("page", "ticket", "watchdog")
# What the bot needs in each channel; the invite grants these server-wide, a private channel must grant them again.
NEEDS = {"page": ("view_channel", "send_messages", "embed_links", "read_message_history",
                  "create_public_threads", "send_messages_in_threads"),
         "ticket": ("view_channel", "send_messages", "embed_links", "read_message_history"),
         "watchdog": ("view_channel", "send_messages", "embed_links", "read_message_history")}


def channel_problems(kind, mine, everyone):
    """Why a channel cannot take this kind of message: permissions the bot lacks there, or a channel that
    @everyone can see (pages name people and carry the responder's diagnosis). mine and everyone are the
    channel's discord.Permissions for the bot and for @everyone."""
    problems = []
    missing = [p.replace("_", " ") for p in NEEDS[kind] if not getattr(mine, p)]
    if missing:
        problems.append("the bot lacks " + ", ".join(missing))
    if everyone.view_channel:
        problems.append("@everyone can see it; make it private and add the bot")
    return problems


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def minutes(td):
    m = int(td.total_seconds() // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h {m % 60} min"


class ActionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pager:(?P<action>ack|resolve|escalate):(?P<id>[0-9]+)"):
    """A button that survives restarts: its custom_id carries the action and the incident."""

    LOOK = {"ack": ("Acknowledge", discord.ButtonStyle.primary),
            "resolve": ("Resolve", discord.ButtonStyle.success),
            "escalate": ("Escalate", discord.ButtonStyle.secondary)}

    def __init__(self, action, incident_id, disabled=False):
        label, style = self.LOOK[action]
        super().__init__(discord.ui.Button(label=label, style=style, disabled=disabled,
                                           custom_id=f"pager:{action}:{incident_id}"))
        self.action, self.incident_id = action, incident_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction):
        await interaction.client.on_button(interaction, self.action, self.incident_id)


class PagerBot(discord.Client):
    def __init__(self, pager, runbooks):
        intents = discord.Intents.none()
        intents.guilds = True  # channel and thread cache; not privileged
        super().__init__(intents=intents,
                         allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True))
        self.pager, self.store, self.runbooks = pager, pager.store, runbooks
        self.tree = app_commands.CommandTree(self)
        self.dirty, self.public_dirty = set(), set()
        self.tick_lock = asyncio.Lock()
        self.rotation_mtime, self.rotation_ok = 0, True
        self.session = None  # aiohttp, for Prometheus and alert-issues; discord.Client owns self.http
        self.issue_tries = {}
        self.errors = 0  # failed Discord calls; PagerFailing alerts on its increase
        self.heartbeats = {}  # name -> time of the last ping the outside check accepted
        add_commands(self)

    # ------------------------------------------------------------ lifecycle

    async def setup_hook(self):
        self.add_dynamic_items(ActionButton)

    async def on_ready(self):
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        log.info("connected as %s to %d server(s)", self.user, len(self.guilds))
        await self.say("watchdog", f"Pager online: {len(self.store.open_incidents('page'))} open pages, "
                                   f"rotation {'loaded' if self.rotation_ok else 'NOT loaded, check the logs'}.")
        self.kick()

    def channel_for(self, kind):
        cid = self.store.setting(f"channel_{kind}")
        return self.get_channel(int(cid)) if cid else None

    def connected(self):
        return self.is_ready() and self.channel_for("page") is not None

    async def say(self, kind, text):
        ch = self.channel_for(kind) if self.is_ready() else None
        if ch:
            await ch.send(text)

    def kick(self):
        asyncio.get_running_loop().create_task(self.tick())

    def reload_rotation(self):
        try:
            mtime = os.stat(ROTATION_FILE).st_mtime
            if mtime != self.rotation_mtime:
                self.pager.rotation = Rotation(load_yaml(ROTATION_FILE))
                self.rotation_mtime, self.rotation_ok = mtime, True
                log.info("rotation loaded from %s", ROTATION_FILE)
        except Exception as e:  # keep the last good rotation
            if self.rotation_ok:
                log.error("rotation not reloaded: %s", e)
            self.rotation_ok = False

    # ------------------------------------------------------------ the loop

    async def ticker(self):
        while True:
            await asyncio.sleep(TICK_SECONDS)
            await self.tick()

    async def tick(self):
        async with self.tick_lock:
            try:
                self.reload_rotation()
                for inc in self.pager.sweep():
                    self.dirty.add(inc.id)
                    self.public_dirty.add(inc.id)
                if self.connected():
                    await self.post_missing()
                    for inc, actions in self.pager.due():
                        for action in actions:
                            if not await self.guard(inc, self.perform(inc, action), "action"):
                                break  # recorded only once done, so the next tick tries again
                            self.pager.record(inc, action)
                            self.dirty.add(inc.id)
                            self.public_dirty.add(inc.id)
                    await self.post_events()
                    await self.refresh()
                await self.sync_public()
            except Exception:
                log.exception("tick failed")

    async def guard(self, inc, coro, what):
        """Run one Discord call for one incident; a failure stays with that incident. A message or thread
        someone deleted is forgotten, so the incident is posted again on the next tick."""
        try:
            await coro
            return True
        except discord.NotFound:
            self.errors += 1
            log.warning("#%d: message or thread is gone (%s); posting it again", inc.id, what)
            inc = self.store.get(inc.id)
            inc.message_id = inc.thread_id = None
            self.store.save(inc)
        except Exception as e:  # Forbidden, a Discord outage, a bug: counted, logged, next incident
            self.errors += 1
            log.warning("#%d: %s failed: %s: %s", inc.id, what, type(e).__name__, e)
        return False

    async def post_missing(self):
        for kind in ("page", "ticket"):
            for inc in self.store.open_incidents(kind):
                if not inc.message_id:
                    await self.guard(inc, self.post_incident(inc), "post")

    async def post_incident(self, inc):
        ch = self.channel_for(inc.kind)
        if not ch:
            return
        msg = await ch.send(embed=self.embed(inc), view=self.view(inc) if inc.kind == "page" else None)
        inc.message_id = msg.id
        if inc.kind == "page":
            thread = await msg.create_thread(name=f"#{inc.id} {inc.alertname}"[:100], auto_archive_duration=1440)
            inc.thread_id = thread.id
        self.store.save(inc)

    async def perform(self, inc, action):
        if action.kind == "agent":
            text = await diagnose(inc, self.query, self.runbooks)
            await self.thread_send(inc, text)
        # notify and renotify are posted from their timeline event, with the mention

    async def thread_send(self, inc, text):
        if not inc.thread_id:
            return False
        thread = self.get_channel(inc.thread_id) or await self.fetch_channel(inc.thread_id)
        await thread.send(text[:2000])
        return True

    async def post_events(self):
        names = {p["name"]: key for key, p in self.pager.rotation.people.items()}
        for rowid, incident_id, at, kind, actor, detail in self.store.unposted():
            inc = self.store.get(incident_id)
            if inc.kind != "page":
                self.store.mark_posted(rowid)
                continue
            if not inc.thread_id:
                if inc.status == "resolved":
                    self.store.mark_posted(rowid)  # resolved before it ever reached Discord
                continue  # the thread comes with the message; post later
            stamp = f"`{at[11:16]} UTC`"
            if kind in ("notified", "re-notified"):
                did = self.store.discord_id(names.get(actor, ""))
                who = f"<@{did}>" if did else actor
                line = f"{stamp} {who}: {inc.alertname} needs an acknowledgement ({detail})."
            else:
                line = f"{stamp} **{kind}** by {actor}" + (f": {detail}" if detail else "")
            sent = []

            async def send():
                sent.append(await self.thread_send(inc, line))
            if await self.guard(inc, send(), "timeline") and sent[0]:
                self.store.mark_posted(rowid)

    async def refresh(self):
        for incident_id in list(self.dirty):
            inc = self.store.get(incident_id)
            ch = self.channel_for(inc.kind)
            keep = False
            if inc.message_id and ch:
                if inc.kind == "ticket" and not self.store.setting(f"issue:{inc.id}"):
                    await self.find_issue(inc)
                    # alert-issues gets the same notification at the same time; look again for about 5 minutes
                    tries = self.issue_tries[inc.id] = self.issue_tries.get(inc.id, 0) + 1
                    keep = not self.store.setting(f"issue:{inc.id}") and inc.status != "resolved" and tries < 20
                await self.guard(inc, ch.get_partial_message(inc.message_id).edit(
                    embed=self.embed(inc), view=self.view(inc) if inc.kind == "page" else None), "update")
            if not keep:
                self.dirty.discard(incident_id)

    # ------------------------------------------------------------ rendering

    def embed(self, inc):
        now, rot = self.pager.clock(), self.pager.rotation
        label = "PAGE" if inc.kind == "page" else "TICKET"
        e = discord.Embed(title=f"{label} #{inc.id} {inc.status.upper()}: {inc.alertname}"[:256],
                          description=(inc.summary or "")[:1000] or None, colour=COLOURS[inc.status])
        e.add_field(name="Severity", value=inc.severity or "?", inline=True)
        e.add_field(name="Prometheus", value=inc.prometheus or "?", inline=True)
        e.add_field(name="Fired", value=discord.utils.format_dt(inc.created_at, "f"), inline=True)
        if inc.kind == "page" and inc.status != "resolved":
            level = level_reached(inc, rot, now, self.pager.overrides()) + 1
            e.add_field(name="Escalation", value=f"level {level} of {len(rot.rules)}", inline=True)
        if inc.acked_at:
            e.add_field(name="Acknowledged", value=f"{rot.name(inc.acked_by)}, after {minutes(inc.acked_at - inc.created_at)}", inline=True)
        if inc.resolved_at:
            e.add_field(name="Resolved", value=f"{rot.name(inc.resolved_by)}, after {minutes(inc.resolved_at - inc.created_at)}", inline=True)
        issue = self.store.setting(f"issue:{inc.id}")
        if issue:
            e.add_field(name="Board issue", value=issue, inline=False)
        if inc.kind == "page":
            e.set_footer(text="Acknowledge stops the escalation. Alertmanager resolves the page when the alert clears.")
        return e

    def view(self, inc):
        rot, now = self.pager.rotation, self.pager.clock()
        last = level_reached(inc, rot, now, self.pager.overrides()) + 1 >= len(rot.rules)
        v = discord.ui.View(timeout=None)
        v.add_item(ActionButton("ack", inc.id, disabled=inc.status != "triggered"))
        v.add_item(ActionButton("resolve", inc.id, disabled=inc.status == "resolved"))
        v.add_item(ActionButton("escalate", inc.id, disabled=inc.status == "resolved" or last))
        return v

    # ------------------------------------------------------------ buttons

    async def on_button(self, interaction, action, incident_id):
        inc = self.store.get(incident_id)
        person = self.store.person_for(interaction.user.id)
        if inc is None:
            return await interaction.response.send_message("No such incident.", ephemeral=True)
        if person is None or self.pager.rotation.kind(person) != "human":
            return await interaction.response.send_message(
                "Your account is not on the rotation. A server admin links it with /pager iam.", ephemeral=True)
        done = {"ack": self.pager.acknowledge, "resolve": self.pager.resolve,
                "escalate": self.pager.escalate}[action](inc, person)
        inc = self.store.get(incident_id)
        await interaction.response.edit_message(embed=self.embed(inc), view=self.view(inc))
        if not done:
            await interaction.followup.send(f"Nothing to do: the page is {inc.status}"
                                            + (" or at its last level." if action == "escalate" else "."), ephemeral=True)
        self.public_dirty.add(inc.id)
        self.kick()

    # ------------------------------------------------------------ outside calls (read-only and internal)

    async def query(self, prometheus, promql):
        base = PROMETHEUS.get(prometheus)
        if not base:
            raise LookupError(f"no Prometheus URL for {prometheus!r}")
        async with self.session.get(f"{base}/api/v1/query", params={"query": promql},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        if data.get("status") != "success":
            raise RuntimeError(data.get("error", "query failed"))
        return data["data"]["result"]

    async def find_issue(self, inc):
        try:
            async with self.session.get(f"{ALERT_ISSUES}/issues/{inc.fingerprint}", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    self.store.setting(f"issue:{inc.id}", (await r.json())["url"])
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError) as e:
            log.warning("issue lookup for #%d failed: %s", inc.id, e)

    async def sync_public(self):
        """The redacted record of each changed page goes to its board issue through alert-issues."""
        for incident_id in list(self.public_dirty):
            inc = self.store.get(incident_id)
            if inc is None or inc.kind != "page":
                self.public_dirty.discard(incident_id)
                continue
            body = {"fingerprint": inc.fingerprint, "starts_at": inc.starts_at,
                    "pager": public_view(inc, self.pager.rotation)}
            try:
                async with self.session.post(f"{ALERT_ISSUES}/events", json=body, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200 or (r.status == 404 and inc.status == "resolved"):
                        self.public_dirty.discard(incident_id)
                    elif r.status != 404:  # 404 while open: the issue does not exist yet; try again next tick
                        log.warning("public record for #%d: alert-issues answered %d", inc.id, r.status)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("public record for #%d: %s", inc.id, e)


# ---------------------------------------------------------------- slash commands

def add_commands(bot):
    rot = bot.pager.rotation
    humans = [app_commands.Choice(name=p["name"], value=k) for k, p in rot.people.items() if p["kind"] == "human"]
    everyone = [app_commands.Choice(name=p["name"], value=k) for k, p in rot.people.items()]
    schedules = [app_commands.Choice(name=s, value=s) for s in rot.schedules]

    admin = app_commands.Group(name="pager", description="Pager setup and shift changes (server admins)",
                               default_permissions=discord.Permissions(administrator=True), guild_only=True)

    @admin.command(name="setup", description="Pick three private channels: pages, tickets, the alerting path")
    @app_commands.describe(pages="Private channel for pages (buttons and threads)", tickets="Private channel for tickets",
                           watchdog="Private channel for the alerting path")
    async def setup(interaction: discord.Interaction, pages: discord.TextChannel, tickets: discord.TextChannel,
                    watchdog: discord.TextChannel):
        chosen = list(zip(CHANNELS, (pages, tickets, watchdog)))
        me, everyone = interaction.guild.me, interaction.guild.default_role
        lines = []
        if len({ch.id for _, ch in chosen}) < 3:
            lines.append("Pick three different channels.")
        for kind, ch in chosen:
            for problem in channel_problems(kind, ch.permissions_for(me), ch.permissions_for(everyone)):
                lines.append(f"{ch.mention} ({kind}s): {problem}.")
        if lines:
            log.warning("setup refused: %d problems", len(lines))
            return await interaction.response.send_message("Nothing changed.\n" + "\n".join(lines), ephemeral=True)
        for kind, ch in chosen:
            bot.store.setting(f"channel_{kind}", ch.id)
        await interaction.response.send_message(
            f"Pages go to {pages.mention}, tickets to {tickets.mention}, the alerting path to {watchdog.mention}.", ephemeral=True)
        await bot.say("watchdog", "Pager set up: this channel gets the alerting path.")
        bot.kick()

    @admin.command(name="iam", description="Link your Discord account to a person in rotation.yml")
    @app_commands.choices(person=humans)
    async def iam(interaction: discord.Interaction, person: app_commands.Choice[str]):
        bot.store.link(person.value, interaction.user.id)
        await interaction.response.send_message(f"You are {person.name} on the rotation.", ephemeral=True)

    @admin.command(name="override", description="Put someone on call for a while, over the schedule")
    @app_commands.choices(person=everyone, schedule=schedules)
    async def override(interaction: discord.Interaction, person: app_commands.Choice[str],
                       hours: app_commands.Range[int, 1, 48], starts_in_hours: app_commands.Range[int, 0, 168] = 0,
                       schedule: app_commands.Choice[str] = None):
        start = bot.pager.clock() + timedelta(hours=starts_in_hours)
        o = bot.pager.rotation.override({"person": person.value, "start": start, "end": start + timedelta(hours=hours),
                                         "schedule": schedule.value if schedule else bot.pager.rotation.rules[0].schedule}, "command")
        bot.store.add_override(o, str(interaction.user.id))
        text = (f"Override: {person.name} is on call for {o.schedule} from {discord.utils.format_dt(o.start, 'f')} "
                f"to {discord.utils.format_dt(o.end, 'f')}.")
        await interaction.response.send_message(text)
        bot.kick()

    @app_commands.command(name="oncall", description="Who is on call now, and the escalation policy")
    async def oncall(interaction: discord.Interaction):
        r, now = bot.pager.rotation, bot.pager.clock()
        e = discord.Embed(title="On call now", colour=0x5865F2)
        for name, person in bot.pager.on_call_now().items():
            e.add_field(name=name, value=r.name(person) if person else "nobody", inline=True)
        steps = [f"{minutes(rule.after) if rule.after else 'at once'}: {rule.schedule}" for rule in r.rules]
        e.add_field(name=f"Escalation policy: {r.policy_name}",
                    value="\n".join(steps) + f"\nre-notify every {minutes(r.renotify_after)}, {r.renotify_limit} times", inline=False)
        active = [o for o in [*r.overrides, *bot.pager.overrides()] if o.end > now]
        if active:
            e.add_field(name="Overrides", value="\n".join(
                f"{r.name(o.person)} on {o.schedule}, {discord.utils.format_dt(o.start, 'f')} to {discord.utils.format_dt(o.end, 'f')}"
                for o in active[:10]), inline=False)
        await interaction.response.send_message(embed=e)

    bot.tree.add_command(admin)
    bot.tree.add_command(oncall)


# ---------------------------------------------------------------- Alertmanager webhook and metrics

def web_app(bot):
    async def alerts(request, kind):
        try:
            payload = await request.json()
            items = payload["alerts"]
            assert isinstance(items, list)
        except (ValueError, KeyError, AssertionError):
            return web.Response(status=400, text="bad payload\n")
        changes = []
        for alert in items:
            if kind == "ticket" and (alert.get("labels") or {}).get("severity") == "page":
                continue  # a page is already an incident in #pager
            try:
                inc, change = bot.pager.on_alert(alert, kind)
            except ValueError as e:
                return web.Response(status=400, text=f"{e}\n")
            changes.append(f"{change} #{inc.id}" if inc else change)
            if inc and change != "ignored":
                bot.dirty.add(inc.id)
                if kind == "page":
                    bot.public_dirty.add(inc.id)
        log.info("%s %s: %s", kind, payload.get("groupKey", "?"), ", ".join(changes) or "nothing")
        bot.kick()
        return web.Response(text="ok\n")

    async def metrics(request):
        lines = [
            "# HELP pager_discord_connected 1 when the bot is connected to Discord and its channels are set.",
            "# TYPE pager_discord_connected gauge",
            f"pager_discord_connected {int(bot.connected())}",
            "# HELP pager_rotation_ok 0 when the last change to rotation.yml failed to load.",
            "# TYPE pager_rotation_ok gauge",
            f"pager_rotation_ok {int(bot.rotation_ok)}",
            "# HELP pager_incidents_open Open incidents by kind.",
            "# TYPE pager_incidents_open gauge",
        ]
        for kind in ("page", "ticket"):
            lines.append(f'pager_incidents_open{{kind="{kind}"}} {len(bot.store.open_incidents(kind))}')
        lines += ["# HELP pager_tick_errors_total Discord calls that failed for one incident (post, update, timeline, action).",
                  "# TYPE pager_tick_errors_total counter",
                  f"pager_tick_errors_total {bot.errors}"]
        lines += ["# HELP pager_heartbeat_last_ping_timestamp_seconds Last ping the outside heartbeat check accepted.",
                  "# TYPE pager_heartbeat_last_ping_timestamp_seconds gauge"]
        lines += [f'pager_heartbeat_last_ping_timestamp_seconds{{name="{n}"}} {t:.0f}' for n, t in sorted(bot.heartbeats.items())]
        lines += ["# HELP pager_incidents_unacknowledged Open pages nobody acknowledged.",
                  "# TYPE pager_incidents_unacknowledged gauge",
                  f"pager_incidents_unacknowledged {sum(i.status == 'triggered' for i in bot.store.open_incidents('page'))}"]
        return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")

    async def pages(request):
        return await alerts(request, "page")

    async def tickets(request):
        return await alerts(request, "ticket")

    async def heartbeat(request):
        """Alertmanager's Watchdog notification in, an empty GET to the outside check out. Alertmanager's
        JSON (its address, the generator URL, labels) stays in the house; the check sees only the ping."""
        name = request.match_info["name"]
        await request.read()  # read and drop the body
        if not HEARTBEAT_NAME.fullmatch(name):
            return web.Response(status=404, text="no such check\n")
        try:
            with open(os.path.join(HEARTBEAT_DIR, f"heartbeat_url_{name.replace('-', '_')}")) as f:
                url = f.read().strip()
        except OSError:
            log.warning("heartbeat %s: no ping URL file", name)  # never log the URL itself
            return web.Response(status=404, text="no ping URL for this check\n")
        try:
            async with bot.session.get(url, headers={"User-Agent": "pager"}, timeout=aiohttp.ClientTimeout(total=10)) as r:
                ok = r.status < 300
                if not ok:
                    log.warning("heartbeat %s: check answered %d", name, r.status)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("heartbeat %s: %s", name, type(e).__name__)
            ok = False
        if not ok:
            return web.Response(status=502, text="the check did not take the ping\n")  # Alertmanager retries
        bot.heartbeats[name] = time.time()
        return web.Response(text="ok\n")

    async def healthz(request):
        return web.Response(text="ok\n")

    app = web.Application(client_max_size=1 << 20)
    app.router.add_post("/alerts/page", pages)
    app.router.add_post("/alerts/ticket", tickets)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/heartbeat/{name}", heartbeat)
    return app


async def run():
    pager = Pager(Store(STATE_DB), Rotation(load_yaml(ROTATION_FILE)))
    bot = PagerBot(pager, load_yaml(RUNBOOKS_FILE) or [])
    bot.rotation_mtime = os.stat(ROTATION_FILE).st_mtime
    bot.session = aiohttp.ClientSession()
    bot.public_dirty.update(i.id for i in pager.store.recent(50) if i.kind == "page")  # idempotent on the issue side
    runner = web.AppRunner(web_app(bot), access_log=None)  # every scrape would log a line
    await runner.setup()
    await web.TCPSite(runner, port=PORT).start()
    log.info("pager webhook on :%d", PORT)
    asyncio.get_running_loop().create_task(bot.ticker())
    try:
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    except OSError:
        token = ""
    try:
        if not token:
            log.warning("no Discord token in %s: incidents are stored and posted once the bot connects", TOKEN_FILE)
            await asyncio.Event().wait()
        await bot.start(token)
    finally:
        await bot.session.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
