"""
Discord Finance News Bot — main entry point.
Polls Finnhub for financial news and pushes formatted updates to a Discord channel.
"""

import asyncio
import logging
import os
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

    # Command: /news <ticker> — on-demand stock news
    if message.content.startswith("/news"):
        parts = message.content.split()
        if len(parts) < 2:
            await message.channel.send("用法: `/news TICKER`  例如 `/news AAPL`")
            return

        ticker = parts[1].upper().strip("$")
        await message.channel.send(f"🔍 正在查询 ${ticker} 相关新闻...")
        # Use scanner to fetch (Finnhub /news endpoint covers all)
        items = await scanner.fetch_latest()
        ticker_items = []
        for item in items:
            text = f"{item.get('headline', '')} {item.get('summary', '')}"
            if ticker.upper() in text.upper():
                ticker_items.append(item)

        if not ticker_items:
            await message.channel.send(f"未找到 ${ticker} 的相关新闻。")
            return

        for item in ticker_items[:5]:  # max 5
            try:
                embed = build_discord_embed(item)
                await message.channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to send ticker embed: {e}")

    # Command: !now — force one poll cycle
    elif message.content.strip() == "!now":
        await message.channel.send("⏳ 正在拉取最新新闻...")
        try:
            items = await scanner.fetch_latest()
        except Exception as e:
            await message.channel.send(f"拉取失败: {e}")
            return
        if not items:
            await message.channel.send("暂无新新闻。")
            return
        for item in items:
            try:
                embed = build_discord_embed(item)
                await message.channel.send(embed=embed)
            except Exception as e:
                logger.error(f"Failed to send embed: {e}")
            await asyncio.sleep(0.3)
        await message.channel.send(f"✅ 已推送 {len(items)} 条新闻。")

    # Command: /help
    elif message.content.strip() == "/help":
        help_text = (
            "**Finance News Bot — 指令**\n"
            "`/news TICKER` — 查询特定股票的最新新闻（如 `/news AAPL`）\n"
            "`!now` — 立即拉取一轮最新新闻\n"
            "`/help` — 显示此帮助\n"
            "\n新闻自动推送中，每天美东 16:00 发送每日总结。"
        )
        await message.channel.send(help_text)


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

    for item in items:
        try:
            embed = build_discord_embed(item)
            await channel.send(embed=embed)
            _daily_news.append(item)
        except Exception as e:
            logger.error(f"Failed to send embed: {e}")

        # Rate-limit Discord sends (5/s for non-verified bots)
        await asyncio.sleep(0.3)


@tasks.loop(hours=24)
async def daily_summary_task():
    """Send a pinned daily summary at 16:00 ET (21:00 UTC)."""
    # This task runs every 24h; we check if current hour is ~21 UTC
    now = datetime.now(timezone.utc)
    if now.hour != 21:
        return  # only run at ~21 UTC

    channel = bot.get_channel(CHANNEL_ID)
    if channel is None or not _daily_news:
        return

    ticker_count: dict[str, int] = {}
    for item in _daily_news:
        text = f"{item.get('headline', '')} {item.get('summary', '')}"
        sentiment = detect_sentiment(text)
        for t in extract_tickers(text):
            ticker_count[t] = ticker_count.get(t, 0) + 1

    summary_lines = [
        f"**📋 每日金融新闻总结 — {now.strftime('%Y-%m-%d')}**",
        f"今日共推送 **{len(_daily_news)}** 条新闻",
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
    # Align to next 21:00 UTC
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
