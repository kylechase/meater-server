"""Push alerts to an ntfy topic when a cook needs attention.

This runs on the server, so the alert goes out even when no browser has the UI
open.  Messages are published as JSON to the server root, which keeps titles
like "Ribeye, medium-rare" intact; ntfy's header form only carries latin-1.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

#: ntfy priorities: 5 is "urgent", which breaks through Do Not Disturb.
PRIORITY_URGENT = 5
PRIORITY_HIGH = 4
_TIMEOUT = aiohttp.ClientTimeout(total=10)


def split_topic_url(url: str) -> tuple[str, str]:
    """``https://host/path/topic`` -> (``https://host/path``, ``topic``).

    A bare ``host/topic`` is treated as https.
    """
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    base_path, _, topic = path.rpartition("/")
    if not topic:
        raise ValueError(f"ntfy URL has no topic: {url}")
    return urlunsplit((parts.scheme, parts.netloc, base_path, "", "")), topic


class NtfyNotifier:
    """Fire-and-forget publisher.  A failed push is logged, never raised."""

    def __init__(self, url: str, token: str | None = None) -> None:
        self.base, self.topic = split_topic_url(url)
        self.token = token
        self._tasks: set[asyncio.Task] = set()

    def send(
        self,
        title: str,
        message: str,
        priority: int = PRIORITY_HIGH,
        tags: list[str] | None = None,
    ) -> None:
        """Schedule a push.  Safe to call from synchronous state-machine code."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _LOGGER.warning("ntfy: no event loop, dropping alert %r", title)
            return
        task = loop.create_task(self.publish(title, message, priority, tags or []))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def publish(
        self, title: str, message: str, priority: int, tags: list[str]
    ) -> bool:
        payload = {
            "topic": self.topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags,
        }
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session, session.post(
                f"{self.base}/", json=payload, headers=headers
            ) as response:
                if response.status >= 400:
                    body = (await response.text())[:200]
                    _LOGGER.warning("ntfy: HTTP %d: %s", response.status, body)
                    return False
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.warning("ntfy: could not reach %s: %s", self.base, exc)
            return False
        _LOGGER.info("ntfy: sent %r to %s", title, self.topic)
        return True

    async def drain(self) -> None:
        """Wait for in-flight pushes, e.g. at shutdown."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
