import os
import re
import json
import hmac
import random
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ---------- CONFIG ----------
TOKEN = os.environ.get("DISCORD_TOKEN")
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
TEST_GUILD_ID = 1550297120601542776

DAILY_PULL_LIMIT = 2
# No default on purpose: if IMPORT_PASSWORD isn't set, /setup import stays disabled.
IMPORT_PASSWORD = os.environ.get("IMPORT_PASSWORD", "")

URL_REGEX = re.compile(r"https?://[^\s<>\)\]]+")


# ---------- PERSISTENCE ----------
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"guilds": {}}


def save_data(data):
    # Write to a temp file first so a crash mid-write can't corrupt the real file
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)


data = load_data()


def get_guild_config(guild_id: int) -> dict:
    gid = str(guild_id)
    if gid not in data["guilds"]:
        data["guilds"][gid] = {
            "source_channel": None,
            "panel_channel": None,
            "panel_message": None,
            "links": [],
            "user_state": {},
        }
    cfg = data["guilds"][gid]
    cfg.setdefault("user_state", {})
    return cfg


def today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def get_user_state(cfg: dict, user_id: int) -> dict:
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


# ---------- LINK HELPERS ----------
def extract_urls(text: str) -> list[str]:
    """Finds every URL in a block of text (trailing punctuation trimmed, duplicates removed, order kept)."""
    seen = set()
    urls = []
    for raw in URL_REGEX.findall(text or ""):
        url = raw.rstrip(".,;:!?\"'")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def add_links(cfg: dict, urls: list[str], author_id: int, author: str, message_id: int = 0) -> int:
    """Adds URLs that aren't already in the pool. Returns how many were actually new."""
    links = cfg["links"]
    existing = {l["url"] for l in links}
    added = 0
    for url in urls:
        if url in existing:
            continue
        existing.add(url)
        links.append({
            "url": url,
            "author_id": author_id,
            "author": author,
            "message_id": message_id,
        })
        added += 1
    return added


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
    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        print(f"[panel] could not post panel: {e!r}")
        return None
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

    # on_ready can fire again after a reconnect, so only sync once per run
    if not getattr(bot, "_synced", False):
        guild = discord.Object(id=TEST_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        try:
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} slash command(s) to guild {TEST_GUILD_ID}")
            bot._synced = True
        except Exception as e:
            print(f"Slash sync failed: {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    cfg = get_guild_config(message.guild.id)
    if cfg.get("source_channel") != message.channel.id:
        return
    urls = extract_urls(message.content)
    if not urls:
        return
    added = add_links(cfg, urls, message.author.id, str(message.author), message.id)
    if not added:
        return  # everything in that message was already stored
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
    guild_only=True,
)


