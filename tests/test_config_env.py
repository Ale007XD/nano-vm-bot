"""Разбор списков ID из env (ADMIN_IDS, KB_CHANNEL_IDS) настоящим Settings."""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "core", "config.py")


@pytest.fixture
def Settings():
    spec = importlib.util.spec_from_file_location("_real_config_for_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Settings


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("123456789", [123456789]),
        ("123456789,987654321", [123456789, 987654321]),
        (" 1 , 2 ,", [1, 2]),
        ("[1, 2]", [1, 2]),
        ("", []),
    ],
)
def test_admin_ids_formats(Settings, monkeypatch, raw, expected):
    monkeypatch.setenv("ADMIN_IDS", raw)
    assert Settings(_env_file=None).ADMIN_IDS == expected


def test_kb_channel_ids_negative_and_empty(Settings, monkeypatch):
    monkeypatch.setenv("KB_CHANNEL_IDS", "-1001234567890,-1009876543210")
    assert Settings(_env_file=None).KB_CHANNEL_IDS == [-1001234567890, -1009876543210]
    monkeypatch.setenv("KB_CHANNEL_IDS", "")
    assert Settings(_env_file=None).KB_CHANNEL_IDS == []


def test_showcase_open_access_formats(Settings, monkeypatch):
    for raw, expected in (("", None), ("true", True), ("false", False)):
        monkeypatch.setenv("SHOWCASE_OPEN_ACCESS", raw)
        assert Settings(_env_file=None).SHOWCASE_OPEN_ACCESS is expected


def test_env_example_loads(Settings, monkeypatch, tmp_path):
    """.env.example должен читаться как есть (регрессия: список админов через запятую)."""
    example = os.path.join(os.path.dirname(__file__), "..", ".env.example")
    for k in ("ADMIN_IDS", "KB_CHANNEL_IDS", "SHOWCASE_OPEN_ACCESS"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=example)
    assert s.ADMIN_IDS == [123456789, 987654321]
    assert s.KB_CHANNEL_IDS == []
    assert s.APP_VERSION == "1.10.2"
