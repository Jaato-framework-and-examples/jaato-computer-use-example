"""Binary WebSocket frame parsing (01-PROTOCOL.md §4).

The device sends screenshots as self-describing binary frames:

    ┌────────────┬───────────────────────────┬────────────────┐
    │  4 bytes   │      headerLen bytes      │  payload bytes │
    │ headerLen  │    UTF-8 JSON header      │    raw blob    │
    │ (BE uint32)│                           │                │
    └────────────┴───────────────────────────┴────────────────┘

Only parsing is needed daemon-side — the device is the only producer of binary
frames. The header ties the blob to a request via ``correlationId`` and to a
visual frame via ``snapshotVersion`` (§4, §8).
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass


@dataclass
class BinaryFrame:
    """A decoded binary frame: its JSON header plus the raw payload bytes.

    ``header`` carries at least ``type``, ``correlationId``, ``snapshotVersion``,
    ``format``, ``width``, ``height`` and ``reason`` (``on_demand`` | ``bundled``)
    for a screenshot. ``payload`` is the encoded image (png/webp/jpeg per the
    ``format`` the daemon requested).
    """

    header: dict
    payload: bytes

    @property
    def snapshot_version(self) -> int | None:
        v = self.header.get("snapshotVersion")
        return int(v) if v is not None else None

    @property
    def correlation_id(self) -> str | None:
        return self.header.get("correlationId")


def parse_binary_frame(data: bytes) -> BinaryFrame:
    """Decode one binary WebSocket frame (§4).

    Raises ``ValueError`` on a truncated frame — a malformed frame is a hard
    protocol error, not something to paper over.
    """
    if len(data) < 4:
        raise ValueError(f"binary frame too short: {len(data)} bytes")
    (header_len,) = struct.unpack(">I", data[:4])
    end = 4 + header_len
    if len(data) < end:
        raise ValueError(
            f"binary frame header truncated: need {end} bytes, have {len(data)}")
    header = json.loads(data[4:end].decode("utf-8"))
    payload = data[end:]
    return BinaryFrame(header=header, payload=payload)
