"""Listening to the CAN bus on the OBD port, without sending anything to the car.

Many cars put their own internal traffic (steering angle, brake switch,
wheel speeds, indicators) on the OBD port's CAN pins. Others hide it behind a
gateway, and the port stays silent until a scan tool asks a question. This
module listens with the adapter's monitor mode (ATMA) and keeps what it hears.

An ELM327-class adapter cannot keep up with a busy bus: it fills its buffer
in a fraction of a second, stops with BUFFER FULL, and has to be restarted.
So a capture is a series of short bursts, a sample of the bus rather than
every frame. An STN-class adapter (OBDLink) keeps up far longer. Filtering to
a few message ids (``ids=``) lets even a slow adapter hear those ids at a
useful rate.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .elm import Elm327

_HEX = re.compile(r"^[0-9A-F]+$")
_BYTE = re.compile(r"^[0-9A-F]{2}$")


@dataclass
class Frame:
    mono: float        # time.monotonic() when the line reached this computer
    wall: float        # time.time() at the same moment (UTC seconds)
    id: str            # "025" (11-bit) or "18FEF100" (29-bit)
    data: bytes

    def to_dict(self) -> dict:
        return {"wall": round(self.wall, 3), "id": self.id, "data": self.data.hex().upper()}


def parse_line(line: str) -> tuple[str, bytes] | None:
    """One monitor line with headers on: CAN id, then up to eight data bytes.

    Spaced (ATS1) and compact (ATS0) forms both work: compact 11-bit lines
    have an odd number of hex digits, compact 29-bit lines an even number.
    Anything else (errors, prompts, ISO-TP length lines) is None.
    """
    s = line.strip().upper()
    if not s:
        return None
    tokens = s.split()
    if len(tokens) > 1:
        if len(tokens[0]) == 3 and _HEX.match(tokens[0]):
            ident, rest = tokens[0], tokens[1:]
        elif len(tokens[0]) == 8 and _HEX.match(tokens[0]):
            ident, rest = tokens[0], tokens[1:]
        elif len(tokens) >= 4 and all(_BYTE.match(t) for t in tokens[:4]):
            ident, rest = "".join(tokens[:4]), tokens[4:]
        else:
            return None
        if not all(_BYTE.match(t) for t in rest):
            return None
        data = bytes.fromhex("".join(rest))
    else:
        if not _HEX.match(s) or len(s) < 3:
            return None
        if len(s) % 2:
            ident, data = s[:3], bytes.fromhex(s[3:])
        elif len(s) >= 8:
            ident, data = s[:8], bytes.fromhex(s[8:])
        else:
            return None
    if len(data) > 8:
        return None
    value = int(ident, 16)
    if (len(ident) == 3 and value > 0x7FF) or (len(ident) == 8 and value > 0x1FFFFFFF):
        return None
    return ident, data


def is_diagnostic(ident: str) -> bool:
    """OBD/UDS request and reply ids. They are scan-tool traffic, not the car's own."""
    i = ident.upper()
    if len(i) == 3:
        return i == "7DF" or (i.startswith("7E") and i[2] in "0123456789ABCDEF")
    return i.startswith("18DB") or i.startswith("18DA")


def mask_for(ids: list[str]) -> tuple[str, str]:
    """ATCF filter and ATCM mask that pass every id given (and maybe a few more).

    car-use still drops the extras in software, so the hardware filter only
    has to be a superset.
    """
    clean = [validate_id(i) for i in ids]
    if not clean:
        raise ValueError("no CAN ids given")
    widths = {len(i) for i in clean}
    if len(widths) != 1:
        raise ValueError("mix of 11-bit and 29-bit ids; filter one kind at a time")
    width = widths.pop()
    full = 0x7FF if width == 3 else 0x1FFFFFFF
    values = [int(i, 16) for i in clean]
    mask = full
    for v in values:
        mask &= ~(v ^ values[0]) & full
    return f"{values[0] & mask:0{width}X}", f"{mask:0{width}X}"


def validate_id(ident: str) -> str:
    i = str(ident).strip().upper().replace("0X", "")
    if not _HEX.match(i or "-") or len(i) not in (1, 2, 3, 8):
        raise ValueError(f"CAN id must be up to three hex digits (11-bit) or eight (29-bit), got {ident!r}")
    if len(i) < 3:
        i = i.zfill(3)
    if (len(i) == 3 and int(i, 16) > 0x7FF) or int(i, 16) > 0x1FFFFFFF:
        raise ValueError(f"CAN id out of range: {ident!r}")
    return i


