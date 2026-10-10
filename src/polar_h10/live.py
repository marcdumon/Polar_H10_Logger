"""The live view of `polar-h10 record`, on stderr: strap state, heart rate, samples per stream, dropouts and markers.
Number keys add the markers of config.toml; q stops the recording."""

import asyncio
import contextlib
import os
import sys
import termios
import tty
from collections.abc import Callable, Iterator

from rich.console import Console
from rich.live import Live
from rich.table import Table

from polar_h10.recorder import NoSession, Recorder


def panel(status: dict, markers: dict[str, str]) -> Table:
    """The view of one status."""
    table = Table(title='Polar H10 logger', show_header=False, min_width=60)
    table.add_column(style='bold')
    table.add_column()
    device, session, last = status.get('device'), status.get('session'), status.get('last_hr') or {}
    table.add_row('State', status['state'])
    table.add_row('Strap', f'{device["name"]}, battery {device["battery"]} %' if device else '-')
    rr = f', last RR {last["rr_raw"][-1] * 1000 / 1024:.0f} ms' if last.get('rr_raw') else ''
    table.add_row('Heart rate', f'{last["hr_bpm"]} bpm{rr}' if last else '-')
    if session:
        table.add_row('Session', f'{session["id"]}, {session["seconds"]:.0f} s')
        table.add_row('Samples', ', '.join(f'{name} {count}' for name, count in session['samples'].items()))
        table.add_row('Dropouts', str(session['dropouts']))
        table.add_row('Markers', str(session['markers']))
        table.add_row('Folder', session['folder'])
    else:
        table.add_row('Session', 'none yet')
    if status.get('error'):
        table.add_row('Error', status['error'], style='red')
    keys = [f'{key} {label}' for key, label in sorted(markers.items())]
    table.caption = f'Keys: {", ".join([*keys, "q stop"])}'
    return table


def on_key(recorder: Recorder, markers: dict[str, str], stop: Callable[[], None]) -> Callable[[str], None]:
    """What a key does: q stops the recording; a marker key adds its marker, once the session runs."""

    def press(key: str) -> None:
        """One key press."""
        if key == 'q':
            stop()
        elif key in markers:
            with contextlib.suppress(NoSession):
                recorder.mark(markers[key], 'key')

    return press


@contextlib.contextmanager
def keys(press: Callable[[str], None]) -> Iterator[None]:
    """Single key presses without Enter go to `press` while the view runs; without a terminal, no key comes."""
    if not sys.stdin.isatty():
        yield
        return
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    def ready() -> None:
        """Each character that came."""
        for key in os.read(fd, 32).decode(errors='ignore'):
            press(key)

    loop.add_reader(fd, ready)
    try:
        yield
    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


async def show(recorder: Recorder, markers: dict[str, str], stop: asyncio.Event) -> None:
    """The view on stderr, 4 times a second, until the stop or the end of the session (the summary goes to stdout)."""
    with Live(panel(recorder.status(), markers), console=Console(stderr=True), refresh_per_second=4) as view:
        while not stop.is_set() and recorder.running:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 0.25)
            view.update(panel(recorder.status(), markers))
