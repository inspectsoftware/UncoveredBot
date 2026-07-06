<div align="center">

# 💎 Prices Uncovered - Live Price Bot

**A Discord bot for tracking Growtopia item prices with community-driven accuracy voting, full price history, and automatic dispute detection.**

![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)
![discord.py](https://img.shields.io/badge/discord.py-2.3+-5865F2?logo=discord&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![Version](https://img.shields.io/badge/version-1.0.2-c084fc)

</div>

---

## ✨ What it does

Price editors post rich, styled price embeds for in-game items. The community votes on each price with persistent **✅ Accurate / ❌ Inaccurate** buttons. Every post, edit, and vote is recorded in SQLite, giving you searchable price history, dispute alerts, and stale-price reports out of the box.

<table>
<tr>
<td>

**📍 Live price embeds**
Wiki-linked titles, demand-colored accents, price trends, proof screenshots, and Discord-native timestamps that render in each viewer's local timezone.

</td>
<td>

**🗳️ One vote per user**
Vote state is keyed by message ID in SQLite, so votes survive restarts and nobody can double-vote. Button labels update with live counts.

</td>
</tr>
<tr>
<td>

**⚠️ Automatic dispute detection**
When a price collects enough inaccurate votes (configurable threshold + ratio), it's flagged **Disputed - needs a re-check** on the embed and the price editor role gets pinged.

</td>
<td>

**🕰️ Stale price sweeper**
A daily background task reports items whose latest price is older than a configurable number of days, so nothing rots quietly.

</td>
</tr>
</table>

---

## 🧭 Commands

### Everyone

| Command | Description |
|---|---|
| `/checkprice <item>` | Look up the latest recorded price for an item (with autocomplete + fuzzy matching) |
| `/pricehistory <item>` | Timeline of an item's price movements - posts 📦 and edits 📝 |

### Price editors (role-gated)

| Command | Description |
|---|---|
| `/postprice` | Post a full price dashboard embed - item, price, demand, trend, variant, emoji, thumbnail, banner, proof pic, notes, target channel |
| `/editprice <message>` | Edit any field of an already-posted price via message link or ID; marks it *(edited)* and records the change in history |
| `/deleteprice <message>` | Delete a price post and wipe its vote data |
| `/votes [message]` | Vote breakdown for one price - or, with no argument, a leaderboard of the most disputed prices |
| `/version` | Show the running bot version |

### Admin / owner

| Command | Description |
|---|---|
| `/forceupdate` | Force-sync slash commands (requires *Manage Server*) |
| `/restart` | Restart the bot process (bot owner / team members only) |

---

## 🏗️ Architecture highlights

```mermaid
flowchart LR
    A["/postprice"] -->|embed + buttons| B[Price message]
    B -->|votes| C[(pricebot.db)]
    A -->|history| C
    C -->|threshold hit| D[⚠️ Disputed flag + role ping]
    C -->|daily sweep| E[🕰️ Stale price report]
    F[DiscordLogHandler] -->|queued embeds| G[#bot-log channel]
```

- **SQLite storage** : two tables (`votes`, `prices`) with a context-managed connection (commit-on-success, guaranteed close). A one-time migration imports the legacy `votes.json` on first boot.
- **Persistent views** : vote buttons use fixed `custom_id`s and are re-registered on startup, so they keep working across restarts.
- **Discord-native logging** : a custom `logging.Handler` ships INFO+ records to a log channel as color-coded embeds via a non-blocking queue, with loop-protection against discord.py's own chatter.
- **Interaction audit trail** : every slash command and button press is logged with the user, options, and channel.
- **3-second rule respected** : button clicks are acknowledged immediately; DB writes, message edits, and log sends happen after the ack so the interaction token never expires mid-vote.

---

## 🚀 Setup

### 1. Requirements

- Python **3.10+** (uses `X | None` union syntax)
- A Discord bot application with the **Message Content** intent enabled

### 2. Install

```bash
git clone <this-repo>
cd price-bot
pip install -r requirements.txt
```

### 3. Configure

Set your environment variables (only `DISCORD_TOKEN` is strictly required):

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | - *(required)* | Bot token |
| `PRICE_EDITOR_ROLE_ID` | built-in ID | Role allowed to post/edit/delete prices |
| `LOG_CHANNEL_ID` | built-in ID | Channel for vote logs |
| `BOT_LOG_CHANNEL_ID` | built-in ID | Channel for interaction audit + info/warning/error embeds |
| `GUILD_ICON_URL` | built-in URL | Author icon on price embeds |
| `INVITE_URL` | built-in URL | Server invite shown in embed footers |
| `DISPUTED_MIN_VOTES` | `5` | Minimum inaccurate votes before a price can be flagged |
| `DISPUTED_RATIO` | `0.6` | Fraction of votes that must be inaccurate to flag |
| `STALE_PRICE_DAYS` | `14` | Age threshold for the daily stale-price report |

### 4. Run

```bash
python bot.py
```

On first boot the bot creates `pricebot.db`, migrates any legacy `votes.json`, re-registers persistent vote buttons, and syncs slash commands.

---

## 🗃️ Data model

| Table | Contents |
|---|---|
| `votes` | One row per `(message_id, user_id)` - enforces one vote per user per price |
| `prices` | Append-only history: every `post` and `edit` with item, price, demand, trend, poster, and source message |

`prices.item_key` (lowercased) is indexed for fast autocomplete and fuzzy lookups.

---

<div align="center">

*Monitored by **Prices Uncovered** 💎*

</div>
