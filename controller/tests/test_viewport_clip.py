"""Off-screen nodes are hidden from the ref vocabulary (annotate).

A scrollable list reports rows far outside the viewport; clicking a recycled
off-screen row lands on a visible sibling, so those rows must not be offered as
tappable refs. tree_text drops them (noting the count so the agent scrolls) and
set_of_marks draws no marker for them.
"""
import io

from PIL import Image

from a11y.annotate import set_of_marks, tree_text
from a11y.device_session import Observation
from a11y.protocol import Snapshot


def _snap_on_and_off_screen() -> Snapshot:
    return Snapshot.parse({
        "snapshotVersion": 7, "pkg": "com.x", "activity": "com.x/.Main",
        "screen": {"width": 1080, "height": 2340},
        "nodes": [
            {"ref": 1, "cls": "android.widget.TextView", "text": "Onscreen",
             "bounds": [0, 100, 500, 200], "flags": ["clickable", "enabled", "visible"]},
            {"ref": 2, "cls": "android.widget.TextView", "text": "Offscreen",
             "bounds": [0, 5000, 500, 5100], "flags": ["clickable", "enabled", "visible"]},
        ],
    })


def test_tree_text_hides_offscreen_and_notes_count():
    obs = Observation(snapshot=_snap_on_and_off_screen(), image=None)
    tree = tree_text(obs)
    assert "'Onscreen'" in tree
    assert "'Offscreen'" not in tree            # the y=5000 row is dropped
    assert "[1]" in tree and "[2]" not in tree
    assert "nodes=1 on screen" in tree
    assert "+1 off screen" in tree              # agent is told to scroll


def test_set_of_marks_runs_and_skips_offscreen():
    buf = io.BytesIO()
    Image.new("RGB", (540, 1170), "white").save(buf, format="PNG")
    obs = Observation(snapshot=_snap_on_and_off_screen(), image=buf.getvalue())
    png = set_of_marks(obs)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"       # valid PNG, no crash on the off-screen bounds
