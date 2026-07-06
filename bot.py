import asyncio
import contextlib
import datetime
import json
import logging
import os
import re
import sqlite3
import sys
import traceback

import discord
from discord import app_commands
from discord.ext import tasks

# Forces UTF-8 so emojis never break again on Windows.
sys.stdout.reconfigure(encoding="utf-8")

intents = discord.Intents.default()
intents.message_content = True

BOT_VERSION = "1.0.1"
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1523400321747910706"))
GUILD_ICON_URL = os.getenv(
    "GUILD_ICON_URL",
    "https://cdn.discordapp.com/icons/1508866190200803428/e5fc80288e30ae28c8332b609a8dc127.webp?size=640",
)
INVITE_URL = os.getenv("INVITE_URL", "https://discord.gg/6kREU4J2")
PRICE_EDITOR_ROLE_ID = int(os.getenv("PRICE_EDITOR_ROLE_ID", "1523453205311258654"))
# Single home for everything: interaction audit trail + info/warnings/errors.
BOT_LOG_CHANNEL_ID = int(os.getenv("BOT_LOG_CHANNEL_ID", "1523464553785065483"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VOTES_FILE = os.path.join(BASE_DIR, "votes.json")  # legacy — migrated into SQLite on startup
DB_FILE = os.path.join(BASE_DIR, "pricebot.db")

# A price is flagged as disputed once it has at least DISPUTED_MIN_VOTES inaccurate
# votes AND inaccurate votes make up at least DISPUTED_RATIO of the total.
DISPUTED_MIN_VOTES = int(os.getenv("DISPUTED_MIN_VOTES", "5"))
DISPUTED_RATIO = float(os.getenv("DISPUTED_RATIO", "0.6"))
# Items whose latest entry is older than this get reported by the daily sweeper.
STALE_PRICE_DAYS = int(os.getenv("STALE_PRICE_DAYS", "14"))

log = logging.getLogger("pricebot")

# Embed field labels, used as lookup keys by /editprice
FIELD_PRICE = "💰 PRICE"
FIELD_DEMAND = "📈 DEMAND"
FIELD_TREND = "📉 TREND"
FIELD_POSTED_BY = "👤 Posted by"
FIELD_NOTES = "🧠 Notes"
FIELD_STATUS = "⚠️ STATUS"
FIELD_VERSION = "\u200b"

DEMAND_CHOICES = [
    app_commands.Choice(name="🔥 High", value="High"),
    app_commands.Choice(name="📊 Decent", value="Decent"),
    app_commands.Choice(name="📉 Low", value="Low"),
]
TREND_CHOICES = [
    app_commands.Choice(name="📈 Rising", value="📈 Rising"),
    app_commands.Choice(name="📉 Falling", value="📉 Falling"),
    app_commands.Choice(name="➡️ Stable", value="➡️ Stable"),
]


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def build_description(edited: bool = False) -> str:
    suffix = " (edited)" if edited else ""
    # <t:...:f> is Discord's native timestamp — renders in each viewer's local timezone.
    return f"> **📍 PRICES UNCOVERED - LIVE** 📍\n> 📅 <t:{int(utc_now().timestamp())}:f>{suffix}"


def demand_color(demand: str) -> int:
    d = demand.lower()
    if "high" in d:
        return 0xc084fc
    if "decent" in d:
        return 0xfbbf24
    return 0x67e8f9


def demand_display(demand: str) -> str:
    d = demand.lower()
    emoji = "🔥" if "high" in d else "📊" if "decent" in d else "📉"
    return f"**{emoji} {demand}**"


def parse_embed_title(title: str) -> tuple[str, str]:
    """Split '{emoji}  NAME  {emoji}' into (emoji, name)."""
    parts = [p.strip() for p in title.split("  ") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1]
    return "🌟", title.strip()


def _load_votes() -> dict:
    try:
        with open(VOTES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# --- SQLite storage (votes + price history) ---

@contextlib.contextmanager
def db():
    """Connection with commit-on-success and guaranteed close."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS votes (
                message_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                accurate INTEGER NOT NULL,
                voted_at TEXT NOT NULL,
                PRIMARY KEY (message_id, user_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                item_key TEXT NOT NULL,
                price TEXT NOT NULL,
                demand TEXT,
                trend TEXT,
                poster_id INTEGER,
                message_id TEXT,
                channel_id TEXT,
                action TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_item_key ON prices(item_key, created_at)")


def migrate_votes_json() -> None:
    """One-time import of the legacy votes.json into SQLite."""
    if not os.path.exists(VOTES_FILE):
        return
    try:
        data = _load_votes()
        with db() as conn:
            for message_id, entry in data.items():
                # Legacy data only stored totals + voter ids, not who voted what;
                # totals are preserved, per-user accuracy is best-effort.
                accurate_left = entry.get("accurate", 0)
                for user_id in entry.get("voters", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO votes (message_id, user_id, accurate, voted_at) VALUES (?, ?, ?, ?)",
                        (str(message_id), user_id, 1 if accurate_left > 0 else 0, utc_now().isoformat()),
                    )
                    accurate_left -= 1
        os.rename(VOTES_FILE, VOTES_FILE + ".migrated")
        log.info("Migrated legacy votes.json into pricebot.db")
    except Exception:
        log.exception("Failed to migrate votes.json")


def has_voted(message_id: int, user_id: int) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM votes WHERE message_id = ? AND user_id = ?", (str(message_id), user_id)
        ).fetchone()
    return row is not None


def add_vote(message_id: int, user_id: int, accurate: bool) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO votes (message_id, user_id, accurate, voted_at) VALUES (?, ?, ?, ?)",
            (str(message_id), user_id, int(accurate), utc_now().isoformat()),
        )


def vote_counts(message_id: int) -> tuple[int, int]:
    """Returns (accurate, inaccurate) for a price message."""
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(accurate), 0) AS acc, COUNT(*) AS total FROM votes WHERE message_id = ?",
            (str(message_id),),
        ).fetchone()
    return row["acc"], row["total"] - row["acc"]


def record_price(
    item: str,
    price: str,
    demand: str | None,
    trend: str | None,
    poster_id: int,
    message: discord.Message,
    action: str,
) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO prices (item, item_key, price, demand, trend, poster_id, message_id, channel_id, action, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item, item.lower(), price, demand, trend, poster_id,
             str(message.id), str(message.channel.id), action, utc_now().isoformat()),
        )


def latest_price(item: str) -> sqlite3.Row | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM prices WHERE item_key = ? ORDER BY id DESC LIMIT 1", (item.lower(),)
        ).fetchone()
        if row is None:  # fall back to fuzzy match
            row = conn.execute(
                "SELECT * FROM prices WHERE item_key LIKE ? ORDER BY id DESC LIMIT 1", (f"%{item.lower()}%",)
            ).fetchone()
    return row


def price_history(item: str, limit: int = 10) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM prices WHERE item_key = ? ORDER BY id DESC LIMIT ?", (item.lower(), limit)
        ).fetchall()


def item_for_message(message_id: int) -> str | None:
    with db() as conn:
        row = conn.execute(
            "SELECT item FROM prices WHERE message_id = ? ORDER BY id DESC LIMIT 1", (str(message_id),)
        ).fetchone()
    return row["item"] if row else None


async def item_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT item FROM prices WHERE item_key LIKE ? ORDER BY item LIMIT 25",
            (f"%{current.lower()}%",),
        ).fetchall()
    return [app_commands.Choice(name=r["item"], value=r["item"]) for r in rows]


def extract_embed_price_data(embed: discord.Embed) -> tuple[str | None, str | None, str | None]:
    """Pull (price, demand, trend) back out of a price embed's fields."""
    price = demand = trend = None
    for f in embed.fields:
        if f.name == FIELD_PRICE and f.value:
            m = re.search(r"\n(.+?) 💎\n", f.value)
            price = m.group(1) if m else f.value
        elif f.name == FIELD_DEMAND and f.value:
            demand = f.value.strip("*")
        elif f.name == FIELD_TREND:
            trend = f.value
    return price, demand, trend


LEVEL_COLORS = {
    logging.DEBUG: 0x64748b,
    logging.INFO: 0x38bdf8,
    logging.WARNING: 0xfbbf24,
    logging.ERROR: 0xef4444,
    logging.CRITICAL: 0x991b1b,
}
LEVEL_EMOJIS = {
    logging.DEBUG: "🔧",
    logging.INFO: "ℹ️",
    logging.WARNING: "⚠️",
    logging.ERROR: "❌",
    logging.CRITICAL: "🛑",
}


class DiscordLogHandler(logging.Handler):
    """Ships log records to the bot log channel as embeds.

    Records are queued and sent by a background task so logging never blocks.
    Send failures fall back to the console, never to the logger, which would loop and then break.
    """

    def __init__(self, client: discord.Client):
        super().__init__()
        self.client = client
        self.queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue(maxsize=200)

    def emit(self, record: logging.LogRecord) -> None:
        # Drop discord.py's own INFO chatter, every message we send generates more
        # of it, which would loop forever. Its warnings/errors still come through.
        if record.name.startswith("discord") and record.levelno < logging.WARNING:
            return
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            pass

    def _record_to_embed(self, record: logging.LogRecord) -> discord.Embed:
        text = record.getMessage()
        if record.exc_info and record.exc_info[0] is not None:
            tb = "".join(traceback.format_exception(*record.exc_info))
            text += f"\n```py\n{tb[-1800:]}\n```"
        return discord.Embed(
            title=f"{LEVEL_EMOJIS.get(record.levelno, '📄')} {record.levelname} • {record.name}",
            description=text[:4000],
            color=LEVEL_COLORS.get(record.levelno, 0x64748b),
            timestamp=datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc),
        )

    async def sender_loop(self) -> None:
        await self.client.wait_until_ready()
        while not self.client.is_closed():
            record = await self.queue.get()
            channel = self.client.get_channel(BOT_LOG_CHANNEL_ID)
            if channel is None:
                continue
            try:
                await channel.send(embed=self._record_to_embed(record))
            except Exception as e:
                print(f"Failed to ship log to Discord ({e}): {record.getMessage()!r}")


class PremiumPriceView(discord.ui.View):
    """Persistent vote buttons. Vote state lives in pricebot.db keyed by message ID,
    so votes survive restarts and every user gets exactly one vote."""

    def __init__(self):
        super().__init__(timeout=None)

    @classmethod
    def with_counts(cls, acc: int, inacc: int) -> "PremiumPriceView":
        view = cls()
        view.accurate.label = f"✅ Accurate ({acc})"
        view.inaccurate.label = f"❌ Inaccurate ({inacc})"
        return view

    async def _handle_vote(self, interaction: discord.Interaction, accurate: bool):
        message = interaction.message
        if message is None:
            await interaction.response.send_message("Couldn't find the price message.", ephemeral=True)
            return

        if has_voted(message.id, interaction.user.id):
            await interaction.response.send_message("You already voted on this price.", ephemeral=True)
            return

        # Acknowledge the click immediately — the interaction token expires after ~3s,
        # so all slow work (db write, message edit, log send) must happen afterwards.
        label = "💎" if accurate else "📝"
        try:
            await interaction.response.send_message(f"{label} Your vote has been recorded.", ephemeral=True)
        except discord.NotFound:
            # Interaction expired before we could ack (heavy lag) — drop the vote.
            log.warning(f"Vote ack expired for {interaction.user} on message {message.id} — vote dropped.")
            return

        add_vote(message.id, interaction.user.id, accurate)
        acc, inacc = vote_counts(message.id)

        # Live vote counts on the buttons.
        try:
            await message.edit(view=PremiumPriceView.with_counts(acc, inacc))
        except discord.HTTPException as e:
            log.warning(f"Couldn't update vote counts on message {message.id}: {e}")

        await self._maybe_flag_disputed(message, acc, inacc)
        await self._log_vote(interaction, acc, inacc, accurate)

    @discord.ui.button(label="✅ Accurate", style=discord.ButtonStyle.green, custom_id="price_vote:accurate")
    async def accurate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, accurate=True)

    @discord.ui.button(label="❌ Inaccurate", style=discord.ButtonStyle.red, custom_id="price_vote:inaccurate")
    async def inaccurate(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, accurate=False)

    async def _maybe_flag_disputed(self, message: discord.Message, acc: int, inacc: int):
        if inacc < DISPUTED_MIN_VOTES or inacc / (acc + inacc) < DISPUTED_RATIO:
            return
        if not message.embeds:
            return
        embed = message.embeds[0]
        if _find_field(embed, FIELD_STATUS) >= 0:
            return  # already flagged

        item_name = item_for_message(message.id) or parse_embed_title(embed.title or "")[1]
        embed.insert_field_at(1, name=FIELD_STATUS, value="**⚠️ Disputed — needs a re-check**", inline=False)
        try:
            await message.edit(embed=embed)
        except discord.HTTPException as e:
            log.warning(f"Couldn't add disputed flag to message {message.id}: {e}")

        log_channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
        if log_channel:
            try:
                await log_channel.send(
                    content=f"<@&{PRICE_EDITOR_ROLE_ID}> a price has been flagged as disputed",
                    embed=discord.Embed(
                        title=f"⚠️ Disputed price - {item_name}",
                        description=f"{inacc} inaccurate vs {acc} accurate votes.\n{message.jump_url}",
                        color=0xef4444,
                        timestamp=utc_now(),
                    ),
                )
            except discord.HTTPException as e:
                log.warning(f"Couldn't send disputed alert: {e}")

    async def _log_vote(self, interaction: discord.Interaction, acc: int, inacc: int, accurate: bool):
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if not log_channel:
            return

        item_name = "Unknown Item"
        message = interaction.message
        if message and message.embeds and message.embeds[0].title:
            _, item_name = parse_embed_title(message.embeds[0].title)

        vote_type = "✅ Accurate" if accurate else "❌ Inaccurate"
        embed = discord.Embed(
            title=f"📊 Vote - {item_name}",
            description=f"{vote_type} by {interaction.user.mention}",
            color=0x22d3ee,
            timestamp=utc_now(),
        )
        embed.add_field(name="✅ Accurate", value=acc, inline=True)
        embed.add_field(name="❌ Inaccurate", value=inacc, inline=True)
        embed.set_footer(text="Prices Uncovered - Live Voting System")
        await log_channel.send(embed=embed)


class GrowPriceBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        init_db()
        migrate_votes_json()

        # Ship our logs + discord.py warnings/errors to the bot log channel.
        self.log_handler = DiscordLogHandler(self)
        self.log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self.log_handler)
        self.loop.create_task(self.log_handler.sender_loop())

        # Re-register the persistent view so buttons keep working after restarts.
        self.add_view(PremiumPriceView())
        stale_price_sweep.start()
        await self.tree.sync()
        log.info("Slash commands synced, running live.")


