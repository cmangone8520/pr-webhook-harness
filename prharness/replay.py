from __future__ import annotations

from collections import OrderedDict


class ReplayCache:
    """Bounded TTL cache of ``X-GitHub-Delivery`` ids used to drop redeliveries."""

    def __init__(self, ttl_seconds: float, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[str, float] = OrderedDict()

    def seen(self, delivery_id: str, now: float) -> bool:
        """Return ``True`` if already seen; otherwise record it and return ``False``."""
        self._evict_expired(now)
        if delivery_id in self._entries:
            return True
        self._entries[delivery_id] = now + self._ttl
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        while self._entries:
            key, expiry = next(iter(self._entries.items()))
            if expiry > now:
                break
            self._entries.pop(key)
