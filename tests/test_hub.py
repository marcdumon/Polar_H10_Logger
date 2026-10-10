"""The live hub: each subscriber gets what is published after it joined; a full queue loses new events, it never
blocks; the event stream sends the chosen streams as Server-Sent Events; closing the hub ends every stream."""

import pytest

from polar_h10.hub import Hub, event_stream


async def test_a_subscriber_gets_the_events_published_after_it_joined() -> None:
    hub = Hub(size=2)
    hub.publish('hr', {'n': 0})
    queue = hub.subscribe()
    for n in (1, 2, 3):
        hub.publish('hr', {'n': n})
    assert [queue.get_nowait()[1]['n'] for _ in range(queue.qsize())] == [1, 2], 'a full queue loses the new event'
    hub.unsubscribe(queue)
    assert hub.subscribers == 0


async def test_closing_the_hub_ends_each_event_stream() -> None:
    hub = Hub(size=1)
    stream = event_stream(hub, {'hr'})
    assert await anext(stream) == ': connected\n\n'
    hub.publish('hr', {'hr_bpm': 86})
    hub.close()
    hub.publish('hr', {'hr_bpm': 87})
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert hub.subscribers == 0
    with pytest.raises(StopAsyncIteration):
        await anext(event_stream(hub, {'hr'}))


async def test_the_event_stream_sends_the_chosen_streams() -> None:
    hub = Hub()
    stream = event_stream(hub, {'hr'})
    assert await anext(stream) == ': connected\n\n'
    hub.publish('ecg', {'ecg_uv': [1]})
    hub.publish('hr', {'hr_bpm': 86, 'rr_raw': [700]})
    assert await anext(stream) == 'event: hr\ndata: {"hr_bpm":86,"rr_raw":[700]}\n\n'
    await stream.aclose()
    assert hub.subscribers == 0, 'a closed stream leaves the hub'