@setup_group.command(name="source", description="Set the channel to watch for links")
@app_commands.checks.has_permissions(administrator=True)
async def setup_source(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_guild_config(interaction.guild.id)
    cfg["source_channel"] = channel.id
    save_data(data)
    await interaction.response.send_message(
        f"✅ Source channel set to {channel.mention}.\n"
        "New links posted there are collected automatically. "
        "To also collect the links already in the channel, run `/setup scan`.",
        ephemeral=True,
    )


@setup_group.command(name="panel", description="Set the channel where the link panel lives")
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel(interaction: discord.Interaction, channel: discord.TextChannel):
    cfg = get_guild_config(interaction.guild.id)
    cfg["panel_channel"] = channel.id
    cfg["panel_message"] = None
    save_data(data)
    await interaction.response.defer(ephemeral=True)
    msg = await refresh_panel(interaction.guild)
    if msg is None:
        await interaction.followup.send(
            f"⚠️ Panel channel set to {channel.mention}, but I couldn't post there. "
            "Check that I can view the channel, send messages and embed links.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"✅ Panel channel set to {channel.mention}.", ephemeral=True
    )


@setup_group.command(name="status", description="Show the current setup")
@app_commands.checks.has_permissions(administrator=True)
async def setup_status(interaction: discord.Interaction):
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
@app_commands.checks.has_permissions(administrator=True)
async def setup_clear(interaction: discord.Interaction):
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


# ---------- SCAN CHANNEL HISTORY ----------
@setup_group.command(
    name="scan",
    description="Read the source channel's past messages and store every link found",
)
@app_commands.describe(
    limit="How many recent messages to read (0 = the whole channel)",
)
@app_commands.checks.has_permissions(administrator=True)
async def setup_scan(
    interaction: discord.Interaction,
    limit: app_commands.Range[int, 0, 1000000] = 0,
):
    cfg = get_guild_config(interaction.guild.id)
    source_id = cfg.get("source_channel")
    if not source_id:
        await interaction.response.send_message(
            "❌ Set a source channel first with `/setup source`.", ephemeral=True
        )
        return

    channel = interaction.guild.get_channel(source_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(source_id)
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ I can't find the source channel anymore. Set it again with `/setup source`.",
                ephemeral=True,
            )
            return

    await interaction.response.defer(ephemeral=True, thinking=True)

    scanned = 0
    found = 0
    added = 0
    try:
        async for message in channel.history(limit=limit or None, oldest_first=True):
            scanned += 1
            if message.author.bot:
                continue
            urls = extract_urls(message.content)
            if not urls:
                continue
            found += len(urls)
            added += add_links(cfg, urls, message.author.id, str(message.author), message.id)
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ I can't read the history of {channel.mention}. "
            "Give me **View Channel** and **Read Message History** there.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        print(f"[scan] failed: {e!r}")
        await interaction.followup.send(
            "❌ Discord returned an error while reading the channel. Try again in a bit.",
            ephemeral=True,
        )
        return

    save_data(data)
    await refresh_panel(interaction.guild)

    result = (
        f"✅ Scanned **{scanned}** message(s) in {channel.mention}.\n"
        f"Links found: **{found}** | New: **{added}** | Already stored: **{found - added}**\n"
        f"Total pool now: **{len(cfg['links'])}**"
    )
    try:
        await interaction.followup.send(result, ephemeral=True)
    except discord.HTTPException:
        # The interaction expired during a very long scan, so post the result in the channel instead
        try:
            await channel.send(result)
        except discord.HTTPException:
            pass


# ---------- ADD LINKS (paste blob) ----------
@setup_group.command(
    name="addlinks",
    description="Paste a blob of text — every URL in it gets added to the pool",
)
@app_commands.describe(
    text="Paste anything containing URLs (one per line, sentences, whatever)",
)
@app_commands.checks.has_permissions(administrator=True)
async def setup_addlinks(interaction: discord.Interaction, text: str):
    urls = extract_urls(text)
    if not urls:
        await interaction.response.send_message(
            "❌ No URLs found in that text.", ephemeral=True
        )
        return

    cfg = get_guild_config(interaction.guild.id)
    added = add_links(cfg, urls, interaction.user.id, f"added by {interaction.user}")

    save_data(data)
    await refresh_panel(interaction.guild)

    await interaction.response.send_message(
        f"✅ Added **{added}** new link(s) ({len(urls) - added} already stored). "
        f"Total pool now: **{len(cfg['links'])}**",
        ephemeral=True,
    )


# ---------- IMPORT COMMAND (kept as a backup) ----------
@setup_group.command(name="import", description="Import links from a .txt file (one URL per line)")
@app_commands.describe(
    file="A .txt file with one URL per line",
    password="Import password",
)
@app_commands.checks.has_permissions(administrator=True)
async def setup_import(
    interaction: discord.Interaction,
    file: discord.Attachment,
    password: str,
):
    if not IMPORT_PASSWORD:
        await interaction.response.send_message(
            "❌ Importing is disabled: no IMPORT_PASSWORD is set on the bot.", ephemeral=True
        )
        return

    if not hmac.compare_digest(password.encode(), IMPORT_PASSWORD.encode()):
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
    urls = extract_urls(text)
    added = add_links(cfg, urls, interaction.user.id, f"imported by {interaction.user}")

    save_data(data)
    await refresh_panel(interaction.guild)

    await interaction.followup.send(
        f"✅ Import complete. Added **{added}** new ({len(urls) - added} already stored). "
        f"Total pool now: **{len(cfg['links'])}**",
        ephemeral=True,
    )


tree.add_command(setup_group)


# ---------- ERROR HANDLING ----------
@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the Administrator permission to use this."
    else:
        print(f"[command error] {error!r}")
        msg = "❌ Something went wrong running that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


# ---------- RUN ----------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN environment variable not set.")
    bot.run(TOKEN)