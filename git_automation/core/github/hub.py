"""A tiny in-process async pub/sub hub for fanning GitHub events out to clients.

The background notification poller is the sole *publisher*: after each poll round
it calls :meth:`Hub.publish` with an event dict. Each connected browser opens a
WebSocket that :meth:`Hub.subscribe`\\s to get its own queue and forwards every
event it receives. This decouples the single poll loop from N live subscribers.

Design constraints:

* **Non-blocking publisher.** :meth:`publish` must never block or await on a
  slow/stuck subscriber. Each subscriber owns a *bounded* queue; when it is full
  we drop the *oldest* event for that subscriber and enqueue the newest, so a
  laggy client loses history but never stalls the poller or other subscribers.
* **Clean teardown.** A disconnecting socket calls :meth:`unsubscribe`, so no
  subscriber queue is ever leaked.

A module-level singleton :data:`hub` is the shared instance the app wires up.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Bounded per-subscriber backlog. Small: live UI cares about the latest state,
# not deep history, and a full queue triggers drop-oldest rather than blocking.
_QUEUE_MAXSIZE = 64


class Hub:
    """An async fan-out: many subscriber queues fed by one publisher."""

    def __init__(self, *, maxsize: int = _QUEUE_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[dict]] = set()

    def subscribe(self) -> asyncio.Queue[dict]:
        """Register and return a fresh bounded queue receiving published events."""
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        """Remove ``queue`` from the fan-out set (idempotent)."""
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        """Number of currently registered subscriber queues."""
        return len(self._subscribers)

    async def publish(self, event: dict) -> None:
        """Fan ``event`` out to every subscriber without ever blocking.

        For each subscriber queue we enqueue ``event``; if the queue is full we
        discard its oldest pending event and retry, so a slow consumer loses
        history but never stalls the publisher or its peers.
        """
        # Snapshot so a concurrent (un)subscribe cannot mutate the set mid-loop.
        for queue in list(self._subscribers):
            self._offer(queue, event)

    def _offer(self, queue: asyncio.Queue[dict], event: dict) -> None:
        """Enqueue ``event`` on ``queue``, dropping the oldest item if full."""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()  # drop oldest
            except asyncio.QueueEmpty:  # pragma: no cover - racing drain
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - racing fill
                logger.debug("hub: subscriber queue still full after drop; dropping event")


# Shared singleton wired into the poller (publisher) and the WS endpoint (subscribers).
hub = Hub()