@dataclass
class Capture:
    """Frames heard while listening, and how the listening went."""
    frames: list[Frame] = field(default_factory=list)
    seconds: float = 0.0
    bursts: int = 0
    buffer_full: int = 0
    unsupported: bool = False
    not_can: bool = False
    errors: list[str] = field(default_factory=list)
    ids_filter: list[str] = field(default_factory=list)

    def ids(self, include_diagnostic: bool = False) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.frames:
            if include_diagnostic or not is_diagnostic(f.id):
                out[f.id] = out.get(f.id, 0) + 1
        return dict(sorted(out.items()))

    def car_frames(self) -> list[Frame]:
        return [f for f in self.frames if not is_diagnostic(f.id)]

    def traffic(self) -> tuple[str, str]:
        """open | partial | hidden | not_can | adapter_cannot_listen, with a plain reason."""
        if self.not_can:
            return "not_can", ("This car talks an older protocol (ISO 9141, KWP2000 or J1850) on "
                               "the OBD port, not CAN. There is no broadcast data to hear there; "
                               "standard OBD readings still work.")
        if self.unsupported:
            return "adapter_cannot_listen", (
                "This adapter does not support monitor mode (ATMA), common on clones. "
                "Use an OBDLink (STN chip) or a genuine ELM327 to listen.")
        n = len(self.ids())
        if self.ids_filter:
            return "filtered", (f"Listened to {len(self.ids_filter)} chosen id"
                                f"{'s' if len(self.ids_filter) != 1 else ''} only; {n} heard.")
        if n >= 5 or (self.buffer_full and n):
            return "open", (f"The OBD port carries the car's own CAN traffic: {n} message ids "
                            "heard without asking anything.")
        if self.buffer_full:
            return "open", ("The bus is busy enough to overflow the adapter, but no frame could "
                            "be read back. Try an OBDLink-class adapter.")
        if n:
            return "partial", (f"Only {n} message id{'s' if n != 1 else ''} reached the port. "
                               "Some gateways pass a few through; look for steering and brake "
                               "among them.")
        return "hidden", ("The port stayed silent until asked. A gateway is likely keeping the "
                          "car's internal traffic off the OBD port. Standard OBD readings still "
                          "work, and maker-specific Mode 22 requests may still reach steering "
                          "angle, more slowly.")

    def summary(self) -> dict:
        verdict, why = self.traffic()
        car = self.car_frames()
        return {
            "verdict": verdict,
            "why": why,
            "seconds": round(self.seconds, 2),
            "frames": len(car),
            "frames_per_s": round(len(car) / self.seconds, 1) if self.seconds else None,
            "ids": self.ids(),
            "bursts": self.bursts,
            "buffer_full": self.buffer_full,
            "sampled": self.buffer_full > 0,
            "ids_filter": self.ids_filter or None,
        }

    def __repr__(self) -> str:
        s = self.summary()
        return (f"Capture({s['verdict']}: {s['frames']} frames, {len(s['ids'])} ids in "
                f"{s['seconds']} s, {s['bursts']} bursts, buffer full {s['buffer_full']}x)")


class Listener:
    """Put the adapter in listening shape, take bursts, then put it back.

    Headers on (to see ids), spaces on, CAN auto-formatting off (so broadcast
    frames are not read as ISO-TP) and silent monitoring on where supported.
    On exit, every one of those goes back, and any id filter is cleared, so
    ordinary OBD requests work again.
    """

    def __init__(self, elm: Elm327, ids: list[str] | None = None):
        self.elm = elm
        self.ids = [validate_id(i) for i in ids] if ids else []
        self.capture = Capture(ids_filter=list(self.ids))
        self._filtered = False

    def __enter__(self) -> "Listener":
        if not self.elm.info.is_can:
            self.capture.not_can = True
            return self
        for cmd in ("ATH1", "ATS1", "ATCAF0"):
            self.elm.at_ok(cmd)
        self.elm.at_ok("ATCSM1")          # silent monitoring: never acknowledge frames
        if self.ids:
            filt, mask = mask_for(self.ids)
            self._filtered = self.elm.at_ok("ATCF" + filt) and self.elm.at_ok("ATCM" + mask)
        return self

    def __exit__(self, *exc) -> None:
        if self.capture.not_can:
            return
        if self.ids:
            width = len(self.ids[0])
            self.elm.at_ok("ATCF" + "0" * width)
            self.elm.at_ok("ATCM" + "0" * width)
        for cmd in ("ATCAF1", "ATS0", "ATH0"):
            self.elm.at_ok(cmd)

    def burst(self, seconds: float) -> list[Frame]:
        """One monitor burst, up to `seconds`. Returns the frames it heard."""
        cap = self.capture
        if cap.not_can or cap.unsupported:
            return []
        b = self.elm.monitor(seconds)
        cap.bursts += 1
        cap.seconds += b.seconds
        cap.errors.extend(b.errors[:5])
        if b.ended == "unsupported":
            cap.unsupported = True
            return []
        if b.ended == "buffer_full":
            cap.buffer_full += 1
        wanted = set(self.ids)
        out = []
        for mono, wall, line in b.lines:
            parsed = parse_line(line)
            if parsed is None:
                continue
            ident, data = parsed
            if wanted and ident not in wanted:
                continue
            out.append(Frame(mono, wall, ident, data))
        cap.frames.extend(out)
        return out

    def listen(self, seconds: float) -> Capture:
        """Bursts until `seconds` have passed (restarting after BUFFER FULL)."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            got = self.burst(left)
            if self.capture.unsupported or self.capture.not_can:
                break
            if not got and left > 0.05:
                time.sleep(min(0.02, left))
        return self.capture


def listen(elm: Elm327, seconds: float = 3.0, ids: list[str] | None = None) -> Capture:
    """Listen for `seconds` and give the adapter back in its usual shape."""
    with Listener(elm, ids) as lst:
        return lst.listen(seconds)
