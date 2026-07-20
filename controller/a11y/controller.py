"""The control loop's shared state + the act->settle->recover->reobserve cycle.

:class:`Controller` is the daemon-side "mind" state that the ``screen.*`` host
tools mutate. It holds the live :class:`DeviceSession`, the audit trail, the
settle tuner, and the *pending observation* — the fresh snapshot produced after
the last action.

Computer-use interaction: each ``screen.*`` action re-observes and the host tool
returns the fresh set-of-marks screenshot as its *tool result*, so the model
sees the effect of every action and drives a multi-step loop within one turn.
``acted_this_turn`` is tracked only so the outer loop can tell an action turn
from a pure-conversation one — it no longer refuses multiple actions.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, List, Optional

import websockets

from . import grounding
from .audit import AuditLog
from .device_session import DeviceSession, Observation
from .protocol import Action, Selector, SettleConfig
from .settle_policy import SettleTuner, session_default
from .wire import DeviceError, ErrorCode

log = logging.getLogger("a11y.controller")


class Controller:
    """Owns the observe/act/settle policy state for one device session."""

    def __init__(self, session: DeviceSession, audit: AuditLog,
                 package_scope: List[str], screenshot_defaults: dict,
                 redaction: dict, settle_ceiling_s: float = 12.0,
                 follow_foreground: bool = True,
                 reacquire: Optional[Callable[[Optional[str]],
                                              Awaitable[Optional[DeviceSession]]]] = None
                 ) -> None:
        self._session = session
        self._audit = audit
        self._scope = package_scope
        self._shot_defaults = screenshot_defaults
        self._redaction = redaction
        self._settle_ceiling_s = settle_ceiling_s
        # Returns the newest live bridge session (waits for a reconnect); used to
        # recover when the held session drops. None disables recovery.
        self._reacquire = reacquire
        # follow-the-foreground: after every observe, re-scope to the on-screen
        # app so the agent always sees the current window. Disabled when the
        # operator pins a scope (non-empty package_scope).
        self._follow = follow_foreground

        pkg = package_scope[0] if package_scope else ""
        self._settle = session_default(pkg)
        self._tuner = SettleTuner(self._settle)

        self.pending_observation: Optional[Observation] = None
        self._acted_this_turn = False
        self.done = False
        self.done_summary = ""

    # -- lifecycle -----------------------------------------------------------
    async def configure(self) -> None:
        """Push the initial session policy (01 §5.1). With a non-empty scope the
        device starts observing/acting; an empty scope keeps it fail-closed."""
        await self._session.configure(
            self._settle, self._scope, self._shot_defaults, self._redaction)

    async def first_observation(self) -> Observation:
        return await self._observe_and_follow()

    @property
    def platform(self) -> str:
        """The device's declared platform ("android" / "windows"). Drives the
        platform-specific host tools and the first-turn desktop preamble."""
        return self._session.platform

    async def list_windows(self) -> dict:
        """Raw ``windows`` verb result — every top-level window, non-scope-gated
        (the device owns the shape: on Windows, ``windows[]`` of id / title /
        exePath / aumid / foreground). Backs the ``screen_windows`` tool and the
        Windows first-turn desktop preamble."""
        return await self._session.request("windows", {})

    # -- follow-the-foreground ----------------------------------------------
    async def _foreground_pkg(self) -> str:
        """Foreground package to scope to, from the non-scope-gated ``windows``
        verb. The window model that verb returns differs by platform, so the
        device *declares* its platform in ``hello`` and we dispatch on it — we do
        NOT sniff which fields the response carries (that would infer the platform
        by guessing; the device is the source of truth for what it is). A device
        that declares no platform we implement is a fail-loud error, never a
        silent default to one shape.
        """
        data = await self._session.request("windows", {})
        platform = self._session.platform
        if platform == "android":
            return self._foreground_android(data)
        if platform == "windows":
            return self._foreground_windows(data)
        raise DeviceError(
            ErrorCode.INTERNAL,
            f"device declared platform {platform!r}; no foreground-pick strategy "
            "for it — the device must send a known 'platform' in its hello")

    @staticmethod
    def _foreground_android(data: dict) -> str:
        """Android window model. Neither field is right alone: ``foregroundPkg``
        (top window) becomes ``com.android.systemui`` under a system overlay (nav
        bar / shade / AOD / lock), while ``foregroundActivity`` (resumed activity)
        goes stale on the launcher/home (many OEMs report the launcher window as
        ``type=="system"``, so no app activity resumes). Combine them:

        - focused window is an app (``type=="application"``) or the launcher
          (``pkg == launcherPkg``) -> it's a real foreground surface; scope to it.
        - otherwise the focused window is system chrome / AOD; the resumed
          activity still names the app behind it, so scope to that.
        """
        fpkg = data.get("foregroundPkg") or ""
        launcher = data.get("launcherPkg") or ""
        focused = next((w for w in (data.get("windows") or []) if w.get("focused")), None)
        ftype = focused.get("type") if focused else None
        activity = data.get("foregroundActivity") or ""
        act_pkg = activity.split("/", 1)[0] if "/" in activity else ""
        if fpkg and (fpkg == launcher or ftype == "application"):
            target = fpkg
        else:
            target = act_pkg or fpkg
        log.info("foreground pick (android): fpkg=%s launcher=%s ftype=%s act=%s -> %s",
                 fpkg, launcher, ftype, act_pkg, target)
        return target

    @staticmethod
    def _foreground_windows(data: dict) -> str:
        """Windows window model (04-DEVICE_DESIGN-WINDOWS §9). No launcher /
        resumed-activity / system-overlay nuance to reconcile: the ``windows``
        verb marks the active top-level window with ``foreground==true``, and a
        window's package identity is its AUMID (UWP) or executable path (Win32) —
        the same ``pkg`` observe reports. Pick that window's package; no window
        foregrounded -> empty string (fail-closed, exactly as the Android path
        yields "" when nothing resolves)."""
        win = next((w for w in (data.get("windows") or []) if w.get("foreground")), None)
        target = (win.get("aumid") or win.get("exePath") or "") if win else ""
        log.info("foreground pick (windows): win=%s -> %s",
                 win.get("title") if win else None, target)
        return target

    async def _apply_scope(self, pkg: str) -> None:
        """Re-point the session (and settle policy) at a new package scope."""
        self._scope = [pkg]
        self._settle = session_default(pkg)
        self._tuner = SettleTuner(self._settle)
        await self._session.configure(
            self._settle, self._scope, self._shot_defaults, self._redaction)
        log.info("followed foreground -> re-scoped to %s", pkg)

    async def _observe_and_follow(self) -> Observation:
        """Observe (following the foreground), recovering from a dropped device.
        Every observe path funnels through here — startup, post-act, post-wait —
        and the act path reaches it via ``_reobserve``, so recovering here alone
        makes a flap a brief pause rather than a stuck 'device offline'. A live-
        session error (grounding, settle) is re-raised unchanged; only a *dead*
        session triggers adoption of the newest one."""
        try:
            return await self._observe_once()
        except (DeviceError, websockets.ConnectionClosed):
            if self._session.alive:
                raise  # a genuine op error on a live session, not a drop
            status = await self._recover_session()
            if status != "recovered":
                raise DeviceError(ErrorCode.INTERNAL, "device unavailable — no reconnect")
            return await self._observe_once()

    async def _observe_once(self) -> Observation:
        """In follow mode, re-scope to the on-screen app (from the ``windows``
        verb) before observing, so the snapshot carries that app's nodes."""
        if self._follow:
            fg = await self._foreground_pkg()
            cur = self._scope[0] if self._scope else ""
            if fg and fg != cur:
                await self._apply_scope(fg)
        obs = await self._session.observe(screenshot=True, screenshot_params=self._shot_defaults)
        self.pending_observation = obs
        return obs

    async def _recover_session(self) -> str:
        """The held session is dead. Wait for the device to dial back in and adopt
        the newest live bridge session (re-configured to the current scope), so a
        flap — or an operator DISCONNECT that is followed by a reconnect — resumes
        cleanly. The bye reason is passed to ``reacquire`` so it can announce a
        ``user_disconnect`` distinctly (reconnect the device to resume) vs a bare
        network drop. Returns ``"recovered"``, or ``"unavailable"`` if nothing
        comes back in time (or recovery is disabled)."""
        if self._reacquire is None:
            return "unavailable"
        try:
            new = await self._reacquire(self._session.bye_reason)
        except Exception as exc:  # timeout / bridge stopped
            log.warning("reconnect wait ended without a device: %s", exc)
            return "unavailable"
        if new is None:
            return "unavailable"
        self._session = new
        self._settle = session_default(self._scope[0] if self._scope else "")
        self._tuner = SettleTuner(self._settle)
        await self._session.configure(
            self._settle, self._scope, self._shot_defaults, self._redaction)
        log.info("adopted new session %s (scope=%s)", new.device_id, self._scope or [])
        return "recovered"

    def begin_turn(self) -> None:
        """Reset the per-turn action guard before pushing a new observation."""
        self._acted_this_turn = False

    @property
    def acted_this_turn(self) -> bool:
        return self._acted_this_turn

    # -- action entry points (called by host tools) --------------------------
    async def act_ref(self, ref: int, action: Action) -> str:
        """Execute ``action`` against the set-of-marks ``ref`` on the current
        snapshot: ground the ref to a selector, act, settle, recover, re-observe.
        Returns a compact ack for the model."""
        snap = self._session.current_snapshot
        if snap is None:
            return "no current snapshot — observation pending"
        try:
            node = snap.by_ref(ref)
        except KeyError:
            return f"ref {ref} is not on the current screen (v{snap.version}); wait for the next screenshot"
        selector = grounding.to_selector(ref, snap, acting_immediately=True)
        # A version-independent fallback (viewId/text/bounds) for the STALE-retry:
        # if the screen churns between observe and act, this resolves the same
        # node in the CURRENT tree instead of failing on a moved snapshotVersion.
        stable = grounding.to_selector(ref, snap, acting_immediately=False)
        return await self._act_and_settle(selector, action, node.bounds, stable)

    async def global_action(self, name: str) -> str:
        """Perform a GLOBAL action (BACK/HOME/RECENTS). No selector target."""
        return await self._act_and_settle(Selector(), Action.global_(name), None)

    async def gesture(self, path: List[List[int]], duration_ms: int) -> str:
        """Raw gesture escape hatch (a swipe/tap by coordinate)."""
        xs = [p[0] for p in path]
        ys = [p[1] for p in path]
        bounds = [min(xs), min(ys), max(xs), max(ys)]
        action = (Action.gesture_tap(path[0][0], path[0][1], duration_ms)
                  if len(path) == 1 else Action.gesture_swipe(path, duration_ms))
        return await self._act_and_settle(Selector(bounds=bounds), action, None)

    async def wait(self) -> str:
        """Wait for the UI to settle without acting (waitForSettle), then
        re-observe. For screens that keep changing after an external trigger."""
        self._acted_this_turn = True
        try:
            settled = await self._session.wait_for_settle(timeout=self._settle_ceiling_s + 20)
            note = f"settled({settled.reason}) v{settled.version}"
        except DeviceError as exc:
            note = f"wait error {exc.code}"
        await self._reobserve()
        return f"waited: {note}; screen refreshed"

    def mark_done(self, summary: str) -> str:
        self.done = True
        self.done_summary = summary
        return "acknowledged done"

    # -- internals -----------------------------------------------------------
    async def _act_and_settle(self, selector: Selector, action: Action,
                             node_bounds: Optional[List[int]],
                             stable_selector: Optional[Selector] = None) -> str:
        """The core cycle (03 §3): act, await settle (with recovery), adapt the
        settle policy from the outcome, re-observe, audit, and return an ack."""
        self._acted_this_turn = True
        from_version = self._session.current_snapshot.version if self._session.current_snapshot else 0
        try:
            await self._session.act(selector, action)
        except DeviceError as exc:
            if exc.code == ErrorCode.STALE and stable_selector is not None:
                # The screen churned (live clock/animation) between observe and
                # act, so the version-bound ref selector went stale even though
                # the target node is unchanged. Retry once with a version-
                # independent selector resolved in the current tree.
                log.info("STALE (screen churned) — retrying with a version-independent selector")
                try:
                    await self._session.act(stable_selector, action)
                    selector = stable_selector
                except DeviceError as exc2:
                    await self._reobserve()
                    return f"action not applied (STALE; retry {exc2.code}); screen refreshed"
            else:
                rec = grounding.describe_recovery(exc, node_bounds, action)
                if rec.next == "retry" and rec.selector and rec.action:
                    log.info("recovery retry: %s", rec.reason)
                    try:
                        await self._session.act(rec.selector, rec.action)
                        selector, action = rec.selector, rec.action
                    except DeviceError as exc2:
                        await self._reobserve()
                        return f"action failed ({exc2.code}); screen refreshed"
                else:
                    await self._reobserve()
                    return f"action not applied ({exc.code}: {rec.reason}); screen refreshed"

        try:
            settled = await self._session.await_settled(timeout=self._settle_ceiling_s)
            reason, sv = settled.reason, settled.version
        except DeviceError:
            reason, sv = "timeout", from_version

        widened = self._tuner.observe(self._scope[0] if self._scope else "", reason)
        if widened is not None:
            self._settle = widened
            log.info("settle widened after repeated timeouts -> %s", widened.to_wire())
            await self._session.configure(
                self._settle, self._scope, self._shot_defaults, self._redaction)

        obs = await self._reobserve()
        self._audit.record(selector, action, from_version, sv, reason)
        return (f"{action.action} on {selector.describe()} -> settled({reason}) "
                f"v{obs.version}; {len(obs.snapshot.nodes)} nodes now")

    async def _reobserve(self) -> Observation:
        return await self._observe_and_follow()
