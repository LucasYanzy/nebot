"""
Finnhub news scanner — polls API, deduplicates, yields new items.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"


class NewsScanner:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._seen_ids: set[int] = set()
        self._last_id: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._reset_time_utc = 0  # daily reset marker

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_latest(self) -> list[dict]:
        """Fetch recent news from Finnhub. Returns only new (unseen) items."""
        await self._ensure_session()
        self._daily_reset_if_needed()

        url = f"{FINNHUB_BASE}/news?category=general&token={self.api_key}"
        if self._last_id:
            url += f"&minId={self._last_id}"

        try:
            async with self._session.get(url, timeout=15) as resp:
                if resp.status == 429:
                    logger.warning("Finnhub rate limit hit, backing off 5s")
                    await asyncio.sleep(5)
                    return []
                resp.raise_for_status()
                items: list[dict] = await resp.json()
        except Exception as e:
            logger.error(f"Finnhub fetch failed: {e}")
            return []

        if not items:
            return []

        # Track highest ID for next incremental fetch
        item_ids = [it["id"] for it in items if "id" in it]
        if item_ids:
            self._last_id = max(item_ids)

        new_items = [it for it in items if it.get("id") not in self._seen_ids]
        for it in new_items:
            self._seen_ids.add(it["id"])

        return new_items

    async def cold_start_fetch(self) -> list[dict]:
        """Async initial fetch — returns last 10 new items, no flood."""
        await self._ensure_session()
        self._daily_reset_if_needed()
        url = f"{FINNHUB_BASE}/news?category=general&token={self.api_key}"
        try:
            async with self._session.get(url, timeout=15) as resp:
                resp.raise_for_status()
                items: list[dict] = await resp.json()
        except Exception as e:
            logger.error(f"Cold start fetch failed: {e}")
            return []

        if not items:
            return []

        item_ids = [it["id"] for it in items if "id" in it]
        if item_ids:
            self._last_id = max(item_ids)

        new_items = [it for it in items if it.get("id") not in self._seen_ids]
        for it in new_items:
            self._seen_ids.add(it["id"])
        return new_items[-10:]  # last 10 to avoid flood

    def _daily_reset_if_needed(self):
        now = datetime.now(timezone.utc)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if self._reset_time_utc < today_midnight:
            self._seen_ids.clear()
            self._last_id = None
            self._reset_time_utc = today_midnight
