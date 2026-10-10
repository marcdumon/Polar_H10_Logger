"""Capture a few raw notifications of the real strap into tests/fixtures/h10_frames.json, for the regression test.
Wear the strap, close the phone app, then: uv run python scripts/capture_frames.py"""

import asyncio
import json
from pathlib import Path

from polar_h10.config import load
from polar_h10.strap import BleakStrap

OUT = Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'h10_frames.json'
KEEP = 5
SECONDS = 5


async def main() -> None:
    """Connect, run every stream for a few seconds, keep the first notifications of each."""
    strap = BleakStrap(load())
    got: dict[str, list[str]] = {'hr': [], 'ecg': [], 'acc': []}
    await strap.connect(lambda: None)
    try:
        caps = await strap.capabilities()
        await strap.start_hr(lambda data: got['hr'].append(data.hex()))
        await strap.start_pmd('ecg', {'sample_rate': 130, 'resolution': 14}, lambda data: got['ecg'].append(data.hex()))
        await strap.start_pmd('acc', {'sample_rate': 200, 'resolution': 16, 'range': 8}, lambda data: got['acc'].append(data.hex()))
        await asyncio.sleep(SECONDS)
        await strap.stop_pmd('ecg')
        await strap.stop_pmd('acc')
    finally:
        await strap.disconnect()
    OUT.parent.mkdir(exist_ok=True)
    kept = {name: hexes[:KEEP] for name, hexes in got.items()}
    OUT.write_text(json.dumps({'capabilities': caps, 'mtu': strap.mtu} | kept, indent=2))
    print(f'Wrote {OUT}: ' + ', '.join(f'{name} {len(hexes)}' for name, hexes in got.items()))


asyncio.run(main())
