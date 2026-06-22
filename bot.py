"""
Discord Finance News Bot — main entry point.
Polls Finnhub for financial news and pushes formatted updates to a Discord channel.
"""

import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timezone

import discord
from discord.ext import tasks
from dotenv import load_dotenv

from formatter import build_discord_embed, detect_sentiment, extract_tickers
from scanner import NewsScanner

load_dotenv()

# ─── Config ─────────────────────────────────────────────────────────

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY", "")
CHANNEL_ID_STR = os.getenv("DISCORD_CHANNEL_ID", "")
CHANNEL_ID = int(CHANNEL_ID_STR) if CHANNEL_ID_STR.strip() else 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("finance-bot")

# ─── Init ───────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

scanner = NewsScanner(api_key=FINNHUB_KEY)

# Daily summary accumulator
_daily_news: list[dict] = []

# Cached last fetch for filter commands
_last_fetched_items: list[dict] = []

# ─── Keyword hotness tracking ──────────────────────────────────────
_hotness_counter: Counter = Counter()
_recent_headlines: list[str] = []


# ─── Helpers ────────────────────────────────────────────────────────

def _is_config_valid() -> bool:
    """Validate and log config. Does NOT exit — returns False if broken."""
    logger.info(f"ENV check: TOKEN={'SET' if DISCORD_TOKEN and DISCORD_TOKEN != 'your_bot_token_here' else 'MISSING'}")
    logger.info(f"ENV check: FINNHUB={'SET' if FINNHUB_KEY and FINNHUB_KEY != 'your_finnhub_key_here' else 'MISSING'}")
    logger.info(f"ENV check: CHANNEL_ID={CHANNEL_ID_STR}")
    if DISCORD_TOKEN in ("", "your_bot_token_here"):
        logger.error("DISCORD_BOT_TOKEN not set")
        return False
    if FINNHUB_KEY in ("", "your_finnhub_key_here"):
        logger.error("FINNHUB_API_KEY not set")
        return False
    if CHANNEL_ID == 0:
        logger.error("DISCORD_CHANNEL_ID not set or zero")
        return False
    return True


def _track_hotness(items: list[dict]):
    """Update keyword hotness from news items."""
    for item in items:
        text = f"{item.get('headline', '')} {item.get('summary', '')}"
        _recent_headlines.append(text)
        tickers = extract_tickers(text)
        for t in tickers:
            _hotness_counter[t] += 1
    # Keep only last 200 headlines for windowed stats
    if len(_recent_headlines) > 200:
        _recent_headlines[:] = _recent_headlines[-200:]


def _get_hot_keywords(top_n: int = 15) -> list[tuple[str, int]]:
    """Return top N hot keywords (tickers) with counts."""
    return _hotness_counter.most_common(top_n)


HELP_TEXT = (
    "**Finance News Bot — 指令**\n"
    "`/news TICKER` — 查询特定股票的最新新闻（如 `/news AAPL`）\n"
    "`!now` — 立即拉取一轮最新新闻\n"
    "`!green` / `!bull` — 只看 🟢 利好新闻\n"
    "`!red` / `!bear` — 只看 🔴 利空新闻\n"
    "`!all` — 显示全部新闻（重置过滤器）\n"
    "`!hot` — 🔥 当前热词统计 Top 15\n"
    "`/help` 或 @我 — 显示此帮助\n"
    "\n新闻自动推送中，每天美东 16:00 发送每日总结。"
)


# ─── Interactive Filter View (Buttons) ─────────────────────────────

