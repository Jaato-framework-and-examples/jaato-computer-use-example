"""Follow-the-foreground re-scope logic (Controller, no device/network).

Design A: with an empty (unpinned) scope the controller learns the on-screen
package from the device's non-scope-gated ``windows`` metadata verb and
auto-re-scopes to it before observing; with a pinned scope it never re-scopes
and never calls the verb. Using ``windows`` (not the observe tree or
``window_changed``, both scope-gated) is what lets it follow *into* an app that
is currently out of scope.
"""
import asyncio

from a11y.controller import Controller
from a11y.device_session import Observation
from a11y.protocol import Snapshot


def _snap(version: int, pkg: str, activity: str, nodes=None) -> Snapshot:
    return Snapshot.parse({
        "snapshotVersion": version, "pkg": pkg, "activity": activity,
        "screen": {"width": 1080, "height": 2340}, "nodes": nodes or [],
    })


class _FakeSession:
    """Answers the ``windows`` verb with a scripted foreground pkg; returns queued
    snapshots per observe(); records each configure() scope."""

    def __init__(self, foreground, snaps, platform="android"):
        self._foreground = foreground  # str, or list consumed per windows call
        self._snaps = list(snaps)
        self.platform = platform       # the device declares what it is (hello)
        self.alive = True              # a live session (a config error re-raises, not "drop")
        self.configured_scopes = []
        self.windows_calls = 0
        self.current_snapshot = None

    async def request(self, verb, args=None):
        assert verb == "windows", verb
        self.windows_calls += 1
        spec = (self._foreground.pop(0) if isinstance(self._foreground, list)
                else self._foreground)
        # A dict is a full windows response; a bare string is a pkg whose resumed
        # activity we synthesise (foregroundPkg == the app, no overlay).
        if isinstance(spec, dict):
            return spec
        return {"foregroundPkg": spec, "foregroundActivity": f"{spec}/.Main"}

    async def observe(self, screenshot=True, screenshot_params=None):
        snap = self._snaps.pop(0)
        self.current_snapshot = snap
        return Observation(snapshot=snap, image=None)

    async def configure(self, settle, scope, shot, redaction):
        self.configured_scopes.append(list(scope))


def _controller(session, scope, follow):
    return Controller(session, audit=None, package_scope=scope,
                      screenshot_defaults={}, redaction={},
                      follow_foreground=follow)


def test_follow_rescopes_to_foreground_when_unpinned():
    # windows verb reports com.bar.app while scope is empty -> re-scope, then the
    # single observe returns that app's nodes.
    in_scope = _snap(11, "com.bar.app", "com.bar.app/.Main",
                     nodes=[{"ref": 1, "cls": "android.widget.Button",
                             "bounds": [0, 0, 10, 10], "flags": ["visible"]}])
    sess = _FakeSession("com.bar.app", [in_scope])
    ctl = _controller(sess, scope=[], follow=True)

    obs = asyncio.run(ctl.first_observation())

    assert sess.windows_calls == 1
    assert sess.configured_scopes == [["com.bar.app"]]   # re-scoped once, to the foreground
    assert obs.snapshot.version == 11 and len(obs.snapshot.nodes) == 1


def test_pinned_scope_never_follows_nor_queries():
    snap = _snap(20, "com.foo.app", "com.foo.app/.Main")
    sess = _FakeSession("com.bar.app", [snap])
    ctl = _controller(sess, scope=["com.foo.app"], follow=False)

    obs = asyncio.run(ctl.first_observation())

    assert sess.windows_calls == 0                        # verb not consulted when pinned
    assert sess.configured_scopes == []                   # stayed pinned
    assert obs.snapshot.version == 20


def test_follow_uses_activity_under_systemui_overlay():
    # AOD / lock / nav-bar overlay: top window is com.android.systemui, but the
    # resumed activity still names the real app -> follow the app, not systemui.
    in_scope = _snap(40, "com.bar.app", "com.bar.app/.Main",
                     nodes=[{"ref": 1, "cls": "android.widget.EditText",
                             "bounds": [0, 0, 10, 10], "flags": ["editable", "visible"]}])
    sess = _FakeSession({"foregroundPkg": "com.android.systemui",
                         "foregroundActivity": "com.bar.app/com.bar.app.Main"}, [in_scope])
    ctl = _controller(sess, scope=[], follow=True)

    obs = asyncio.run(ctl.first_observation())

    assert sess.configured_scopes == [["com.bar.app"]]   # followed the app, not systemui
    assert obs.snapshot.version == 40


