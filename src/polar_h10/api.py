"""The HTTP API while `polar-h10 record` runs: the status, the strap's settings, markers, the stop, the session list and a
live event stream for other programs. The record command starts the session itself; POST /stop ends it as q does. The
routes run in the event loop of the recording, so a marker never races the recorder's own writes."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from polar_h10.config import Config
from polar_h10.files import list_sessions, read_session
from polar_h10.hub import Hub, event_stream
from polar_h10.protocol import ProtocolError
from polar_h10.recorder import NoSession, Recorder
from polar_h10.strap import StrapError

STREAMS = {'hr', 'rr', 'ecg', 'acc', 'status'}


class MarkerBody(BaseModel):
    """A marker for the running session."""

    label: str
    source: str = 'api'


async def _tick(recorder: Recorder, hub: Hub) -> None:
    """The status on the event stream once a second."""
    while True:
        hub.publish('status', recorder.status())
        await asyncio.sleep(1)


def create_app(recorder: Recorder, hub: Hub, config: Config, stop: Callable[[], None]) -> FastAPI:
    """The API around the recorder of a `record` command; `stop` ends the recording, as q does."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Publish the status while the API runs."""
        ticker = asyncio.create_task(_tick(recorder, hub))
        yield
        ticker.cancel()

    app = FastAPI(title='Polar H10 logger', lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=list(config.cors_origins), allow_methods=['*'], allow_headers=['*'])

    @app.get('/status')
    async def status() -> dict:
        """The strap state and the running session."""
        return recorder.status()

    @app.get('/capabilities')
    async def capabilities() -> dict:
        """The settings the strap offers: from the running session, or from a short connection."""
        try:
            return await recorder.capabilities()
        except (StrapError, ProtocolError) as error:
            raise HTTPException(503, str(error)) from error

    @app.post('/markers', status_code=201)
    async def marker(body: MarkerBody) -> dict:
        """A marker in the running session."""
        try:
            recorder.mark(body.label, body.source)
        except NoSession as error:
            raise HTTPException(409, str(error)) from error
        return {'label': body.label, 'source': body.source}

    @app.post('/stop', status_code=202)
    async def stop_recording() -> dict:
        """End the recording, as q, Ctrl+C and SIGTERM do: the files close and the summary is printed."""
        stop()
        return {'stopping': True}

    @app.get('/stream')
    async def stream(streams: str = 'hr,ecg,acc,status') -> StreamingResponse:
        """Server-Sent Events of the chosen streams; rr is part of hr."""
        wanted = {name.strip() for name in streams.split(',') if name.strip()}
        if unknown := wanted - STREAMS:
            raise HTTPException(400, f'Unknown streams: {", ".join(sorted(unknown))}. Choose from {", ".join(sorted(STREAMS))}.')
        if 'rr' in wanted:
            wanted.add('hr')
        return StreamingResponse(event_stream(hub, wanted), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    @app.get('/sessions')
    async def sessions() -> list[dict]:
        """All sessions, newest first."""
        return list_sessions(config.storage_dir)

    @app.get('/sessions/{session_id}')
    async def session(session_id: str) -> dict:
        """One session's session.json."""
        found = read_session(config.storage_dir, session_id)
        if found is None:
            raise HTTPException(404, f'No session {session_id}.')
        return found

    return app
