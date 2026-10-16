import os
import re
import json
import random
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ---------- CONFIG ----------
TOKEN = os.environ["DISCORD_TOKEN"]
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
TEST_GUILD_ID = 1550297120601542776

DAILY_PULL_LIMIT = 2
IMPORT_PASSWORD = os.environ.get("IMPORT_PASSWORD", "changeme123")  # set in Render env vars!

URL_REGEX = re.compile(r"https?://[^\s<>]+")


# ---------- PERSISTENCE ----------
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"guilds": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

data = load_data()


def get_guild_config(guild_id: int) -> dict:
    gid = str(guild_id)
    if gid not in data["guilds"]:
        data["guilds"][gid] = {
            "source_channel": None,
            "panel_channel": None,
            "panel_message": None,
            "links": [],
            # user_state: {
            #   "<user_id>": {
            #       "day": "YYYY-MM-DD",        # today's UTC date
            #       "pull_count": int,          # pulls used today
            #       "seen_urls": [url, ...]     # ALL URLs this user has EVER received
            #   }
            # }
            "user_state": {},
        }
    cfg = data["guilds"][gid]
    cfg.setdefault("user_state", {})
    return cfg


def today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def get_user_state(cfg: dict, user_id: int) -> dict:
    """Get a user's state. Daily counter resets on a new day; seen_urls persists forever."""
    uid = str(user_id)
    today = today_utc()
    state = cfg["user_state"].get(uid)

    if state is None:
        state = {"day": today, "pull_count": 0, "seen_urls": []}
        cfg["user_state"][uid] = state
        return state

    state.setdefault("seen_urls", [])

    if state.get("day") != today:
        state["day"] = today
        state["pull_count"] = 0

    return state


# ---------- BOT ----------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ---------- HEALTH SERVER ----------
async def health(request):
    return web.Response(text="OK")

async def start_webserver():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[health] listening on 0.0.0.0:{port}")


