"""config.toml: the example loads, paths become absolute, and a missing or wrong setting is named with key and file."""

import shutil
from pathlib import Path

import pytest

from polar_h10.config import ConfigError, Streams, load

EXAMPLE = Path(__file__).resolve().parents[1] / 'config.example.toml'


def write(tmp_path: Path, text: str) -> Path:
    """A config file with this text."""
    path = tmp_path / 'config.toml'
    path.write_text(text)
    return path


def test_the_example_loads_with_paths_beside_the_file(tmp_path: Path) -> None:
    path = tmp_path / 'config.toml'
    shutil.copy(EXAMPLE, path)
    config = load(path)
    assert config.storage_dir == tmp_path / 'data' / 'sessions' and config.memory_dir == tmp_path / 'data' / 'memory'
    assert config.streams == Streams() and config.port == 8770 and config.cors_origins == ('http://127.0.0.1:8765',)
    assert config.markers['1'] == 'angina onset' and config.max_tries == 0 and config.first_wait_s == 2.0


def test_only_the_storage_paths_are_required(tmp_path: Path) -> None:
    config = load(write(tmp_path, "[storage]\ndir = '/srv/h10'\nmemory_dir = '~/h10-memory'\n"))
    assert config.storage_dir == Path('/srv/h10') and config.memory_dir == Path.home() / 'h10-memory'
    assert config.name_prefix == 'Polar H10' and config.address == '' and config.markers == {}


STORAGE = "[storage]\ndir = 'a'\nmemory_dir = 'b'\n"


@pytest.mark.parametrize(('text', 'message'), [
    ('', r'Missing \[storage\] dir'),
    (STORAGE + "[api]\nport = 'x'\n", r'\[api\] port .* must be int'),
    (STORAGE + '[api]\nport = true\n', r'\[api\] port .* must be int'),
    (STORAGE + '[streams]\nhr = 1\n', r'\[streams\] hr .* must be bool'),
    (STORAGE + '[api]\ncors_origins = [1]\n', r'cors_origins .* list of texts'),
    (STORAGE + '[markers]\n1 = 2\n', r'\[markers\]'),
    ('[storage\n', 'not valid TOML'),
])
def test_a_wrong_setting_is_named_with_its_key_and_file(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load(write(tmp_path, text))


@pytest.mark.parametrize(('text', 'message'), [
    (STORAGE + '[streams]\nacc_rate = 50\n', r'Unknown setting \[streams\] acc_rate .* acc_sample_rate'),
    (STORAGE + '[stream]\nacc = false\n', r'Unknown section \[stream\]'),
])
def test_a_misspelled_setting_is_named_not_ignored(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load(write(tmp_path, text))


def test_a_missing_file_says_how_to_make_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r'Copy config\.example\.toml'):
        load(tmp_path / 'config.toml')