def test_follow_scopes_to_launcher_not_stale_activity():
    # On the home / app-drawer the launcher window is focused (this OEM reports it
    # as type=="system"), and foregroundActivity is a STALE app. Must follow the
    # launcher (via fpkg == launcherPkg), not the stale activity.
    drawer = _snap(50, "com.sec.android.app.launcher", "com.sec.android.app.launcher/.Home",
                   nodes=[{"ref": 1, "cls": "android.widget.TextView",
                           "bounds": [0, 0, 10, 10], "flags": ["visible"]}])
    sess = _FakeSession({"foregroundPkg": "com.sec.android.app.launcher",
                         "foregroundActivity": "com.stale.app/.Old",
                         "launcherPkg": "com.sec.android.app.launcher",
                         "windows": [{"pkg": "com.sec.android.app.launcher",
                                      "type": "system", "focused": True}]},
                        [drawer])
    ctl = _controller(sess, scope=[], follow=True)

    obs = asyncio.run(ctl.first_observation())

    assert sess.configured_scopes == [["com.sec.android.app.launcher"]]  # launcher, not com.stale.app
    assert obs.snapshot.version == 50


def test_follow_no_rescope_when_already_in_scope():
    snap = _snap(30, "com.bar.app", "com.bar.app/.Main")
    sess = _FakeSession("com.bar.app", [snap])
    ctl = _controller(sess, scope=["com.bar.app"], follow=True)

    obs = asyncio.run(ctl.first_observation())

    assert sess.windows_calls == 1                        # checked...
    assert sess.configured_scopes == []                   # ...but foreground == scope, no re-scope
    assert obs.snapshot.version == 30


def test_follow_rescopes_to_windows_foreground_by_declared_platform():
    # A device declaring platform="windows" speaks the Windows window model: the
    # windows verb marks the active window with foreground=true and carries no
    # Android fields. Dispatch on the declared platform picks that window's
    # package (exePath here; aumid when present for UWP), NOT by sniffing fields.
    in_scope = _snap(60, r"C:\Windows\explorer.exe", None,
                     nodes=[{"ref": 1, "cls": "Button",
                             "bounds": [0, 0, 10, 10], "flags": ["clickable"]}])
    sess = _FakeSession(
        {"windows": [
            {"id": 66, "title": "Notepad", "exePath": r"C:\Windows\notepad.exe",
             "foreground": False},
            {"id": 77, "title": "File Explorer", "exePath": r"C:\Windows\explorer.exe",
             "foreground": True}]},
        [in_scope], platform="windows")
    ctl = _controller(sess, scope=[], follow=True)

    obs = asyncio.run(ctl.first_observation())

    assert sess.configured_scopes == [[r"C:\Windows\explorer.exe"]]  # the foreground window
    assert obs.snapshot.version == 60


def test_follow_prefers_aumid_for_uwp_windows_foreground():
    # A UWP window carries an aumid; that IS its package identity (Win32 windows
    # have only exePath). Prefer aumid when present, matching how the device
    # reports pkg = aumid (UWP) / exePath (Win32).
    snap = _snap(61, "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", None)
    sess = _FakeSession(
        {"windows": [{"id": 88, "title": "Calculator",
                      "exePath": r"C:\Program Files\WindowsApps\...\Calculator.exe",
                      "aumid": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                      "foreground": True}]},
        [snap], platform="windows")
    ctl = _controller(sess, scope=[], follow=True)

    asyncio.run(ctl.first_observation())

    assert sess.configured_scopes == [["Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"]]


def test_undeclared_platform_fails_loud_not_silent_default():
    # A device that declares no platform we implement must fail loud — never fall
    # back to guessing a window-model dialect (the exact fool-fallback we reject).
    import pytest
    from a11y.wire import DeviceError

    snap = _snap(70, "com.bar.app", "com.bar.app/.Main")
    sess = _FakeSession("com.bar.app", [snap], platform="")
    ctl = _controller(sess, scope=[], follow=True)

    with pytest.raises(DeviceError):
        asyncio.run(ctl.first_observation())
