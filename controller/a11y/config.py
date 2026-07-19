"""Load the device-facing bridge config (``.jaato/a11y-bridge.yaml``).

This is client-side config (listener host/port/token, package scope, screenshot
and redaction policy, loop bounds) — distinct from the jaato *profile* that
selects the LLM provider/model. Validation is fail-loud: a missing auth choice
or an empty package scope is an error, never a silent permissive default.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class BridgeConfig:
    host: str
    port: int
    path: str
    token: Optional[str]
    unsafe_no_auth: bool
    package_scope: List[str]
    screenshot_defaults: dict
    redaction: dict
    max_steps: int
    settle_ceiling_s: float
    max_idle_turns: int

    @property
    def listen_url(self) -> str:
        shown = self.host if self.host != "0.0.0.0" else "<this-host>"
        return f"ws://{shown}:{self.port}{self.path}"


def _resolve_token(listen: dict) -> tuple[Optional[str], bool]:
    """Resolve the bearer token from the config, fail-loud on an unset auth
    choice. Exactly one of token / token_env / unsafe_no_auth must be provided."""
    unsafe = bool(listen.get("unsafe_no_auth", False))
    token = listen.get("token")
    token_env = listen.get("token_env")
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise ValueError(
                f"listen.token_env={token_env!r} is set but that env var is empty")
    chosen = [x for x in (token, unsafe) if x]
    if not chosen:
        raise ValueError(
            "listen auth unset: provide one of token / token_env / unsafe_no_auth")
    if token and unsafe:
        raise ValueError("listen: token and unsafe_no_auth are mutually exclusive")
    return token, unsafe


def load(workspace: str, scope_override: Optional[List[str]] = None) -> BridgeConfig:
    """Load + validate the config from ``<workspace>/.jaato/a11y-bridge.yaml``.

    ``scope_override`` (from the CLI) replaces the file's ``device.package_scope``.
    A non-empty scope PINS authority to those packages; an empty scope selects
    follow-the-foreground, where the controller learns the on-screen package
    after connect and auto-re-scopes (01 §13.3).
    """
    cfg_path = Path(workspace) / ".jaato" / "a11y-bridge.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"missing bridge config: {cfg_path}")
    raw = yaml.safe_load(cfg_path.read_text()) or {}

    listen = raw.get("listen", {})
    token, unsafe = _resolve_token(listen)

    device = raw.get("device", {})
    scope = scope_override if scope_override is not None else list(device.get("package_scope", []))
    # An empty scope is valid: the controller runs follow-the-foreground — it
    # learns the on-screen package after connect and auto-re-scopes as the
    # foreground app changes. A non-empty scope PINS authority to those
    # packages (no auto-follow). 01 §13.3.

    shot = raw.get("screenshot", {})
    screenshot_defaults = {
        "format": shot.get("format", "png"),
        "quality": int(shot.get("quality", 80)),
        "maxDimension": int(shot.get("max_dimension", 1280)),
        "crop": shot.get("crop"),
    }
    red = raw.get("redaction", {})
    redaction = {
        "maskPasswordNodes": bool(red.get("mask_password_nodes", True)),
        "extraMaskSelectors": list(red.get("extra_mask_selectors", [])),
    }
    loop = raw.get("loop", {})
    return BridgeConfig(
        host=listen.get("host", "0.0.0.0"),
        port=int(listen.get("port", 8765)),
        path=listen.get("path", "/a11y"),
        token=token,
        unsafe_no_auth=unsafe,
        package_scope=scope,
        screenshot_defaults=screenshot_defaults,
        redaction=redaction,
        max_steps=int(loop.get("max_steps", 40)),
        settle_ceiling_s=float(loop.get("settle_ceiling_s", 12)),
        max_idle_turns=int(loop.get("max_idle_turns", 2)),
    )