# ---------- PANEL ----------
def build_panel_embed(guild_id: int) -> discord.Embed:
    links = get_guild_config(guild_id)["links"]
    embed = discord.Embed(
        title="📎 Link Panel",
        description=(
            "Click the button below to grab a random link!\n"
            "Want to add a link? Just post it in the source channel."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Links available", value=str(len(links)), inline=True)
    embed.add_field(name="Daily limit", value=f"{DAILY_PULL_LIMIT} pulls / user", inline=True)
    embed.set_footer(text="One button. Infinite(ish) links.")
    return embed


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Get a Link!",
        style=discord.ButtonStyle.primary,
        emoji="🎲",
        custom_id="linkpanel:getlink",
    )
    async def get_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        cfg = get_guild_config(interaction.guild.id)
        links = cfg["links"]
        if not links:
            await interaction.response.send_message("📭 No links yet!", ephemeral=True)
            return

        user_id = interaction.user.id
        state = get_user_state(cfg, user_id)

        if state["pull_count"] >= DAILY_PULL_LIMIT:
            await interaction.response.send_message(
                f"⏳ You've hit your daily limit of **{DAILY_PULL_LIMIT}** links. "
                "Come back tomorrow (UTC).",
                ephemeral=True,
            )
            return

        seen = set(state["seen_urls"])
        available = [l for l in links if l["url"] not in seen]

        if not available:
            await interaction.response.send_message(
                "🎉 You've already received every link in the pool! "
                "Wait for new links to be added.",
                ephemeral=True,
            )
            return

        entry = random.choice(available)

        state["pull_count"] += 1
        state["seen_urls"].append(entry["url"])
        save_data(data)

        remaining = DAILY_PULL_LIMIT - state["pull_count"]

        embed = discord.Embed(
            title="🎲 Your random link",
            description=f"<{entry['url']}>",
            color=discord.Color.green(),
        )
        embed.set_footer(
            text=f"Originally shared by {entry['author']} • "
                 f"{remaining} pull(s) left today"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def refresh_panel(guild: discord.Guild) -> discord.Message | None:
    cfg = get_guild_config(guild.id)
    panel_id = cfg.get("panel_channel")
    if not panel_id:
        return None
    channel = guild.get_channel(panel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(panel_id)
        except discord.HTTPException:
            return None
    embed = build_panel_embed(guild.id)
    msg_id = cfg.get("panel_message")
    view = PanelView()
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return msg
        except (discord.NotFound, discord.HTTPException):
            cfg["panel_message"] = None
    msg = await channel.send(embed=embed, view=view)
    cfg["panel_message"] = msg.id
    save_data(data)
    try:
        await msg.pin()
    except discord.HTTPException:
        pass
    return msg


# ---------- EVENTS ----------
@bot.event
async def on_ready():
    bot.add_view(PanelView())

    if not getattr(bot, "_webserver_started", False):
        bot.loop.create_task(start_webserver())
        bot._webserver_started = True

    print(f"Logged in as {bot.user}")

    guild = discord.Object(id=TEST_GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    try:
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash command(s) to guild {TEST_GUILD_ID}")
    except Exception as e:
        print(f"Slash sync failed: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    cfg = get_guild_config(message.guild.id)
    if cfg.get("source_channel") != message.channel.id:
        return
    found = URL_REGEX.findall(message.content)
    if not found:
        return
    links = cfg["links"]
    added = False
    for url in found:
        if any(l["url"] == url for l in links):
            continue
        links.append({
            "url": url,
            "author_id": message.author.id,
            "author": str(message.author),
            "message_id": message.id,
        })
        added = True
    if added:
        save_data(data)
        await refresh_panel(message.guild)
        try:
            await message.add_reaction("📎")
        except discord.HTTPException:
            pass


# ---------- SETUP GROUP ----------
setup_group = app_commands.Group(
    name="setup",
    description="Configure the link collector",
    default_permissions=discord.Permissions(administrator=True),
)


@setup_group.command(name="source", description="Set the channel to watch for links")
async def setup_source(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    cfg = get_guild_config(interaction.guild.id)
    cfg["source_channel"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ Source channel set to {channel.mention}.", ephemeral=True
    )


@setup_group.command(name="panel", description="Set the channel where the link panel lives")
async def setup_panel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    cfg = get_guild_config(interaction.guild.id)
    cfg["panel_channel"] = channel.id
    cfg["panel_message"] = None
    save_data(data)
    await interaction.response.defer(ephemeral=True)
    await refresh_panel(interaction.guild)
    await interaction.followup.send(
        f"✅ Panel channel set to {channel.mention}.", ephemeral=True
    )


@setup_group.command(name="status", description="Show the current setup")
async def setup_status(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    cfg = get_guild_config(interaction.guild.id)
    src = f"<#{cfg['source_channel']}>" if cfg.get("source_channel") else "❌ not set"
    pnl = f"<#{cfg['panel_channel']}>" if cfg.get("panel_channel") else "❌ not set"
    embed = discord.Embed(title="⚙️ Link Collector Setup", color=discord.Color.green())
    embed.add_field(name="Source channel", value=src, inline=False)
    embed.add_field(name="Panel channel", value=pnl, inline=False)
    embed.add_field(name="Links stored", value=str(len(cfg["links"])), inline=False)
    embed.add_field(name="Daily limit", value=f"{DAILY_PULL_LIMIT} / user", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@setup_group.command(name="clear", description="Wipe all stored links and user history")
async def setup_clear(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return
    cfg = get_guild_config(interaction.guild.id)
    cfg["links"].clear()
    cfg["user_state"] = {}
    cfg["panel_message"] = None
    save_data(data)
    await interaction.response.defer(ephemeral=True)
    await refresh_panel(interaction.guild)
    await interaction.followup.send(
        "🗑️ Cleared all links and user history.", ephemeral=True
    )


# ---------- IMPORT COMMAND ----------
@setup_group.command(name="import", description="Import links from a .txt file (one URL per line)")
@app_commands.describe(
    file="A .txt file with one URL per line",
    password="Import password",
)
async def setup_import(
    interaction: discord.Interaction,
    file: discord.Attachment,
    password: str,
):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    if password != IMPORT_PASSWORD:
        await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
        return

    if not file.filename.lower().endswith(".txt"):
        await interaction.response.send_message(
            "❌ Only `.txt` files are supported.", ephemeral=True
        )
        return

    if file.size > 2 * 1024 * 1024:
        await interaction.response.send_message(
            "❌ File too large (max 2 MB).", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        raw = await file.read()
        text = raw.decode("utf-8", errors="ignore")
    except Exception as e:
        await interaction.followup.send(f"❌ Could not read file: `{e}`", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    links = cfg["links"]
    existing = {l["url"] for l in links}

    added = 0
    duplicates = 0
    invalid = 0

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        matches = URL_REGEX.findall(line)
        if not matches:
            invalid += 1
            continue
        for url in matches:
            if url in existing:
                duplicates += 1
                continue
            links.append({
                "url": url,
                "author_id": interaction.user.id,
                "author": f"imported by {interaction.user}",
                "message_id": 0,
            })
            existing.add(url)
            added += 1

    save_data(data)
    await refresh_panel(interaction.guild)

    await interaction.followup.send(
        f"✅ Import complete.\n"
        f"• Added: **{added}**\n"
        f"• Duplicates skipped: **{duplicates}**\n"
        f"• Invalid lines: **{invalid}**\n"
        f"• Total pool now: **{len(links)}**",
        ephemeral=True,
    )


# ---------- BACKFILL COMMAND ----------
@setup_group.command(
    name="backfill",
    description="Scan the source channel's history and import all links found",
)
@app_commands.describe(
    password="Import password",
    limit="How many recent messages to scan (default 10000, max 100000)",
)
async def setup_backfill(
    interaction: discord.Interaction,
    password: str,
    limit: int = 10000,
):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    if password != IMPORT_PASSWORD:
        await interaction.response.send_message("❌ Wrong password.", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    source_id = cfg.get("source_channel")
    if not source_id:
        await interaction.response.send_message(
            "❌ No source channel set. Run `/setup source` first.", ephemeral=True
        )
        return

    channel = interaction.guild.get_channel(source_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(source_id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Could not access the source channel.", ephemeral=True
            )
            return

    limit = max(1, min(limit, 100000))

    await interaction.response.defer(ephemeral=True)

    links = cfg["links"]
    existing = {l["url"] for l in links}

    scanned = 0
    added = 0
    duplicates = 0

    try:
        async for msg in channel.history(limit=limit, oldest_first=False):
            scanned += 1

            if msg.author.bot:
                continue

            found = URL_REGEX.findall(msg.content)
            if not found:
                # also check embeds (sometimes URLs live in embeds only)
                for emb in msg.embeds:
                    if emb.url:
                        found.append(emb.url)
                if not found:
                    continue

            for url in found:
                if url in existing:
                    duplicates += 1
                    continue
                links.append({
                    "url": url,
                    "author_id": msg.author.id,
                    "author": str(msg.author),
                    "message_id": msg.id,
                })
                existing.add(url)
                added += 1
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Missing permission to read message history in the source channel.",
            ephemeral=True,
        )
        return
    except Exception as e:
        await interaction.followup.send(f"❌ Backfill failed: `{e}`", ephemeral=True)
        return

    save_data(data)
    await refresh_panel(interaction.guild)

    await interaction.followup.send(
        f"✅ Backfill complete.\n"
        f"• Messages scanned: **{scanned}**\n"
        f"• Added: **{added}**\n"
        f"• Duplicates skipped: **{duplicates}**\n"
        f"• Total pool now: **{len(links)}**",
        ephemeral=True,
    )


tree.add_command(setup_group)


# ---------- RUN ----------
bot.run(TOKEN)