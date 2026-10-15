# Polar H10 logger

Logs everything a Polar H10 chest strap sends over Bluetooth LE to CSV files, live: heart rate with RR intervals, ECG
(130 Hz, µV), accelerometer (25–200 Hz, ±2–8 g, mg), battery and events. While it records, a local API serves the
status, sessions and a live event stream for other programs.

## Set up

```bash
uv sync
cp config.example.toml config.toml   # then set [device] address, [storage] dir and the marker labels
```

## Use

1. Wear the strap with wet electrodes. Close the phone app that uses it: the strap accepts one connection at a time.
2. `uv run polar-h10 record` connects to the strap and records the streams of `config.toml` until you press `q` or
   Ctrl+C. The terminal shows the strap, the heart rate and the samples; the number keys add the markers of
   `config.toml`. At the end it prints the summary of the session. `--no-acc`, `--acc-rate 50`, `--acc-range 2`,
   `--ecg-resolution 14` and the other options change one session.
3. Other commands: `uv run polar-h10 capabilities` (the settings the strap offers), `uv run polar-h10 sessions` (all
   sessions), and `uv run polar-h10 mark "angina onset"` (a marker from another terminal while `record` runs).

## Files

One folder per session below `[storage] dir`, named by its start time: `hr.csv`, `rr.csv`, `ecg.csv`, `acc.csv`,
`markers.csv`, `events.csv` and `session.json`. `host_ns` is the PC clock (ns since 1970, UTC); `sensor_ns` is the
strap clock. Load a file with `pd.read_csv(folder / 'ecg.csv')`.

## Dropouts

After a lost connection the logger reconnects and goes on in the same session (`dropout` and `reconnected` in
`events.csv`). Heart rate comes back at once; the strap itself sends ECG and accelerometer data again only about 30 s
after the reconnect (measured on 2026-10-09, three times). That gap shows in `sensor_ns`.

## API

Only while `polar-h10 record` runs, at `http://127.0.0.1:8770`: `GET /status`, `GET /capabilities`, `POST /markers`,
`POST /stop` (ends the recording, as `q` does), `GET /stream?streams=hr,ecg,acc,status` (Server-Sent Events),
`GET /sessions`, `GET /sessions/{id}`. The record command starts the session itself.
