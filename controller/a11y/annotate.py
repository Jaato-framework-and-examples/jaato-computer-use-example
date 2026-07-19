"""Set-of-marks annotation (03-DAEMON_DESIGN.md §5).

The tree and the bundled screenshot share one ``snapshotVersion`` (01 §12), so
the daemon can composite a numbered marker at each actionable node's ``bounds``
onto the frame — giving the VLM a pointing vocabulary that maps 1:1 back to the
``ref`` the agent then emits. Drawing lives here, never on-device: it is a
presentation/policy concern.
"""
from __future__ import annotations

import io
from typing import List, Tuple

from PIL import Image, ImageColor, ImageDraw, ImageFont

from .device_session import Observation
from .protocol import Node

_MARK_BG = "#ff2d55"     # high-contrast marker fill
_MARK_FG = "#ffffff"     # label text
_BOX = "#00e0ff"         # node outline


def _font(size: int) -> ImageFont.ImageFont:
    """Load a TrueType font at ``size`` if the platform ships one, else the PIL
    bitmap default. The default has no size control, hence the try."""
    for name in ("DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _visible(bounds: List[int], screen_w: int, screen_h: int) -> bool:
    """True if a node's bounds intersect the visible viewport. Off-screen rows of
    a scrollable list (RecyclerView) are still reported by the a11y tree, but they
    are NOT reliably actionable — performing a click on a recycled off-screen node
    lands on whatever visible sibling now occupies that view — so they must be
    scrolled into view before they can be tapped. Presenting only on-screen nodes
    forces that, and keeps the marker image aligned with the frame."""
    left, top, right, bottom = bounds
    return left < screen_w and right > 0 and top < screen_h and bottom > 0


def set_of_marks(obs: Observation) -> bytes:
    """Return PNG bytes of the observation's frame with a numbered marker drawn
    at every *on-screen* actionable node (§5). Requires ``obs.image`` (a tree-only
    observe has nothing to annotate — the caller must not pass one here).

    The device already downsampled the frame, so bounds (screen px) are scaled
    by ``image_width / screen_width`` before drawing.
    """
    if obs.image is None:
        raise ValueError("set_of_marks requires a bundled screenshot")
    img = Image.open(io.BytesIO(obs.image)).convert("RGB")
    screen_w = obs.snapshot.screen.get("width") or img.width
    scale = img.width / screen_w
    screen_h = int(obs.snapshot.screen.get("height") or (img.height / scale))
    draw = ImageDraw.Draw(img)
    font = _font(max(12, int(img.width / 45)))

    for node in obs.snapshot.actionable_nodes():
        if _visible(node.bounds, screen_w, screen_h):
            _draw_marker(draw, node, scale, font)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _draw_marker(draw: ImageDraw.ImageDraw, node: Node, scale: float,
                 font: ImageFont.ImageFont) -> None:
    left, top, right, bottom = (int(v * scale) for v in node.bounds)
    draw.rectangle((left, top, right, bottom), outline=_BOX, width=2)
    label = str(node.ref)
    tb = draw.textbbox((0, 0), label, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    pad = 3
    # Anchor the badge at the node's top-left, nudged inside the frame.
    bx = max(0, left)
    by = max(0, top)
    draw.rectangle((bx, by, bx + tw + 2 * pad, by + th + 2 * pad), fill=_MARK_BG)
    draw.text((bx + pad, by + pad), label, fill=_MARK_FG, font=font)


def tree_text(obs: Observation) -> str:
    """Render the pruned tree as compact text the model reads alongside the
    marked image: one line per node — ``ref``, id-or-class, label, bounds, flags.
    This is the grounding vocabulary even when no image is available."""
    snap = obs.snapshot
    sw = int(snap.screen.get("width") or 0)
    sh = int(snap.screen.get("height") or 0)
    nodes = snap.nodes
    visible = [n for n in nodes if _visible(n.bounds, sw, sh)] if (sw and sh) else list(nodes)
    hidden = len(nodes) - len(visible)
    header = (f"pkg={snap.pkg} activity={snap.activity} "
              f"v{snap.version} nodes={len(visible)} on screen")
    if hidden:
        header += f" (+{hidden} off screen — scroll to reveal them)"
    lines: List[str] = [header]
    for n in visible:
        ident = n.view_id or n.cls
        flags = _render_flags(n.flags)
        label = (n.text or n.desc or "").replace("\n", " ")
        lines.append(f"[{n.ref}] {ident} {label!r} {n.bounds} <{flags}>")
    return "\n".join(lines)


_SCROLL_DIR_FLAGS = {
    "scrollableDown": "down", "scrollableUp": "up",
    "scrollableLeft": "left", "scrollableRight": "right",
}


def _render_flags(flags: List[str]) -> str:
    """Fold the capability-companion tokens into their parent so the model can read
    an element's affordances at a glance (01-PROTOCOL §8):
    - ``scrollableDown/Up/Left/Right`` -> ``scrollable:down,up`` — tells a vertical
      feed from a horizontal pager, two nodes that otherwise read identically.
    - ``imeEnter`` -> ``editable:submit`` — marks which field can be submitted
      (screen_submit) after typing, instead of the model guessing.
    Other flags pass through unchanged, in order; a device that omits the companion
    tokens just renders a bare ``scrollable`` / ``editable``."""
    dirs = [_SCROLL_DIR_FLAGS[f] for f in flags if f in _SCROLL_DIR_FLAGS]
    has_submit = "imeEnter" in flags
    out: List[str] = []
    for f in flags:
        if f in _SCROLL_DIR_FLAGS or f == "imeEnter":
            continue  # folded into scrollable / editable
        if f == "scrollable" and dirs:
            out.append(f"scrollable:{','.join(dirs)}")
        elif f == "editable" and has_submit:
            out.append("editable:submit")
        else:
            out.append(f)
    return ",".join(out)
