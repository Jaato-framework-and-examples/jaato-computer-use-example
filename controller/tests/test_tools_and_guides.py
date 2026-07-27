"""The single-agent tool surface: shared screen_* (incl. screen_observe), the
platform-gated sets, and the per-platform operating guides that connect_device
returns instead of a per-platform persona.
"""
from types import SimpleNamespace

import pytest

from a11y.host_tools import build_tools, load_guide


def _names(platform):
    return {s["name"] for s in build_tools(SimpleNamespace(platform=platform))}


def test_shared_tools_include_observe_on_every_platform():
    for platform in ("windows", "android"):
        names = _names(platform)
        assert {"screen_tap", "screen_type", "screen_scroll", "screen_submit",
                "screen_gesture", "screen_observe", "screen_wait",
                "screen_done"} <= names


def test_windows_gets_desktop_tools_not_android_nav():
    names = _names("windows")
    assert {"screen_windows", "screen_start_menu", "screen_type_text",
            "screen_enter", "screen_close_window", "screen_switch_window"} <= names
    assert not ({"screen_back", "screen_home", "screen_recents"} & names)


def test_android_gets_nav_not_windows_tools():
    names = _names("android")
    assert {"screen_back", "screen_home", "screen_recents"} <= names
    assert not ({"screen_windows", "screen_start_menu", "screen_switch_window"} & names)


def test_load_guide_per_platform_and_missing_is_loud():
    win = load_guide("windows")          # bundled package asset, no workspace path
    andr = load_guide("android")
    assert "Windows" in win and "screen_start_menu" in win
    assert "Android" in andr and "screen_back" in andr
    with pytest.raises(FileNotFoundError):
        load_guide("symbian")