class NewsFilterView(discord.ui.View):
    """Interactive buttons to filter news by sentiment / show hot keywords."""

    def __init__(self, items: list[dict], timeout: float = 300):
        super().__init__(timeout=timeout)
        self.items = items

    @discord.ui.button(label="🟢 利好", style=discord.ButtonStyle.success, custom_id="filter_green")
    async def filter_green(self, interaction: discord.Interaction, button: discord.ui.Button):
        filtered = [it for it in self.items if detect_sentiment(
            f"{it.get('headline', '')} {it.get('summary', '')}"
        ) == "🟢"]
        await interaction.response.defer()
        await _send_filtered(interaction.channel, filtered, "🟢 利好新闻", delete_after=60)

    @discord.ui.button(label="🔴 利空", style=discord.ButtonStyle.danger, custom_id="filter_red")
    async def filter_red(self, interaction: discord.Interaction, button: discord.ui.Button):
        filtered = [it for it in self.items if detect_sentiment(
            f"{it.get('headline', '')} {it.get('summary', '')}"
        ) == "🔴"]
        await interaction.response.defer()
        await _send_filtered(interaction.channel, filtered, "🔴 利空新闻", delete_after=60)

    @discord.ui.button(label="🟡 中性", style=discord.ButtonStyle.secondary, custom_id="filter_neutral")
    async def filter_neutral(self, interaction: discord.Interaction, button: discord.ui.Button):
        filtered = [it for it in self.items if detect_sentiment(
            f"{it.get('headline', '')} {it.get('summary', '')}"
        ) == "🟡"]
        await interaction.response.defer()
        await _send_filtered(interaction.channel, filtered, "🟡 中性新闻", delete_after=60)

    @discord.ui.button(label="🔥 热词", style=discord.ButtonStyle.primary, custom_id="filter_hot")
    async def show_hot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        hot = _get_hot_keywords(15)
        if not hot:
            await interaction.channel.send("暂无热词数据，先发 `!now` 拉取新闻。", delete_after=30)
            return
        lines = ["**🔥 当前热词 Top 15**", ""]
        for i, (word, count) in enumerate(hot, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            lines.append(f"{emoji} **${word}** — {count} 次提及")
        await interaction.channel.send("\n".join(lines))

    @discord.ui.button(label="📋 全部", style=discord.ButtonStyle.secondary, custom_id="filter_all")
    async def show_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _send_filtered(interaction.channel, self.items, "📋 全部新闻", delete_after=60)


async def _send_filtered(channel, items: list[dict], label: str, delete_after: int = 60):
    """Send filtered news items to channel."""
    if not items:
        msg = await channel.send(f"{label}：暂无匹配新闻。", delete_after=delete_after)
        return
    await channel.send(f"**{label}** — 共 {len(items)} 条：", delete_after=delete_after)
    for item in items:
        try:
            embed = build_discord_embed(item)
            await channel.send(embed=embed, delete_after=delete_after)
        except Exception as e:
            logger.error(f"Failed to send filtered embed: {e}")
        await asyncio.sleep(0.3)


async def _send_news_batch(channel, items: list[dict]):
    """Send news batch with interactive filter buttons."""
    global _last_fetched_items
    _last_fetched_items = items

    _track_hotness(items)

    for item in items:
        try:
            embed = build_discord_embed(item)
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send embed: {e}")
        await asyncio.sleep(0.3)

    # Send filter panel with buttons
    view = NewsFilterView(items, timeout=300)
    await channel.send(
        f"✅ 已推送 {len(items)} 条新闻 | 点击下方按钮筛选：",
        view=view,
        delete_after=300,
    )


# ─── Events ─────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not _is_config_valid():
        logger.critical("Invalid config — check .env. Shutting down.")
        await bot.close()
        return

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        logger.critical(f"Channel {CHANNEL_ID} not found — bot may lack permissions.")
        await bot.close()
        return

    logger.info(f"Target channel: #{channel.name} ({channel.id})")

    # Cold start — fetch last 10 items without flooding
    logger.info("Cold start: fetching recent news...")
    initial_items = await scanner.cold_start_fetch()
    for item in reversed(initial_items):
        try:
            embed = build_discord_embed(item)
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send cold-start embed: {e}")
    logger.info(f"Sent {len(initial_items)} cold-start news items")

    # Start background task
    news_poll_loop.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    content = message.content.strip()
    channel = message.channel

    # ── @mention: reply with help ──────────────────────────────
    if bot.user in message.mentions:
        await channel.send(HELP_TEXT)
        return

    # ── /news TICKER ───────────────────────────────────────────
    if content.startswith("/news"):
        parts = content.split()
        if len(parts) < 2:
            await channel.send("用法: `/news TICKER`  例如 `/news AAPL`")
            return

        ticker = parts[1].upper().strip("$")
        await channel.send(f"🔍 正在查询 ${ticker} 相关新闻...")
        items = await scanner.fetch_latest()
        ticker_items = []
        for item in items:
            text = f"{item.get('headline', '')} {item.get('summary', '')}"
            if ticker.upper() in text.upper():
                ticker_items.append(item)

        if not ticker_items:
            await channel.send(f"未找到 ${ticker} 的相关新闻。")
            return

        for item in ticker_items[:5]:
            try:
                embed = build_discord_embed(item)
                await channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to send ticker embed: {e}")
        return

    # ── !now ──────────────────────────────────────────────────
    if content == "!now":
        await channel.send("⏳ 正在拉取最新新闻...")
        try:
            items = await scanner.fetch_all_now()
        except Exception as e:
            await channel.send(f"拉取失败: {e}")
            return
        if not items:
            await channel.send("暂无新新闻。")
            return
        await _send_news_batch(channel, items)
        return

    # ── !green / !bull — bullish only ─────────────────────────
    if content in ("!green", "!bull"):
        items = _last_fetched_items if _last_fetched_items else await scanner.fetch_all_now()
        filtered = [it for it in items if detect_sentiment(
            f"{it.get('headline', '')} {it.get('summary', '')}"
        ) == "🟢"]
        await _send_filtered(channel, filtered, "🟢 利好新闻")
        return

    # ── !red / !bear — bearish only ───────────────────────────
    if content in ("!red", "!bear"):
        items = _last_fetched_items if _last_fetched_items else await scanner.fetch_all_now()
        filtered = [it for it in items if detect_sentiment(
            f"{it.get('headline', '')} {it.get('summary', '')}"
        ) == "🔴"]
        await _send_filtered(channel, filtered, "🔴 利空新闻")
        return

    # ── !all — reset filter, show all ─────────────────────────
    if content == "!all":
        items = _last_fetched_items if _last_fetched_items else await scanner.fetch_all_now()
        await _send_filtered(channel, items, "📋 全部新闻")
        return

    # ── !hot — trending keywords ──────────────────────────────
    if content == "!hot":
        hot = _get_hot_keywords(15)
        if not hot:
            await channel.send("暂无热词数据，先发 `!now` 拉取新闻。")
            return
        lines = ["**🔥 当前热词 Top 15**", ""]
        for i, (word, count) in enumerate(hot, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            lines.append(f"{emoji} **${word}** — {count} 次提及")
        await channel.send("\n".join(lines))
        return

    # ── /help ─────────────────────────────────────────────────
    if content == "/help":
        await channel.send(HELP_TEXT)
        return


# ─── Background Tasks ───────────────────────────────────────────────

@tasks.loop(seconds=60)
async def news_poll_loop():
    """Poll Finnhub every 60s, push new items to Discord."""
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        return

    try:
        items = await scanner.fetch_latest()
    except Exception as e:
        logger.error(f"Poll error: {e}")
        return

    if not items:
        return

    _track_hotness(items)

    for item in items:
        try:
            embed = build_discord_embed(item)
            await channel.send(embed=embed)
            _daily_news.append(item)
        except Exception as e:
            logger.error(f"Failed to send embed: {e}")
        await asyncio.sleep(0.3)


@tasks.loop(hours=24)
async def daily_summary_task():
    """Send a pinned daily summary at 16:00 ET (21:00 UTC)."""
    now = datetime.now(timezone.utc)
    if now.hour != 21:
        return

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None or not _daily_news:
        return

    ticker_count: dict[str, int] = {}
    green = red = neutral = 0
    for item in _daily_news:
        text = f"{item.get('headline', '')} {item.get('summary', '')}"
        sentiment = detect_sentiment(text)
        if sentiment == "🟢":
            green += 1
        elif sentiment == "🔴":
            red += 1
        else:
            neutral += 1
        for t in extract_tickers(text):
            ticker_count[t] = ticker_count.get(t, 0) + 1

    summary_lines = [
        f"**📋 每日金融新闻总结 — {now.strftime('%Y-%m-%d')}**",
        f"今日共推送 **{len(_daily_news)}** 条新闻",
        f"🟢 {green} 条利好  |  🔴 {red} 条利空  |  🟡 {neutral} 条中性",
        "",
    ]

    if ticker_count:
        summary_lines.append("**🔥 今日最受关注标的：**")
        sorted_tickers = sorted(ticker_count.items(), key=lambda x: x[1], reverse=True)[:10]
        for ticker, count in sorted_tickers:
            summary_lines.append(f"• ${ticker} — {count} 条相关新闻")
    else:
        summary_lines.append("_今日无明显热点标的_")

    summary_msg = await channel.send("\n".join(summary_lines))
    try:
        await summary_msg.pin()
    except Exception as e:
        logger.warning(f"Failed to pin summary: {e}")

    _daily_news.clear()


@daily_summary_task.before_loop
async def _before_daily_summary():
    await bot.wait_until_ready()
    now = datetime.now(timezone.utc)
    target = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if target <= now:
        from datetime import timedelta
        target += timedelta(days=1)
    wait_seconds = (target - now).total_seconds()
    logger.info(f"Daily summary will first run in {wait_seconds:.0f}s (at {target.isoformat()})")
    await asyncio.sleep(wait_seconds)


# ─── Main ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _is_config_valid():
        logger.critical("Missing credentials in .env. Exiting.")
        exit(1)
    bot.run(DISCORD_TOKEN)

