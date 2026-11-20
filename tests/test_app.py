import socket

import numpy as np

from tuneview.app import (
    _PACKAGE_ASSETS, _announce_web, _audio_asset, _lan_address, _web_assets, _web_port,
)


def test_web_port_uses_flet_port_first(monkeypatch):
    monkeypatch.setenv("FLET_SERVER_PORT", "9123")
    monkeypatch.setenv("TUNEVIEW_PORT", "8123")
    assert _web_port() == 9123


def test_web_port_uses_tuneview_port(monkeypatch):
    monkeypatch.delenv("FLET_SERVER_PORT", raising=False)
    monkeypatch.setenv("TUNEVIEW_PORT", "8456")
    assert _web_port() == 8456


def test_web_port_picks_a_free_port(monkeypatch):
    monkeypatch.delenv("FLET_SERVER_PORT", raising=False)
    monkeypatch.delenv("TUNEVIEW_PORT", raising=False)
    port = _web_port()
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", port))  # it really was free


def test_announce_web_prints_this_computer_and_the_lan(monkeypatch, capsys):
    monkeypatch.setattr("tuneview.app._lan_address", lambda: "192.168.1.5")
    _announce_web(8555)
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8555/" in out
    assert "http://192.168.1.5:8555/" in out


def test_lan_address_is_not_loopback():
    assert not _lan_address().startswith("127.")


def test_audio_asset_is_a_content_addressed_wav():
    pcm = np.zeros(100, dtype=np.int16)
    url = _audio_asset(pcm)
    assert url.startswith("audio/") and url.endswith(".wav")
    path = _web_assets() / url
    assert path.exists() and path.stat().st_size > 44
    assert _audio_asset(pcm) == url  # the same sound reuses the same file


def test_packaged_app_icons_are_shipped():
    for name in ("favicon.png", "icon.png", "icon.ico", "manifest.json"):
        path = _PACKAGE_ASSETS / name
        assert path.exists() and path.stat().st_size > 0
    for name in ("icon-192.png", "icon-512.png", "icon-maskable-192.png",
                 "icon-maskable-512.png", "apple-touch-icon-192.png",
                 "loading-animation.png"):
        assert (_PACKAGE_ASSETS / "icons" / name).exists()
    assert (_PACKAGE_ASSETS / "icons" / "LICENSE-APACHE-2.0.txt").exists()


def test_web_assets_override_flet_icons():
    assets = _web_assets()
    assert (assets / "favicon.png").read_bytes() == (
        _PACKAGE_ASSETS / "favicon.png").read_bytes()
    assert (assets / "icons" / "icon-192.png").read_bytes() == (
        _PACKAGE_ASSETS / "icons" / "icon-192.png").read_bytes()
