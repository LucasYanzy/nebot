"""
News formatter — sentiment tagging, ticker extraction, Discord Embed construction.
"""

from __future__ import annotations

import re
from datetime import datetime

import discord

# ─── Sentiment keywords ────────────────────────────────────────────

BULLISH_WORDS = [
    "beat", "beats", "raised", "raises", "upgraded", "surged", "surges",
    "record", "positive", "growth", "rallied", "boosted", "outperform",
    "strong", "jumps", "soars", "climbs", "gains", "breakthrough",
    "approval", "approved", "partnership", "expansion",
]

BEARISH_WORDS = [
    "miss", "misses", "plunge", "plunges", "downgraded", "crashed",
    "layoff", "layoffs", "probe", "lawsuit", "negative", "tumbled",
    "sank", "drops", "declines", "falls", "weak", "warning", "cut",
    "investigation", "fine", "penalty", "recall", "debt", "bankruptcy",
]

# ─── Ticker extraction ─────────────────────────────────────────────

# Common stock symbols to filter noise (top 200 US stocks + major ETFs)
KNOWN_TICKERS = frozenset({
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "NVDA", "BRK.A", "BRK.B",
    "JPM", "JNJ", "V", "PG", "UNH", "HD", "BAC", "MA", "DIS", "ADBE",
    "CRM", "NFLX", "INTC", "CMCSA", "PEP", "TMO", "ABT", "CSCO", "VZ", "XOM",
    "CVX", "WMT", "COST", "ABBV", "MRK", "AVGO", "TXN", "QCOM", "AMD", "PYPL",
    "NKE", "LLY", "MDT", "HON", "BMY", "ORCL", "IBM", "UPS", "PM", "RTX",
    "AMGN", "CAT", "GE", "DE", "BA", "MMM", "GS", "MS", "C", "BLK",
    "SCHW", "AXP", "SPGI", "PLTR", "UBER", "SQ", "SNAP", "RBLX", "COIN", "DKNG",
    "SNOW", "DDOG", "NET", "ZS", "CRWD", "OKTA", "MDB", "TEAM", "SHOP", "ZM",
    "DOCU", "TWLO", "ROKU", "PINS", "U", "PATH", "GTLB", "HCP", "IONQ", "RIVN",
    "LCID", "AFRM", "SOFI", "HOOD", "DNA", "QS", "MVST", "ASTS", "ACHR", "JOBY",
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "ARKK", "XLF", "XLE", "XLK",
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "DOT",
    "GLD", "SLV", "USO", "UNG", "TLT", "HYG", "LQD", "EEM",
})


def extract_tickers(text: str) -> set[str]:
    """Extract stock/crypto tickers from text."""
    found = set()
    # Match $AAPL style references
    dollar_tickers = re.findall(r"\$([A-Z]{1,5})", text)
    found.update(t for t in dollar_tickers if t in KNOWN_TICKERS)

    # Match standalone known tickers (with word boundaries, avoiding false positives)
    for ticker in KNOWN_TICKERS:
        if re.search(rf"\b{re.escape(ticker)}\b", text):
            found.add(ticker)

    return found


def detect_sentiment(text: str) -> str:
    """Return 🟢 / 🔴 / 🟡 based on keyword matching."""
    text_lower = text.lower()
    bull_count = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear_count = sum(1 for w in BEARISH_WORDS if w in text_lower)

    if bull_count > bear_count:
        return "🟢"
    elif bear_count > bull_count:
        return "🔴"
    return "🟡"


def extract_data_highlight(text: str) -> str | None:
    """Extract quantitative highlights like 'revenue +12%' or 'EPS $2.10 beat'."""
    patterns = [
        r"(?:revenue|sales|income|profit)\s*(?:of\s*)?\$?[\d,.]+\s*(?:billion|million|B|M)?\s*(?:beat|miss|up|down)\s*(?:by\s*)?\d+%?",
        r"(?:EPS|earnings per share)\s*(?:of\s*)?\$?[\d.]+\s*(?:beat|miss)",
        r"(?:raised|cut|lowered)\s+(?:guidance|outlook|forecast)",
        r"(?:surged|plunged|jumped|tumbled|rallied|dropped)\s*\d+%",
        r"\d+%\s*(?:surge|plunge|jump|drop|rally|gain|loss|increase|decrease)",
        r"(?:market\s*cap|valuation)\s*(?:exceeds|tops|falls\s*below)\s*\$[\d,.]+",
        r"(?:buyback|dividend)\s*(?:of\s*)?\$[\d,.]+",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def extract_market_reaction(text: str) -> str | None:
    """Extract market reaction like 'after-hours +2.3%'."""
    patterns = [
        r"(?:after[- ]hours|pre[- ]market|extended\s*trading)[^.]*?[+-]?\d+\.?\d*%",
        r"(?:stock|shares)\s+(?:rose|fell|gained|lost|dropped|climbed|declined)\s*\d+\.?\d*%",
        r"(?:up|down)\s+\d+\.?\d*%\s*(?:in\s*)?(?:pre[- ]market|after[- ]hours)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None


def build_discord_embed(item: dict) -> discord.Embed:
    """Build a formatted Discord Embed from a Finnhub news item."""
    title = item.get("headline", "Financial News Update")
    summary = item.get("summary", "") or ""
    source_url = item.get("url", "")
    source_name = item.get("source", "Finnhub")
    published_at = item.get("datetime")
    full_text = f"{title} {summary}"

    sentiment = detect_sentiment(full_text)
    tickers = extract_tickers(full_text)
    data_highlight = extract_data_highlight(full_text)
    market_reaction = extract_market_reaction(full_text)

    # Color based on sentiment
    color_map = {"🟢": 0x00C853, "🔴": 0xFF1744, "🟡": 0xFFAB00}
    embed = discord.Embed(
        title=title,
        url=source_url,
        description=summary[:2048] if summary else None,
        color=color_map.get(sentiment, 0x5865F2),
    )

    embed.set_author(name=f"{source_name} | Financial News")

    if published_at:
        try:
            dt = datetime.fromtimestamp(published_at)
            embed.set_footer(text=f"{sentiment}  •  {dt.strftime('%Y-%m-%d %H:%M UTC')}")
        except (ValueError, TypeError):
            embed.set_footer(text=f"{sentiment}")

    # Fields: data highlight, market reaction, affected tickers
    if data_highlight:
        embed.add_field(name="📊 数据亮点", value=data_highlight, inline=False)
    if market_reaction:
        embed.add_field(name="📈 市场反应", value=market_reaction, inline=False)
    if tickers:
        ticker_str = " ".join(f"${t}" for t in sorted(tickers))
        embed.add_field(name="🎯 影响标的", value=ticker_str[:1024], inline=False)

    return embed
