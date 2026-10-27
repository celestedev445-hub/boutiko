"""Remember recently seen WhatsApp message ids.

Meta retries a webhook delivery when it doesn't get a 200 in time, so the same
message can arrive more than once. This in-memory store is enough for a single
API process; with several workers it would need to move to the database.
"""

import threading
from collections import OrderedDict


class SeenMessages:
    def __init__(self, max_size: int = 10_000) -> None:
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def add(self, message_id: str) -> bool:
        """Record the id. Returns False if it was already seen."""
        with self._lock:
            if message_id in self._ids:
                return False
            self._ids[message_id] = None
            if len(self._ids) > self._max_size:
                self._ids.popitem(last=False)
            return True

    def clear(self) -> None:
        with self._lock:
            self._ids.clear()


seen_messages = SeenMessages()
