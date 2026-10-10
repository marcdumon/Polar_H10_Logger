"""The settings of config.toml: where the files go, which strap, the default streams, the API, reconnects and the
marker labels. A missing or wrong setting is named with its key and the file."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / 'config.toml'
REQUIRED = object()
# the keys of each section; [markers] takes any key (a key of the live view)
KNOWN = {'storage': {'dir', 'memory_dir'}, 'device': {'name_prefix', 'address', 'scan_seconds'},
         'streams': {'hr', 'ecg', 'ecg_sample_rate', 'acc', 'acc_sample_rate', 'acc_range'},
         'api': {'host', 'port', 'cors_origins'}, 'reconnect': {'max_tries', 'first_wait_s', 'max_wait_s'}, 'markers': None}


class ConfigError(Exception):
    """A missing or wrong setting."""


@dataclass(frozen=True)
class Streams:
    """The streams a session starts with when the request does not say otherwise."""

    hr: bool = True
    ecg: bool = True
    ecg_sample_rate: int = 130
    acc: bool = True
    acc_sample_rate: int = 200
    acc_range: int = 8


@dataclass(frozen=True)
class Config:
    """All settings; the paths are absolute."""

    storage_dir: Path
    memory_dir: Path
    name_prefix: str = 'Polar H10'
    address: str = ''
    scan_seconds: float = 20.0
    streams: Streams = field(default_factory=Streams)
    host: str = '127.0.0.1'
    port: int = 8770
    cors_origins: tuple[str, ...] = ()
    max_tries: int = 0
    first_wait_s: float = 2.0
    max_wait_s: float = 30.0
    markers: dict[str, str] = field(default_factory=dict)


def _value(raw: dict, section: str, key: str, kind: type, path: Path, default: object = REQUIRED) -> object:
    """One setting, checked against its type: an int is also a float, a bool is never a number."""
    value = raw.get(section, {}).get(key, default)
    if value is REQUIRED:
        raise ConfigError(f'Missing [{section}] {key} in {path}.')
    is_bool = isinstance(value, bool)
    if kind is float and isinstance(value, int) and not is_bool:
        return float(value)
    if not isinstance(value, kind) or (kind in (int, float) and is_bool):
        raise ConfigError(f'[{section}] {key} in {path} must be {kind.__name__}, not {value!r}.')
    return value


def _path(value: str, path: Path) -> Path:
    """A path of the config: ~ is the home folder, a relative path is relative to the config file's folder."""
    found = Path(value).expanduser()
    return found if found.is_absolute() else (path.parent / found).resolve()


def _check_names(raw: dict, path: Path) -> None:
    """A misspelled section or key is named: ignored, it would silently record with other settings."""
    for section, values in raw.items():
        if section not in KNOWN:
            raise ConfigError(f'Unknown section [{section}] in {path}. The sections are: {", ".join(KNOWN)}.')
        keys = KNOWN[section]
        for key in values if keys is not None and isinstance(values, dict) else ():
            if key not in keys:
                raise ConfigError(f'Unknown setting [{section}] {key} in {path}. [{section}] has: {", ".join(sorted(keys))}.')


def load(path: Path = DEFAULT_PATH) -> Config:
    """Read and check the config file."""
    if not path.exists():
        raise ConfigError(f'No config file at {path}. Copy config.example.toml to config.toml and set [storage] dir.')
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f'{path} is not valid TOML: {error}') from error
    _check_names(raw, path)
    defaults = Streams()
    streams = Streams(**{name: _value(raw, 'streams', name, type(getattr(defaults, name)), path, getattr(defaults, name))
                         for name in ('hr', 'ecg', 'ecg_sample_rate', 'acc', 'acc_sample_rate', 'acc_range')})
    origins = _value(raw, 'api', 'cors_origins', list, path, [])
    if not all(isinstance(origin, str) for origin in origins):
        raise ConfigError(f'[api] cors_origins in {path} must be a list of texts.')
    markers = raw.get('markers', {})
    if not all(isinstance(label, str) for label in markers.values()):
        raise ConfigError(f'Each label in [markers] of {path} must be a text.')
    return Config(
        storage_dir=_path(_value(raw, 'storage', 'dir', str, path), path),
        memory_dir=_path(_value(raw, 'storage', 'memory_dir', str, path), path),
        name_prefix=_value(raw, 'device', 'name_prefix', str, path, 'Polar H10'),
        address=_value(raw, 'device', 'address', str, path, ''),
        scan_seconds=_value(raw, 'device', 'scan_seconds', float, path, 20.0),
        streams=streams,
        host=_value(raw, 'api', 'host', str, path, '127.0.0.1'),
        port=_value(raw, 'api', 'port', int, path, 8770),
        cors_origins=tuple(origins),
        max_tries=_value(raw, 'reconnect', 'max_tries', int, path, 0),
        first_wait_s=_value(raw, 'reconnect', 'first_wait_s', float, path, 2.0),
        max_wait_s=_value(raw, 'reconnect', 'max_wait_s', float, path, 30.0),
        markers=dict(markers),
    )
