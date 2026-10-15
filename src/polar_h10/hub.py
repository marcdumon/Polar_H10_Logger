"""The live hub: every sample batch and the status go to each subscriber's queue (the live views and the API's event
streams); a full queue loses new events instead of slowing the logger. event_stream turns a subscription into
Server-Sent Events text; close() ends every stream, so the API can stop at once."""

import asyncio
import json
from collections.abc import AsyncIterator

COMPACT = (',', ':')


class Hub:
    """The subscribers of the live data."""

    def __init__(self, size: int = 512) -> None:
        """Each subscriber's queue holds at most `size` events."""
        self._size = size
        self._queues: set[asyncio.Queue] = set()
        self.closed = False

    @property
    def subscribers(self) -> int:
        """How many subscribers there are."""
        return len(self._queues)

    def subscribe(self) -> asyncio.Queue:
        """A new queue of (event, data)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._size)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a queue."""
        self._queues.discard(queue)

    def publish(self, event: str, data: dict) -> None:
        """An event to every queue that has room; nothing after close()."""
        if self.closed:
            return
        for queue in self._queues:
            if not queue.full():
                queue.put_nowait((event, data))

    def close(self) -> None:
        """End every event stream: each queue drops what it holds and gets the end mark None."""
        self.closed = True
        for queue in self._queues:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)


async def event_stream(hub: Hub, streams: set[str]) -> AsyncIterator[str]:
    """Server-Sent Events of the chosen streams, until the client goes away or the hub closes."""
    if hub.closed:
        return
    queue = hub.subscribe()
    try:
        yield ': connected\n\n'
        while (item := await queue.get()) is not None:
            event, data = item
            if event in streams:
                yield f'event: {event}\ndata: {json.dumps(data, separators=COMPACT)}\n\n'
    finally:
        hub.unsubscribe(queue)