bot = GrowPriceBot()


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} — bot v{BOT_VERSION}, live and running.")


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    log.exception(f"Unhandled error in event {event_method}")


def _format_options(options: list) -> str:
    parts = []
    for opt in options:
        if "options" in opt:  # subcommand / group
            parts.append(f"{opt['name']} {_format_options(opt['options'])}".strip())
        else:
            parts.append(f"{opt['name']}: {opt.get('value')}")
    return " • ".join(parts)


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Log every command/button interaction to the interaction log channel."""
    # Skip autocomplete keystrokes and pings — only log real actions.
    if interaction.type not in (discord.InteractionType.application_command, discord.InteractionType.component):
        return

    log_channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
    if log_channel is None:
        return

    data = interaction.data or {}
    if interaction.type is discord.InteractionType.application_command:
        action = f"/{data.get('name', 'unknown')}"
        options = _format_options(data.get("options", []))
    else:
        action = f"🔘 Button • {data.get('custom_id', 'unknown')}"
        options = ""

    embed = discord.Embed(
        title="🧾 Interaction",
        description=f"**{action}** by {interaction.user.mention}",
        color=0x94a3b8,
        timestamp=utc_now(),
    )
    if options:
        embed.add_field(name="Options", value=options[:1024], inline=False)
    if interaction.channel:
        embed.add_field(
            name="Channel",
            value=getattr(interaction.channel, "mention", str(interaction.channel)),
            inline=True,
        )
    embed.set_footer(text=f"User ID: {interaction.user.id}")

    try:
        await log_channel.send(embed=embed)
    except discord.HTTPException as e:
        log.warning(f"Failed to log interaction: {e}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    cmd = interaction.command.qualified_name if interaction.command else "unknown"
    if isinstance(error, app_commands.CheckFailure):
        message = "❌ You don't have permission to use this command."
        log.info(f"Permission denied: /{cmd} attempted by {interaction.user} ({interaction.user.id})")
    else:
        message = f"❌ Error: {error}"
        log.error(f"Command error in /{cmd}", exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


@bot.tree.command(name="forceupdate", description="Force sync all slash commands")
@app_commands.checks.has_permissions(manage_guild=True)
async def forceupdate(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await bot.tree.sync()
        await interaction.followup.send(
            "✅ Slash commands synced with Discord (global changes can take up to an hour to appear).",
            ephemeral=True,
        )
        log.info(f"Slash commands force-synced by {interaction.user} ({interaction.user.id})")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to sync commands: {e}", ephemeral=True)
        log.exception("Slash command force-sync failed")


@bot.tree.command(name="version", description="Check the current bot version")
@app_commands.checks.has_role(PRICE_EDITOR_ROLE_ID)
async def version(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(f"🤖 Prices Uncovered Bot • **v{BOT_VERSION}**", ephemeral=True)


@bot.tree.command(name="restart", description="Restart the bot process")
async def restart(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    app_info = await bot.application_info()
    owner_ids = {m.id for m in app_info.team.members} if app_info.team else {app_info.owner.id}
    if interaction.user.id not in owner_ids:
        await interaction.followup.send("Not authorized", ephemeral=True)
        return

    await interaction.followup.send("Restarting...", ephemeral=True)
    await bot.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)


@bot.tree.command(name="postprice", description="Price Dashboard")
@app_commands.checks.has_role(PRICE_EDITOR_ROLE_ID)
@app_commands.choices(demand=DEMAND_CHOICES, trend=TREND_CHOICES)
@app_commands.describe(
    item="Item name (e.g. Hellfire Horns)",
    price="Price range (e.g. 67-72)",
    demand="Demand level",
    variant="Variant, depends on item, not to get confused later (Dark Red / Black)",
    item_emoji="Leading emoji",
    thumbnail_url="Item sprite URL (copy from Growtopia wiki)",
    banner_url="Big banner image URL (optional but makes it look better)",
    proof_pic="Proof screenshot/snippet image to add to the embed",
    trend="Price trend",
    notes="Extra intel or patch note",
    target_channel="Channel to post the price update in",
)
async def postprice(
    interaction: discord.Interaction,
    item: str,
    price: str,
    demand: str,
    variant: str | None = None,
    item_emoji: str = "🌟",
    thumbnail_url: str | None = None,
    banner_url: str | None = None,
    proof_pic: discord.Attachment | None = None,
    trend: str = "➡️ Stable",
    notes: str | None = None,
    target_channel: discord.TextChannel | None = None,
):
    await interaction.response.defer(ephemeral=True)

    try:
        full_name = f"{item} {variant or ''}".strip()

        embed = discord.Embed(
            title=f"{item_emoji}  {full_name.upper()}  {item_emoji}",
            url=f"https://growtopia.fandom.com/wiki/{item.replace(' ', '_')}",
            description=build_description(),
            color=demand_color(demand),
            timestamp=utc_now(),
        )

        embed.add_field(name=FIELD_PRICE, value=f"```fix\n{price} 💎\n```", inline=False)
        embed.add_field(name=FIELD_DEMAND, value=demand_display(demand), inline=True)
        embed.add_field(name=FIELD_TREND, value=trend, inline=True)
        embed.add_field(name=FIELD_POSTED_BY, value=interaction.user.mention, inline=True)

        if notes:
            embed.add_field(name=FIELD_NOTES, value=f"> {notes}", inline=False)

        embed.add_field(name=FIELD_VERSION, value=f"v{BOT_VERSION}", inline=False)

        embed.set_author(name="Prices Uncovered <>", icon_url=GUILD_ICON_URL)

        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if proof_pic and proof_pic.content_type and proof_pic.content_type.startswith("image/"):
            embed.set_image(url=proof_pic.url)
        elif banner_url:
            embed.set_image(url=banner_url)

        embed.set_footer(
            text=f"Monitored by Prices Uncovered - {INVITE_URL}",
            icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None,
        )

        target = target_channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send("❌ Please choose a valid text channel for the price update.", ephemeral=True)
            return

        msg = await target.send(embed=embed, view=PremiumPriceView())
        record_price(full_name, price, demand, trend, interaction.user.id, msg, "post")

        await interaction.followup.send(f"✅ Posted the price update in {target.mention}.", ephemeral=True)

    except Exception as e:
        log.exception("/postprice failed")
        try:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        except Exception:
            log.warning(f"Failed to respond to interaction: {e}")


MESSAGE_LINK_RE = re.compile(
    r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(?:\d+|@me)/(\d+)/(\d+)"
)


async def resolve_price_message(
    interaction: discord.Interaction,
    message_ref: str,
    channel: discord.TextChannel | None = None,
) -> discord.Message:
    """Resolve a message link or raw message ID to a discord.Message."""
    message_ref = message_ref.strip()

    match = MESSAGE_LINK_RE.match(message_ref)
    if match:
        channel_id, message_id = int(match.group(1)), int(match.group(2))
        target = bot.get_channel(channel_id)
        if target is None:
            try:
                target = await bot.fetch_channel(channel_id)
            except discord.NotFound:
                raise ValueError("That link points to a channel I can't find.")
            except discord.Forbidden:
                raise ValueError("Missing permission to access the channel in that link.")
    elif message_ref.isdigit():
        message_id = int(message_ref)
        target = channel or interaction.channel
    else:
        raise ValueError("Invalid `message_ref` — paste a full message link or a raw message ID.")

    if not isinstance(target, discord.TextChannel):
        raise ValueError("Couldn't resolve a valid text channel for that message.")

    try:
        return await target.fetch_message(message_id)
    except discord.NotFound:
        raise ValueError(f"No message with that ID found in {target.mention}.")
    except discord.Forbidden:
        raise ValueError(f"Missing permission to read messages in {target.mention}.")


def _find_field(embed: discord.Embed, name: str) -> int:
    for i, field in enumerate(embed.fields):
        if field.name == name:
            return i
    return -1


@bot.tree.command(name="editprice", description="Edit an already posted price update via message link/ID")
@app_commands.checks.has_role(PRICE_EDITOR_ROLE_ID)
@app_commands.choices(demand=DEMAND_CHOICES, trend=TREND_CHOICES)
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.describe(
    message_ref="Message link or message ID of the price update to edit",
    item="New item name (e.g. Hellfire Horns)",
    price="New price range (e.g. 67-72)",
    demand="New demand level",
    variant="New variant (Dark Red / Black)",
    item_emoji="New leading emoji",
    thumbnail_url="New item sprite URL",
    banner_url="New big banner image URL",
    proof_pic="New proof screenshot/snippet image",
    trend="New price trend",
    notes="New extra intel or patch note",
    channel="Channel of the message (only needed when using a raw message ID from another channel)",
)
async def editprice(
    interaction: discord.Interaction,
    message_ref: str,
    item: str | None = None,
    price: str | None = None,
    demand: str | None = None,
    variant: str | None = None,
    item_emoji: str | None = None,
    thumbnail_url: str | None = None,
    banner_url: str | None = None,
    proof_pic: discord.Attachment | None = None,
    trend: str | None = None,
    notes: str | None = None,
    channel: discord.TextChannel | None = None,
):
    await interaction.response.defer(ephemeral=True)

    try:
        if not any([item, price, demand, variant, item_emoji, thumbnail_url, banner_url, proof_pic, trend, notes]):
            await interaction.followup.send("❌ Nothing to edit — provide at least one option to change.", ephemeral=True)
            return

        try:
            msg = await resolve_price_message(interaction, message_ref, channel)
        except ValueError as ve:
            await interaction.followup.send(f"❌ {ve}", ephemeral=True)
            return

        if not bot.user or msg.author.id != bot.user.id:
            await interaction.followup.send("❌ That message wasn't sent by this bot, so it can't be edited.", ephemeral=True)
            return
        if not msg.embeds:
            await interaction.followup.send("❌ That message doesn't look like a price update (no embed found).", ephemeral=True)
            return

        embed = msg.embeds[0]

        # --- Title / wiki URL (item, variant, item_emoji) ---
        if item or variant is not None or item_emoji:
            old_emoji, old_full = parse_embed_title(embed.title or "")

            base_item = None
            if embed.url and "/wiki/" in embed.url:
                base_item = embed.url.rsplit("/wiki/", 1)[1].replace("_", " ")

            old_variant = ""
            if base_item and old_full.upper().startswith(base_item.upper()):
                old_variant = old_full[len(base_item):].strip()

            new_item = item or base_item or old_full
            new_variant = variant if variant is not None else old_variant
            new_emoji = item_emoji or old_emoji

            full_name = f"{new_item} {new_variant}".strip()
            embed.title = f"{new_emoji}  {full_name.upper()}  {new_emoji}"
            embed.url = f"https://growtopia.fandom.com/wiki/{new_item.replace(' ', '_')}"

        # --- Fields ---
        if price:
            i = _find_field(embed, FIELD_PRICE)
            value = f"```fix\n{price} 💎\n```"
            if i >= 0:
                embed.set_field_at(i, name=FIELD_PRICE, value=value, inline=False)
            else:
                embed.insert_field_at(0, name=FIELD_PRICE, value=value, inline=False)

        if demand:
            i = _find_field(embed, FIELD_DEMAND)
            value = demand_display(demand)
            if i >= 0:
                embed.set_field_at(i, name=FIELD_DEMAND, value=value, inline=True)
            else:
                embed.add_field(name=FIELD_DEMAND, value=value, inline=True)

            embed.color = demand_color(demand)

        if trend:
            i = _find_field(embed, FIELD_TREND)
            if i >= 0:
                embed.set_field_at(i, name=FIELD_TREND, value=trend, inline=True)
            else:
                embed.add_field(name=FIELD_TREND, value=trend, inline=True)

        if notes:
            i = _find_field(embed, FIELD_NOTES)
            value = f"> {notes}"
            if i >= 0:
                embed.set_field_at(i, name=FIELD_NOTES, value=value, inline=False)
            else:
                version_i = _find_field(embed, FIELD_VERSION)
                if version_i >= 0:
                    embed.insert_field_at(version_i, name=FIELD_NOTES, value=value, inline=False)
                else:
                    embed.add_field(name=FIELD_NOTES, value=value, inline=False)

        # --- Images ---
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        if proof_pic and proof_pic.content_type and proof_pic.content_type.startswith("image/"):
            embed.set_image(url=proof_pic.url)
        elif banner_url:
            embed.set_image(url=banner_url)

        # --- Edited marker in description ---
        embed.description = build_description(edited=True)

        await msg.edit(embed=embed)

        # Record the new state in price history.
        recorded_item = item_for_message(msg.id)
        if item or variant is not None:
            recorded_item = full_name  # set in the title branch above
        if not recorded_item:
            recorded_item = parse_embed_title(embed.title or "")[1].title()
        cur_price, cur_demand, cur_trend = extract_embed_price_data(embed)
        if cur_price:
            record_price(recorded_item, cur_price, cur_demand, cur_trend, interaction.user.id, msg, "edit")

        await interaction.followup.send(f"✅ Price update edited: {msg.jump_url}", ephemeral=True)

    except Exception as e:
        log.exception("/editprice failed")
        try:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
        except Exception:
            log.warning(f"Failed to respond to interaction: {e}")


@bot.tree.command(name="votes", description="Show vote results for a price update, or the most disputed prices")
@app_commands.checks.has_role(PRICE_EDITOR_ROLE_ID)
@app_commands.describe(
    message_ref="Message link or ID of a price update (leave empty for the most-disputed leaderboard)",
    channel="Channel of the message (only needed for a raw ID from another channel)",
)
async def votes(
    interaction: discord.Interaction,
    message_ref: str | None = None,
    channel: discord.TextChannel | None = None,
):
    await interaction.response.defer(ephemeral=True)

    if message_ref:
        try:
            msg = await resolve_price_message(interaction, message_ref, channel)
        except ValueError as ve:
            await interaction.followup.send(f"❌ {ve}", ephemeral=True)
            return
        acc, inacc = vote_counts(msg.id)
        item_name = item_for_message(msg.id) or (
            parse_embed_title(msg.embeds[0].title or "")[1] if msg.embeds else "Unknown Item"
        )
        total = acc + inacc
        pct = f"{acc / total:.0%}" if total else "—"
        embed = discord.Embed(
            title=f"📊 Votes - {item_name}",
            description=f"{msg.jump_url}",
            color=0x22d3ee,
            timestamp=utc_now(),
        )
        embed.add_field(name="✅ Accurate", value=acc, inline=True)
        embed.add_field(name="❌ Inaccurate", value=inacc, inline=True)
        embed.add_field(name="Accuracy", value=pct, inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # No message given → leaderboard of most-disputed prices.
    with db() as conn:
        rows = conn.execute(
            """SELECT message_id,
                      COALESCE(SUM(accurate), 0) AS acc,
                      COUNT(*) - COALESCE(SUM(accurate), 0) AS inacc
               FROM votes GROUP BY message_id
               HAVING inacc > 0 ORDER BY inacc DESC LIMIT 10"""
        ).fetchall()
    if not rows:
        await interaction.followup.send("No inaccurate votes recorded yet — nothing is disputed.", ephemeral=True)
        return

    lines = []
    for r in rows:
        item_name = item_for_message(int(r["message_id"])) or f"Message {r['message_id']}"
        lines.append(f"**{item_name}** — ❌ {r['inacc']} / ✅ {r['acc']}")
    embed = discord.Embed(
        title="📊 Most disputed prices",
        description="\n".join(lines),
        color=0xef4444,
        timestamp=utc_now(),
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="deleteprice", description="Delete a posted price update and its vote data")
@app_commands.checks.has_role(PRICE_EDITOR_ROLE_ID)
@app_commands.describe(
    message_ref="Message link or message ID of the price update to delete",
    channel="Channel of the message (only needed for a raw ID from another channel)",
)
async def deleteprice(
    interaction: discord.Interaction,
    message_ref: str,
    channel: discord.TextChannel | None = None,
):
    await interaction.response.defer(ephemeral=True)

    try:
        msg = await resolve_price_message(interaction, message_ref, channel)
    except ValueError as ve:
        await interaction.followup.send(f"❌ {ve}", ephemeral=True)
        return

    if not bot.user or msg.author.id != bot.user.id:
        await interaction.followup.send("❌ That message wasn't sent by this bot.", ephemeral=True)
        return

    item_name = item_for_message(msg.id) or (
        parse_embed_title(msg.embeds[0].title or "")[1] if msg.embeds else "Unknown Item"
    )
    try:
        await msg.delete()
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing permission to delete that message.", ephemeral=True)
        return

    with db() as conn:
        conn.execute("DELETE FROM votes WHERE message_id = ?", (str(msg.id),))

    log.info(f"Price update deleted: {item_name} (message {msg.id}) by {interaction.user} ({interaction.user.id})")
    await interaction.followup.send(f"🗑️ Deleted the price update for **{item_name}** and its votes.", ephemeral=True)


@bot.tree.command(name="checkprice", description="Look up the latest posted price for an item")
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.describe(item="Item name")
async def checkprice(interaction: discord.Interaction, item: str):
    await interaction.response.defer(ephemeral=True)

    row = latest_price(item)
    if row is None:
        await interaction.followup.send(f"❌ No price on record for **{item}**.", ephemeral=True)
        return

    ts = int(datetime.datetime.fromisoformat(row["created_at"]).timestamp())
    embed = discord.Embed(
        title=f"🔎 {row['item']}",
        description=f"Last updated <t:{ts}:R> (<t:{ts}:f>)",
        color=demand_color(row["demand"] or ""),
        timestamp=utc_now(),
    )
    embed.add_field(name=FIELD_PRICE, value=f"```fix\n{row['price']} 💎\n```", inline=False)
    if row["demand"]:
        embed.add_field(name=FIELD_DEMAND, value=row["demand"], inline=True)
    if row["trend"]:
        embed.add_field(name=FIELD_TREND, value=row["trend"], inline=True)
    if row["poster_id"]:
        embed.add_field(name=FIELD_POSTED_BY, value=f"<@{row['poster_id']}>", inline=True)
    if row["channel_id"] and row["message_id"] and interaction.guild:
        embed.add_field(
            name="🔗 Source",
            value=f"https://discord.com/channels/{interaction.guild.id}/{row['channel_id']}/{row['message_id']}",
            inline=False,
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="pricehistory", description="Show how an item's price has moved over time")
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.describe(item="Item name")
async def pricehistory(interaction: discord.Interaction, item: str):
    await interaction.response.defer(ephemeral=True)

    rows = price_history(item)
    if not rows:
        await interaction.followup.send(f"❌ No price history for **{item}**.", ephemeral=True)
        return

    lines = []
    for r in rows:
        ts = int(datetime.datetime.fromisoformat(r["created_at"]).timestamp())
        marker = "📝" if r["action"] == "edit" else "📦"
        lines.append(f"{marker} <t:{ts}:d> — **{r['price']} 💎** • {r['trend'] or '—'} • by <@{r['poster_id']}>")
    embed = discord.Embed(
        title=f"📈 Price history - {rows[0]['item']}",
        description="\n".join(lines),
        color=0x67e8f9,
        timestamp=utc_now(),
    )
    embed.set_footer(text=f"Last {len(rows)} entries • 📦 post / 📝 edit")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tasks.loop(hours=24)
async def stale_price_sweep():
    """Daily report of items whose latest price is older than STALE_PRICE_DAYS."""
    cutoff = (utc_now() - datetime.timedelta(days=STALE_PRICE_DAYS)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """SELECT item, MAX(created_at) AS last_update FROM prices
               GROUP BY item_key HAVING last_update < ?
               ORDER BY last_update LIMIT 20""",
            (cutoff,),
        ).fetchall()
    if not rows:
        return

    channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
    if channel is None:
        return
    lines = []
    for r in rows:
        ts = int(datetime.datetime.fromisoformat(r["last_update"]).timestamp())
        lines.append(f"• **{r['item']}** — last update <t:{ts}:R>")
    embed = discord.Embed(
        title=f"🕰️ Stale prices (older than {STALE_PRICE_DAYS} days)",
        description="\n".join(lines),
        color=0xfbbf24,
        timestamp=utc_now(),
    )
    embed.set_footer(text="These items need a fresh price check")
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        log.warning(f"Couldn't send stale price report: {e}")


@stale_price_sweep.before_loop
async def _wait_for_bot():
    await bot.wait_until_ready()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set.")
    # log_handler=None: discord.py logs through the root logger i just configured,
    # which also feeds the DiscordLogHandler added in setup_hook.
    bot.run(token, log_handler=None)