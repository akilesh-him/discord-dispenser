import discord
from discord import app_commands
from discord.ext import commands
import re
import json
import os
import random

# ---------- CONFIG ----------
import os
TOKEN = os.environ["DISCORD_TOKEN"]
DATA_FILE = "data.json"

URL_REGEX = re.compile(r"https?://[^\s<>]+")

# ---------- PERSISTENCE ----------
# {
#   "guilds": {
#       "<guild_id>": {
#           "source_channel": int | null,
#           "panel_channel": int | null,
#           "panel_message": int | null,
#           "links": [
#               {"url": str, "author_id": int, "author": str, "message_id": int}
#           ]
#       }
#   }
# }

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
        }
    return data["guilds"][gid]


# ---------- BOT ----------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


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
    embed.set_footer(text="One button. Infinite(ish) links.")
    return embed


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

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

        links = get_guild_config(interaction.guild.id)["links"]
        if not links:
            await interaction.response.send_message(
                "📭 No links have been posted yet!", ephemeral=True
            )
            return

        entry = random.choice(links)
        embed = discord.Embed(
            title="🎲 Your random link",
            description=f"[{entry['url']}]({entry['url']})",
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Originally shared by {entry['author']}")
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
    # Register persistent view so the button works after restarts
    bot.add_view(PanelView())

    print(f"Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
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
    author_id = message.author.id
    added = False

    for url in found:
        # Per-user dedup: this user can't add the same URL twice.
        # (Also blocks the same URL being re-added by anyone who already posted it —
        #  remove the `and l["author_id"] != author_id` part if you want global dedup.)
        already_by_user = any(
            l["url"] == url and l["author_id"] == author_id for l in links
        )
        if already_by_user:
            continue

        links.append({
            "url": url,
            "author_id": author_id,
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
@app_commands.describe(channel="Channel the bot will collect links from")
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
@app_commands.describe(channel="Channel the panel embed is posted in")
async def setup_panel(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    cfg["panel_channel"] = channel.id
    cfg["panel_message"] = None  # force fresh panel
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

    await interaction.response.send_message(embed=embed, ephemeral=True)


@setup_group.command(name="clear", description="Wipe all stored links for this server")
async def setup_clear(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Use this in a server.", ephemeral=True)
        return

    cfg = get_guild_config(interaction.guild.id)
    cfg["links"].clear()
    cfg["panel_message"] = None
    save_data(data)

    await interaction.response.defer(ephemeral=True)
    await refresh_panel(interaction.guild)
    await interaction.followup.send("🗑️ Cleared all links.", ephemeral=True)


# ---------- RUN ----------
bot.run(TOKEN)