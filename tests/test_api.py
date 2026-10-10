"""The API while `polar-h10 record` runs, with a stand-in strap: the status, markers with 201 and 409, the strap's
settings from the session or from a short connection with 503 when the strap cannot be reached, the session list, no
start or stop route, the stream names, and CORS for the Automation desk. The API runs in the test's own event loop, as
it runs in the loop of the `record` command."""

import dataclasses
from collections.abc import AsyncIterator

import httpx
import pytest
from fakes import CAPS, FakeStrap, ecg_frame, hr_bytes

from polar_h10.api import create_app
from polar_h10.config import Config
from polar_h10.hub import Hub
from polar_h10.recorder import Recorder, default_request
from polar_h10.strap import NOT_FOUND, StrapNotFound

DESK = 'http://127.0.0.1:8765'


@pytest.fixture
def fake() -> FakeStrap:
    """A strap with one heart rate message and one ECG frame of two samples."""
    return FakeStrap(hr=[hr_bytes(86, (700,))], frames={'ecg': [ecg_frame(10**9, [1, 2])]})


@pytest.fixture
def recorder(config: Config, fake: FakeStrap) -> Recorder:
    """The recorder of a `record` command, with the stand-in strap."""
    return Recorder(config, lambda: fake, Hub())


@pytest.fixture
async def client(config: Config, recorder: Recorder) -> AsyncIterator[httpx.AsyncClient]:
    """The API around the recorder."""
    app = create_app(recorder, recorder.hub, dataclasses.replace(config, cors_origins=(DESK,)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client


async def test_the_status_before_and_during_a_session(client: httpx.AsyncClient, recorder: Recorder) -> None:
    status = (await client.get('/status')).json()
    assert (status['state'], status['session'], status['version']) == ('idle', None, '0.1.0')
    await recorder.start(default_request(recorder.config))
    status = (await client.get('/status')).json()
    assert status['state'] == 'connected' and status['session']['samples']['hr'] == 1
    await recorder.stop()


async def test_a_marker_needs_a_running_session(client: httpx.AsyncClient, recorder: Recorder) -> None:
    assert (await client.post('/markers', json={'label': 'angina onset'})).status_code == 409
    await recorder.start(default_request(recorder.config))
    taken = await client.post('/markers', json={'label': 'angina onset'})
    assert taken.status_code == 201 and taken.json() == {'label': 'angina onset', 'source': 'api'}
    assert (await recorder.stop())['markers'] == 1


async def test_the_strap_settings_come_from_a_short_connection(client: httpx.AsyncClient, fake: FakeStrap) -> None:
    assert (await client.get('/capabilities')).json() == CAPS
    assert not fake.connected, 'the short connection ends'
    fake.fail = StrapNotFound(NOT_FOUND)
    refused = await client.get('/capabilities')
    assert refused.status_code == 503 and refused.json()['detail'] == NOT_FOUND


async def test_sessions_are_listed_and_read(client: httpx.AsyncClient, recorder: Recorder) -> None:
    session_id = (await recorder.start(default_request(recorder.config)))['id']
    await recorder.stop()
    assert (await client.get('/sessions')).json()[0]['id'] == session_id
    assert (await client.get(f'/sessions/{session_id}')).json()['heart']['rr_count'] == 1
    assert (await client.get('/sessions/2020-01-01_00-00-00')).status_code == 404
    assert (await client.get('/sessions/..')).status_code == 404


async def test_start_and_stop_belong_to_the_record_command(client: httpx.AsyncClient) -> None:
    assert (await client.post('/sessions', json={})).status_code == 405
    assert (await client.post('/sessions/stop')).status_code == 405


async def test_the_stream_refuses_unknown_names(client: httpx.AsyncClient) -> None:
    refused = await client.get('/stream', params={'streams': 'hr,nope'})
    assert refused.status_code == 400 and 'nope' in refused.json()['detail']


async def test_the_desk_may_call_the_api(client: httpx.AsyncClient) -> None:
    assert (await client.get('/status', headers={'Origin': DESK})).headers['access-control-allow-origin'] == DESK
