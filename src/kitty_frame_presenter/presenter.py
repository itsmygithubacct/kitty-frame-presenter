"""Core Kitty frame presenter.

Only standard-library modules are used.  The local transport is Linux/POSIX
shared memory (``t=s``); inline ``t=d,o=z`` remains available for sessions in
which the terminal cannot open a local shared-memory object.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import errno
import math
import mmap
import os
import secrets
import socket
import struct
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

CHUNK = 4096
FRAME_BYTES = 3
_SCAN_PIXELS = 32
_SYNC_BEGIN = "\x1b[?2026h"
_SYNC_END = "\x1b[?2026l"
_UINT32_MAX = (1 << 32) - 1
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1
_MAX_SHM_SLOTS = 64
_MAX_DAMAGE_RECTS = 64
_SHM_RETRY_SECONDS = 0.005
_UNIX_SOCKET_PATH_BYTES = 107
_MAX_TAP_RETRY_SECONDS = 3600.0
_MAX_TAP_SEND_TIMEOUT = 60.0
_SESSION_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)

Rect = tuple[int, int, int, int]


class TerminalWriter(Protocol):
    def write(self, value: str) -> object: ...


def _integer(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = _UINT32_MAX,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def _number(
    value: object,
    label: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(cast(Any, value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    if maximum is not None and result > maximum:
        raise ValueError(f"{label} must not exceed {maximum:g}")
    return max(minimum, result)


def _time_value(value: object, label: str = "frame time") -> float:
    result = _number(value, label, minimum=float("-inf"))
    if result < 0:
        raise ValueError(f"{label} must not be negative")
    if result * 1_000_000 >= 1 << 64:
        raise ValueError(f"{label} is outside the supported range")
    return result


def _rectangle(value: object, label: str = "damage rectangle") -> Rect:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 4
    ):
        raise ValueError(f"{label} must contain x, y, width, and height")
    x = _integer(value[0], f"{label} x")
    y = _integer(value[1], f"{label} y")
    width = _integer(value[2], f"{label} width", minimum=1)
    height = _integer(value[3], f"{label} height", minimum=1)
    if x + width > _UINT32_MAX or y + height > _UINT32_MAX:
        raise ValueError(f"{label} exceeds the protocol coordinate range")
    return x, y, width, height


def _scroll_hint(value: object, label: str = "scroll hint") -> tuple[int, int]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 2
    ):
        raise ValueError(f"{label} must contain integer x and y offsets")
    return (
        _integer(value[0], f"{label} x", minimum=_INT32_MIN, maximum=_INT32_MAX),
        _integer(value[1], f"{label} y", minimum=_INT32_MIN, maximum=_INT32_MAX),
    )


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    try:
        return memoryview(cast(Any, value)).tobytes()
    except (TypeError, ValueError, OverflowError, BufferError) as exc:
        raise ValueError("RGB frame must be bytes-like") from exc


def _valid_frames(
    prev: object, cur: object, width: int, height: int
) -> tuple[bytes, bytes]:
    width = _integer(width, "frame width", minimum=1)
    height = _integer(height, "frame height", minimum=1)
    expected = width * height * FRAME_BYTES
    p, c = _as_bytes(prev), _as_bytes(cur)
    if len(p) != expected or len(c) != expected:
        raise ValueError(
            f"RGB frame length must be {expected} bytes for {width}x{height}"
        )
    return p, c


def _row_bounds(p: bytes, c: bytes, offset: int, width: int) -> tuple[int, int] | None:
    """Return the first/last changed pixel columns for one RGB row."""
    stride = width * FRAME_BYTES
    if p[offset : offset + stride] == c[offset : offset + stride]:
        return None
    step = _SCAN_PIXELS * FRAME_BYTES
    left_block = 0
    while left_block < stride:
        end = min(stride, left_block + step)
        if (
            p[offset + left_block : offset + end]
            != c[offset + left_block : offset + end]
        ):
            break
        left_block = end
    left = left_block // FRAME_BYTES
    for x in range(left, min(width, left + _SCAN_PIXELS)):
        at = offset + x * FRAME_BYTES
        if p[at : at + FRAME_BYTES] != c[at : at + FRAME_BYTES]:
            left = x
            break

    right_block = stride
    while right_block > 0:
        start = max(0, right_block - step)
        if (
            p[offset + start : offset + right_block]
            != c[offset + start : offset + right_block]
        ):
            break
        right_block = start
    right = min(width, (right_block + FRAME_BYTES - 1) // FRAME_BYTES)
    for x in range(right - 1, max(left - 1, right - _SCAN_PIXELS - 1), -1):
        at = offset + x * FRAME_BYTES
        if p[at : at + FRAME_BYTES] != c[at : at + FRAME_BYTES]:
            right = x + 1
            break
    return left, right


def diff_rect(prev: object, cur: object, width: int, height: int) -> Rect | None:
    """Return the exact bounding rectangle changed between two RGB frames."""
    p, c = _valid_frames(prev, cur, width, height)
    if prev is cur or p == c:
        return None
    stride = width * FRAME_BYTES
    top = 0
    while p[top * stride : (top + 1) * stride] == c[top * stride : (top + 1) * stride]:
        top += 1
    bottom = height
    while (
        bottom > top + 1
        and p[(bottom - 1) * stride : bottom * stride]
        == c[(bottom - 1) * stride : bottom * stride]
    ):
        bottom -= 1
    left, right = width, 0
    for y in range(top, bottom):
        offset = y * stride
        if right > left:
            # A row can only widen the established span by differing in the
            # margins outside it, so compare just those two slices before
            # paying for a full column scan.
            lb = left * FRAME_BYTES
            rb = right * FRAME_BYTES
            if (left <= 0 or p[offset : offset + lb] == c[offset : offset + lb]) and (
                right >= width
                or p[offset + rb : offset + stride] == c[offset + rb : offset + stride]
            ):
                continue
        bounds = _row_bounds(p, c, offset, width)
        if bounds is None:
            continue
        left = min(left, bounds[0])
        right = max(right, bounds[1])
        if left == 0 and right == width:
            break
    return left, top, right - left, bottom - top


def diff_band(
    prev: object, cur: object, width: int, height: int
) -> tuple[int, int] | None:
    """Compatibility helper returning only the changed row band."""
    try:
        rect = diff_rect(prev, cur, width, height)
    except ValueError:
        return 0, height
    return None if rect is None else (rect[1], rect[3])


def diff_rects(
    prev: object,
    cur: object,
    width: int,
    height: int,
    max_rects: int = 2,
) -> tuple[Rect, ...]:
    """Return up to ``max_rects`` exact rectangles split at clean row gaps.

    Scroll composition commonly leaves two disjoint regions: fixed chrome at
    one edge and a newly exposed strip at the other.  Keeping those separate
    avoids turning their bounding box back into an almost-full frame.
    """
    max_rects = _integer(max_rects, "max_rects", minimum=1)
    p, c = _valid_frames(prev, cur, width, height)
    if prev is cur or p == c:
        return ()
    stride = width * FRAME_BYTES
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for y in range(height):
        changed = p[y * stride : (y + 1) * stride] != c[y * stride : (y + 1) * stride]
        if changed and start is None:
            start = y
        elif not changed and start is not None:
            bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, height))

    if len(bands) > max_rects:
        # Repeatedly splicing the smallest gap is quadratic for alternating
        # rows.  The gaps never change as their neighbors merge, so the same
        # result is obtained by retaining the largest max_rects - 1 gaps once.
        gaps = (
            (bands[index + 1][0] - bands[index][1], index)
            for index in range(len(bands) - 1)
        )
        split_after = {
            index for _gap, index in sorted(gaps, reverse=True)[: max_rects - 1]
        }
        merged: list[tuple[int, int]] = []
        top = bands[0][0]
        for index, (_start, bottom) in enumerate(bands):
            if index in split_after:
                merged.append((top, bottom))
                top = bands[index + 1][0]
        merged.append((top, bands[-1][1]))
        bands = merged

    out: list[Rect] = []
    for top, bottom in bands:
        left, right = width, 0
        for y in range(top, bottom):
            offset = y * stride
            if right > left:
                # Same margin shortcut as diff_rect: only the slices outside
                # the established span can change this band's rectangle.
                lb = left * FRAME_BYTES
                rb = right * FRAME_BYTES
                if (
                    left <= 0 or p[offset : offset + lb] == c[offset : offset + lb]
                ) and (
                    right >= width
                    or p[offset + rb : offset + stride]
                    == c[offset + rb : offset + stride]
                ):
                    continue
            bounds = _row_bounds(p, c, offset, width)
            if bounds:
                left, right = min(left, bounds[0]), max(right, bounds[1])
                if left == 0 and right == width:
                    break
        out.append((left, top, right - left, bottom - top))
    return tuple(out)


def _damage_hint(value: object, width: int, height: int) -> tuple[Rect, ...] | None:
    """Validate a producer-supplied damage superset for one complete frame."""
    if value is None:
        return None
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 4
        and all(type(part) is int for part in value)
    ):
        values: Iterable[object] = (value,)
    else:
        try:
            values = iter(cast(Iterable[object], value))
        except TypeError as exc:
            raise ValueError(
                "damage must be one rectangle or an iterable of rectangles"
            ) from exc

    out: list[Rect] = []
    for index, item in enumerate(values):
        if index >= _MAX_DAMAGE_RECTS:
            raise ValueError(
                f"damage must contain at most {_MAX_DAMAGE_RECTS} rectangles"
            )
        rect = _rectangle(item, f"damage rectangle {index + 1}")
        x, y, rect_width, rect_height = rect
        if x + rect_width > width or y + rect_height > height:
            raise ValueError(f"damage rectangle {index + 1} is outside the frame")
        out.append(rect)
    return tuple(out)


def _rect_union(first: Rect, second: Rect) -> Rect:
    left = min(first[0], second[0])
    top = min(first[1], second[1])
    right = max(first[0] + first[2], second[0] + second[2])
    bottom = max(first[1] + first[3], second[1] + second[3])
    return left, top, right - left, bottom - top


def _rects_touch(first: Rect, second: Rect) -> bool:
    return not (
        first[0] + first[2] < second[0]
        or second[0] + second[2] < first[0]
        or first[1] + first[3] < second[1]
        or second[1] + second[3] < first[1]
    )


def _coalesce_rects(rects: Iterable[Rect], max_rects: int) -> tuple[Rect, ...]:
    """Merge touching rectangles, then make the least-cost merges to a limit."""
    limit = _integer(max_rects, "max_rects", minimum=1)
    merged: list[Rect] = []
    for rect in rects:
        candidate = rect
        index = 0
        while index < len(merged):
            if _rects_touch(candidate, merged[index]):
                candidate = _rect_union(candidate, merged.pop(index))
                index = 0
            else:
                index += 1
        merged.append(candidate)

    while len(merged) > limit:
        best: tuple[int, int, int, Rect] | None = None
        for first_index in range(len(merged) - 1):
            first = merged[first_index]
            first_area = first[2] * first[3]
            for second_index in range(first_index + 1, len(merged)):
                second = merged[second_index]
                union = _rect_union(first, second)
                extra = (
                    union[2] * union[3]
                    - first_area
                    - second[2] * second[3]
                )
                choice = (extra, first_index, second_index, union)
                if best is None or choice[:3] < best[:3]:
                    best = choice
        if best is None:
            break
        _extra, first_index, second_index, union = best
        merged[first_index] = union
        merged.pop(second_index)
    return tuple(sorted(merged, key=lambda rect: (rect[1], rect[0], rect[3], rect[2])))


def diff_damage_rects(
    prev: object,
    cur: object,
    width: int,
    height: int,
    damage: object,
    max_rects: int = 8,
) -> tuple[Rect, ...]:
    """Return exact changes while scanning only a trusted damage superset.

    Producers must include every changed pixel in ``damage``.  An empty
    iterable explicitly means that the complete frame is unchanged; ``None``
    is rejected here because callers that lack metadata should use
    :func:`diff_rect` instead.
    """
    p, c = _valid_frames(prev, cur, width, height)
    hints = _damage_hint(damage, width, height)
    if hints is None:
        raise ValueError("damage must not be None")
    max_rects = _integer(max_rects, "max_rects", minimum=1)
    if prev is cur or not hints:
        return ()
    stride = width * FRAME_BYTES
    changed: list[Rect] = []
    for x, y, rect_width, rect_height in hints:
        top: int | None = None
        bottom = y
        left, right = x + rect_width, x
        row_bytes = rect_width * FRAME_BYTES
        for row in range(y, y + rect_height):
            offset = row * stride + x * FRAME_BYTES
            if right > left:
                # Margin shortcut, adapted for this loop's row-band tracking:
                # clean margins mean the span cannot widen, but the interior
                # must still be compared so top and bottom stay exact.
                lb = (left - x) * FRAME_BYTES
                rb = (right - x) * FRAME_BYTES
                if (
                    left <= x or p[offset : offset + lb] == c[offset : offset + lb]
                ) and (
                    right >= x + rect_width
                    or p[offset + rb : offset + row_bytes]
                    == c[offset + rb : offset + row_bytes]
                ):
                    if p[offset + lb : offset + rb] != c[offset + lb : offset + rb]:
                        if top is None:
                            top = row
                        bottom = row + 1
                    continue
            bounds = _row_bounds(p, c, offset, rect_width)
            if bounds is None:
                continue
            if top is None:
                top = row
            bottom = row + 1
            left = min(left, x + bounds[0])
            right = max(right, x + bounds[1])
        if top is not None:
            changed.append((left, top, right - left, bottom - top))
    return _coalesce_rects(changed, max_rects)


def extract_rect(rgb: object, width: int, height: int, rect: Rect) -> bytes:
    """Copy one rectangular region from a tightly packed RGB frame."""
    frame = _as_bytes(rgb)
    width = _integer(width, "frame width", minimum=1)
    height = _integer(height, "frame height", minimum=1)
    expected = width * height * FRAME_BYTES
    if len(frame) != expected:
        raise ValueError(f"RGB frame length must be {expected}")
    x, y, rw, rh = _rectangle(rect)
    if x + rw > width or y + rh > height:
        raise ValueError("damage rectangle is outside the frame")
    if x == 0 and rw == width:
        stride = width * FRAME_BYTES
        return frame[y * stride : (y + rh) * stride]
    stride = width * FRAME_BYTES
    row_bytes = rw * FRAME_BYTES
    out = bytearray(row_bytes * rh)
    for row in range(rh):
        source = (y + row) * stride + x * FRAME_BYTES
        target = row * row_bytes
        out[target : target + row_bytes] = frame[source : source + row_bytes]
    return bytes(out)


def detect_vertical_scroll(
    prev: object,
    cur: object,
    width: int,
    height: int,
    max_shift: int | None = None,
) -> tuple[int, int] | None:
    """Infer vertical screen motion as ``(0, dy)`` from matching RGB rows.

    ``dy < 0`` means old pixels moved upward.  Candidates are obtained from a
    small set of row checksums and then verified over sampled overlap rows.
    The final presenter still simulates the compose and diffs it against the
    real frame, so an imperfect hint can cost bandwidth but cannot corrupt the
    displayed image.
    """
    p, c = _valid_frames(prev, cur, width, height)
    if p == c or height < 8:
        return None
    stride = width * FRAME_BYTES
    if max_shift is None:
        limit = max(1, height // 3)
    else:
        limit = _integer(max_shift, "max_shift")
        if limit == 0:
            return None
    limit = min(limit, height - 2)
    old_rows: dict[int, list[int]] = {}
    for y in range(height):
        checksum = zlib.crc32(memoryview(p)[y * stride : (y + 1) * stride])
        old_rows.setdefault(checksum, []).append(y)
    votes: dict[int, int] = {}
    sample_count = min(17, height)
    for index in range(sample_count):
        y = index * (height - 1) // max(1, sample_count - 1)
        checksum = zlib.crc32(memoryview(c)[y * stride : (y + 1) * stride])
        for old_y in old_rows.get(checksum, ()):
            dy = y - old_y
            if dy and abs(dy) <= limit:
                votes[dy] = votes.get(dy, 0) + 1
    for dy, _ in sorted(votes.items(), key=lambda item: item[1], reverse=True)[:6]:
        src_y = max(0, -dy)
        dst_y = max(0, dy)
        overlap = height - abs(dy)
        checks = min(25, overlap)
        matches = 0
        for index in range(checks):
            row = index * (overlap - 1) // max(1, checks - 1)
            po = (src_y + row) * stride
            co = (dst_y + row) * stride
            matches += p[po : po + stride] == c[co : co + stride]
        if checks and matches / checks >= 0.60:
            return 0, dy
    return None


def _shift_prediction(
    prev: bytes, width: int, height: int, dx: int, dy: int
) -> tuple[bytes, Rect, Rect]:
    """Simulate an overlapping replacement compose on a root frame.

    Returns predicted pixels, source rectangle and destination rectangle.
    """
    if not dx and not dy:
        raise ValueError("zero scroll shift")
    copy_w, copy_h = width - abs(dx), height - abs(dy)
    if copy_w <= 0 or copy_h <= 0:
        raise ValueError("scroll shift is outside the frame")
    src_x, src_y = max(0, -dx), max(0, -dy)
    dst_x, dst_y = max(0, dx), max(0, dy)
    source = (src_x, src_y, copy_w, copy_h)
    dest = (dst_x, dst_y, copy_w, copy_h)
    result = bytearray(prev)
    stride = width * FRAME_BYTES
    row_bytes = copy_w * FRAME_BYTES
    # ``prev`` is immutable and separate from ``result``, so reading directly
    # from it is overlap-safe and avoids a second near-frame-sized snapshot.
    if src_x == 0 and copy_w == width:
        source_at = src_y * stride
        destination_at = dst_y * stride
        copy_bytes = copy_h * stride
        result[destination_at : destination_at + copy_bytes] = prev[
            source_at : source_at + copy_bytes
        ]
    else:
        for row in range(copy_h):
            source_at = (src_y + row) * stride + src_x * FRAME_BYTES
            destination_at = (dst_y + row) * stride + dst_x * FRAME_BYTES
            result[destination_at : destination_at + row_bytes] = prev[
                source_at : source_at + row_bytes
            ]
    return bytes(result), source, dest


def _tmux_wrap(apc: str) -> str:
    return "\x1bPtmux;" + apc.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def wrap_tmux_passthrough(apc: str) -> str:
    """Wrap one control string in tmux's DCS passthrough envelope."""
    return _tmux_wrap(apc)


def _apc(control: str, payload: str = "", in_tmux: bool = False) -> str:
    value = f"\x1b_G{control};{payload}\x1b\\"
    return _tmux_wrap(value) if in_tmux else value


def _compressed_chunks(data: bytes) -> Iterable[tuple[str, int]]:
    """Yield protocol-sized base64 chunks without retaining a second payload.

    Three raw bytes encode to four base64 characters, so 3072-byte boundaries
    preserve the exact result of encoding the complete compressed stream while
    avoiding an additional full-size base64 ``bytes`` and ``str`` pair.
    """
    compressed = zlib.compress(data, 1)
    raw_chunk = CHUNK // 4 * 3
    for offset in range(0, len(compressed), raw_chunk):
        payload = base64.b64encode(compressed[offset : offset + raw_chunk]).decode(
            "ascii"
        )
        yield payload, int(offset + raw_chunk < len(compressed))


def _image_id(value: object) -> int:
    return _integer(value, "image_id", minimum=1)


def _placement_geometry(
    width: object,
    height: object,
    columns: object,
    rows: object,
    origin_row: object = 1,
    origin_column: object = 1,
) -> tuple[int, int, int, int, int, int]:
    return (
        _integer(width, "frame width", minimum=1),
        _integer(height, "frame height", minimum=1),
        _integer(columns, "placement columns", minimum=1),
        _integer(rows, "placement rows", minimum=1),
        _integer(origin_row, "origin row", minimum=1),
        _integer(origin_column, "origin column", minimum=1),
    )


def _shared_memory_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not name.startswith("/")
        or "/" in name[1:]
        or "\0" in name
    ):
        raise ValueError("shared-memory name must have one leading slash")
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("shared-memory name must be ASCII") from exc
    if len(encoded) > 255:
        raise ValueError("shared-memory name is too long")
    return name


def build_direct(
    rgb: bytes,
    width: int,
    height: int,
    columns: int,
    rows: int,
    image_id: int,
    origin_row: int = 1,
    origin_column: int = 1,
    in_tmux: bool = False,
) -> str:
    """Build a compressed, chunked inline full-frame placement."""
    if not rgb:
        return ""
    width, height, columns, rows, origin_row, origin_column = _placement_geometry(
        width, height, columns, rows, origin_row, origin_column
    )
    image_id = _image_id(image_id)
    data = _as_bytes(rgb)
    expected = width * height * FRAME_BYTES
    if len(data) != expected:
        raise ValueError(f"RGB frame length must be {expected}")
    out = [f"\x1b[{origin_row};{origin_column}H"]
    for index, (payload, more) in enumerate(_compressed_chunks(data)):
        if index == 0:
            control = (
                f"a=T,i={image_id},p=1,z=-1,t=d,f=24,o=z,N=1,"
                f"s={width},v={height},c={columns},r={rows},q=2,C=1,m={more}"
            )
        else:
            control = f"m={more}"
        out.append(_apc(control, payload, in_tmux))
    return "".join(out)


def build_frame_edit(
    rgb: bytes,
    width: int,
    height: int,
    x: int,
    y: int,
    image_id: int,
    in_tmux: bool = False,
) -> str:
    """Build a compressed, chunked inline root-frame edit."""
    if not rgb:
        return ""
    x, y, width, height = _rectangle((x, y, width, height), "frame-edit rectangle")
    image_id = _image_id(image_id)
    data = _as_bytes(rgb)
    expected = width * height * FRAME_BYTES
    if len(data) != expected:
        raise ValueError(f"RGB frame length must be {expected}")
    out: list[str] = []
    for index, (payload, more) in enumerate(_compressed_chunks(data)):
        if index == 0:
            control = (
                f"a=f,i={image_id},r=1,x={x},y={y},t=d,f=24,o=z,N=1,"
                f"s={width},v={height},q=2,m={more}"
            )
        else:
            control = f"a=f,i={image_id},r=1,q=2,m={more}"
        out.append(_apc(control, payload, in_tmux))
    return "".join(out)


def build_full_shm(
    name: str,
    width: int,
    height: int,
    columns: int,
    rows: int,
    image_id: int,
    origin_row: int = 1,
    origin_column: int = 1,
) -> str:
    width, height, columns, rows, origin_row, origin_column = _placement_geometry(
        width, height, columns, rows, origin_row, origin_column
    )
    image_id = _image_id(image_id)
    name = _shared_memory_name(name)
    payload = base64.b64encode(name.encode("ascii")).decode("ascii")
    return f"\x1b[{origin_row};{origin_column}H" + _apc(
        f"a=T,i={image_id},p=1,z=-1,t=s,f=24,N=1,s={width},v={height},"
        f"c={columns},r={rows},q=2,C=1",
        payload,
    )


def build_frame_edit_shm(
    name: str, width: int, height: int, x: int, y: int, image_id: int
) -> str:
    name = _shared_memory_name(name)
    x, y, width, height = _rectangle((x, y, width, height), "frame-edit rectangle")
    image_id = _image_id(image_id)
    payload = base64.b64encode(name.encode("ascii")).decode("ascii")
    return _apc(
        f"a=f,i={image_id},r=1,x={x},y={y},t=s,f=24,N=1,s={width},v={height},q=2",
        payload,
    )


def build_compose(
    image_id: int, source: Rect, destination: Rect, in_tmux: bool = False
) -> str:
    """Build the Kilix-fork overlapping root-frame replacement command."""
    image_id = _image_id(image_id)
    sx, sy, sw, sh = _rectangle(source, "compose source")
    dx, dy, dw, dh = _rectangle(destination, "compose destination")
    if (sw, sh) != (dw, dh):
        raise ValueError("compose source and destination sizes differ")
    # N=2 opts into the fork's safe same-frame-overlap extension.  Stock Kitty
    # retains the protocol-mandated EINVAL behavior for overlap.
    return _apc(
        f"a=c,i={image_id},r=1,c=1,x={dx},y={dy},X={sx},Y={sy},"
        f"w={sw},h={sh},C=1,N=2,q=2",
        "",
        in_tmux,
    )


class ShmBusy(RuntimeError):
    """Raised when every bounded shared-memory slot is still in flight."""


@dataclass
class _ShmSlot:
    name: str
    fd: int = -1
    mapping: mmap.mmap | None = None
    busy: bool = False


class PosixShmRing:
    """A bounded, unlink-acknowledged POSIX shared-memory transport.

    Kitty unlinks a ``t=s`` object immediately after opening it.  An absent
    name is therefore a consumption acknowledgement.  Reusing that *name*
    creates a new object; Kitty's mapping of the old unlinked object remains
    valid, so a fast producer cannot tear an older frame.
    """

    def __init__(self, slots: int = 3, prefix: str = "kitty-frame-presenter"):
        slots = _integer(slots, "slots", minimum=1, maximum=_MAX_SHM_SLOTS)
        if not isinstance(prefix, str):
            raise ValueError("shared-memory prefix must be a string")
        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            raise OSError(errno.ENOSYS, "C library not found")
        self._libc = ctypes.CDLL(libc_name, use_errno=True)
        self._libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
        self._libc.shm_open.restype = ctypes.c_int
        self._libc.shm_unlink.argtypes = [ctypes.c_char_p]
        self._libc.shm_unlink.restype = ctypes.c_int
        token = secrets.token_hex(6)
        base = "".join(
            ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "-"
            for ch in prefix
        ).strip("-")
        base = (base or "kitty-frame-presenter")[:192]
        self._slots = [
            _ShmSlot(f"/{base}-{os.getpid()}-{token}-{i}") for i in range(slots)
        ]
        self.closed = False

    def _open(self, name: str, flags: int, mode: int = 0o600) -> int:
        fd = self._libc.shm_open(name.encode("ascii"), flags, mode)
        if fd < 0:
            value = ctypes.get_errno()
            raise OSError(value, os.strerror(value), name)
        return int(fd)

    def _unlink(self, name: str, missing_ok: bool = True) -> None:
        if self._libc.shm_unlink(name.encode("ascii")) != 0:
            value = ctypes.get_errno()
            if not (missing_ok and value == errno.ENOENT):
                raise OSError(value, os.strerror(value), name)

    @staticmethod
    def _release(slot: _ShmSlot) -> None:
        mapping, descriptor = slot.mapping, slot.fd
        slot.mapping, slot.fd, slot.busy = None, -1, False
        error: Exception | None = None
        if mapping is not None:
            try:
                mapping.close()
            except (BufferError, OSError) as exc:
                error = exc
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    def reap(self) -> int:
        """Release slots whose names Kitty has unlinked; return free count."""
        if self.closed:
            return 0
        for slot in self._slots:
            if not slot.busy:
                continue
            try:
                probe = self._open(slot.name, os.O_RDONLY)
            except OSError as error:
                if error.errno == errno.ENOENT:
                    self._release(slot)
                else:
                    raise
            else:
                os.close(probe)
        return sum(not slot.busy for slot in self._slots)

    @property
    def capacity(self) -> int:
        return len(self._slots)

    @property
    def in_flight(self) -> int:
        self.reap()
        return sum(slot.busy for slot in self._slots)

    def put_many(self, payloads: Sequence[bytes]) -> tuple[str, ...]:
        if self.closed:
            raise RuntimeError("shared-memory ring is closed")
        if not payloads:
            return ()
        self.reap()
        free = [slot for slot in self._slots if not slot.busy]
        if len(free) < len(payloads):
            raise ShmBusy("shared-memory ring is saturated")
        made: list[_ShmSlot] = []
        try:
            for slot, payload in zip(free, payloads):
                data = _as_bytes(payload)
                if not data:
                    raise ValueError("shared-memory payload cannot be empty")
                fd = -1
                mapping: mmap.mmap | None = None
                created = False
                try:
                    fd = self._open(slot.name, os.O_RDWR | os.O_CREAT | os.O_EXCL)
                    created = True
                    os.ftruncate(fd, len(data))
                    mapping = mmap.mmap(fd, len(data), access=mmap.ACCESS_WRITE)
                    mapping[:] = data
                except BaseException:
                    if mapping is not None:
                        try:
                            mapping.close()
                        except (BufferError, OSError):
                            pass
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    # Never unlink an object whose O_EXCL open failed: that
                    # name belongs to another producer.
                    if created:
                        try:
                            self._unlink(slot.name)
                        except OSError:
                            pass
                    raise
                slot.fd, slot.mapping, slot.busy = fd, mapping, True
                made.append(slot)
            return tuple(slot.name for slot in made)
        except BaseException:
            for slot in made:
                try:
                    self._unlink(slot.name)
                except OSError:
                    pass
                try:
                    self._release(slot)
                except (BufferError, OSError):
                    pass
            raise

    def revoke(self, names: Iterable[str]) -> None:
        """Roll back slots whose command never reached the terminal.

        The caller guarantees the escape sequence naming these objects was
        not delivered, so no consumer can be waiting on them: unlink the
        objects and free the slots so the ring recovers its capacity.
        """
        if self.closed:
            return
        wanted = set(names)
        for slot in self._slots:
            if slot.busy and slot.name in wanted:
                self._unlink(slot.name)
                self._release(slot)

    def close(self, *, discard: bool = False) -> None:
        """Release local handles without invalidating unread payloads.

        Kitty owns the normal unlink acknowledgement.  Keeping an in-flight
        name present after close lets a terminal that has received, but not yet
        processed, the command still open the payload.  ``discard=True`` is
        the explicit abort path for output known never to reach a terminal.
        """
        if self.closed:
            return
        error: Exception | None = None
        for slot in self._slots:
            if slot.busy:
                if discard:
                    try:
                        self._unlink(slot.name)
                    except OSError as exc:
                        if error is None:
                            error = exc
                try:
                    self._release(slot)
                except (BufferError, OSError) as exc:
                    if error is None:
                        error = exc
        self.closed = True
        if error is not None:
            raise error

    def __enter__(self) -> PosixShmRing:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class PresenterStats:
    frames_offered: int = 0
    frames_emitted: int = 0
    frames_dropped: int = 0
    frames_unchanged: int = 0
    full_frames: int = 0
    rect_updates: int = 0
    scroll_updates: int = 0
    pixel_bytes: int = 0
    wire_bytes: int = 0
    latency_samples: int = 0
    latency_total_ms: float = 0.0
    latency_max_ms: float = 0.0
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=256))


@dataclass(frozen=True)
class PresentResult:
    kind: str
    emitted: bool = False
    rects: tuple[Rect, ...] = ()
    pixel_bytes: int = 0
    wire_bytes: int = 0


@dataclass(frozen=True)
class TappedFrame:
    """One frame as it was offered, for a consumer other than this terminal.

    Deliberately a copy of what the caller supplied rather than anything this
    presenter has computed: a remote consumer paces itself, so it must not
    inherit this terminal's drop decisions or its damage arithmetic.
    """

    rgb: bytes
    width: int
    height: int
    columns: int
    rows: int
    scroll: tuple[int, int] | None
    content_key: object
    offered_at: float


class FrameSocketTap:
    """Non-blocking handoff from a presenter to a local Unix socket.

    The presenter thread only replaces one newest-frame slot.  Connecting and
    writing happen on a daemon thread, so an absent or stalled consumer cannot
    delay local presentation.  The socket is local and expected to be private;
    the receiver additionally checks the broker session carried in every
    header.
    """

    _HEADER = struct.Struct("!4sHH64sIIIIiiQQ")
    _VERSION = 1
    _FRAME_MAX = 64 * 1024 * 1024

    def __init__(
        self,
        path: str,
        session: str,
        *,
        retry_seconds: float = 1.0,
        send_timeout: float = 0.5,
    ):
        if not isinstance(path, str) or "\0" in path or not os.path.isabs(path):
            raise ValueError("frame tap socket path must be absolute")
        try:
            encoded_path = os.fsencode(path)
        except UnicodeError as exc:
            raise ValueError("frame tap socket path is not encodable") from exc
        if len(encoded_path) > _UNIX_SOCKET_PATH_BYTES:
            raise ValueError("frame tap socket path is too long")
        if (
            not isinstance(session, str)
            or not session
            or len(session) > 64
            or any(character not in _SESSION_CHARACTERS for character in session)
        ):
            raise ValueError("frame tap session must be 1 to 64 safe ASCII characters")
        encoded_session = session.encode("ascii")
        self.path = path
        self.session = encoded_session
        self.retry_seconds = _number(
            retry_seconds,
            "frame tap retry interval",
            minimum=0.05,
            maximum=_MAX_TAP_RETRY_SECONDS,
        )
        self.send_timeout = _number(
            send_timeout,
            "frame tap send timeout",
            minimum=0.05,
            maximum=_MAX_TAP_SEND_TIMEOUT,
        )
        self.frames_offered = 0
        self.frames_sent = 0
        self.frames_dropped = 0
        self.failures = 0
        self._condition = threading.Condition()
        self._pending: TappedFrame | None = None
        self._closed = False
        self._socket: socket.socket | None = None
        self._retry_at = float("-inf")
        self._thread = threading.Thread(
            target=self._run, name="kitty-frame-tap", daemon=True
        )
        self._thread.start()

    @classmethod
    def from_environment(cls) -> FrameSocketTap | None:
        """Create the explicitly configured tap, or return ``None``.

        The shared package never enables discovery by itself.  A host such as
        Kilix can set ``KILIX_FRAME_TAP_SOCKET`` (or a private directory from
        which the session-specific name is derived) without changing callers.
        """

        session = os.environ.get("KITTY_PTY_BROKER_SESSION", "")
        path = os.environ.get("KILIX_FRAME_TAP_SOCKET", "")
        directory = os.environ.get("KILIX_FRAME_TAP_DIRECTORY", "")
        if not path and directory and session:
            path = os.path.join(directory, f"{session}.tap")
        if not path or not session:
            return None
        return cls(path, session)

    def __call__(self, frame: TappedFrame) -> None:
        try:
            rgb = _as_bytes(frame.rgb)
        except ValueError as exc:
            raise ValueError("invalid RGB frame for socket tap") from exc
        geometry = (frame.width, frame.height, frame.columns, frame.rows)
        if any(type(value) is not int for value in geometry):
            raise ValueError("frame geometry must contain integers")
        expected = frame.width * frame.height * FRAME_BYTES
        if (
            frame.width <= 0
            or frame.height <= 0
            or frame.width > 8192
            or frame.height > 8192
            or frame.columns <= 0
            or frame.rows <= 0
            or frame.columns > 10000
            or frame.rows > 10000
            or len(rgb) != expected
            or expected > self._FRAME_MAX
        ):
            raise ValueError("invalid RGB frame for socket tap")
        if type(frame.offered_at) not in (int, float):
            raise ValueError("invalid frame time for socket tap")
        try:
            offered_at = _time_value(frame.offered_at)
        except ValueError as exc:
            raise ValueError("invalid frame time for socket tap") from exc
        if frame.scroll is not None:
            try:
                scroll = _scroll_hint(frame.scroll)
            except ValueError as exc:
                raise ValueError("invalid scroll hint for socket tap") from exc
            frame_scroll: tuple[int, int] | None = scroll
        else:
            frame_scroll = None
        normalized = TappedFrame(
            rgb=rgb,
            width=frame.width,
            height=frame.height,
            columns=frame.columns,
            rows=frame.rows,
            scroll=frame_scroll,
            content_key=frame.content_key,
            offered_at=offered_at,
        )
        with self._condition:
            if self._closed:
                return
            self.frames_offered += 1
            if self._pending is not None:
                self.frames_dropped += 1
            self._pending = normalized
            self._condition.notify()

    @staticmethod
    def _scroll(frame: TappedFrame) -> tuple[int, int]:
        if frame.scroll is None:
            # INT32_MIN means that no producer hint was supplied.
            return _INT32_MIN, _INT32_MIN
        return frame.scroll

    def _encode_header(self, frame: TappedFrame) -> bytes:
        scroll_x, scroll_y = self._scroll(frame)
        return self._HEADER.pack(
            b"KFT1",
            self._VERSION,
            0,
            self.session.ljust(64, b"\0"),
            frame.width,
            frame.height,
            frame.columns,
            frame.rows,
            scroll_x,
            scroll_y,
            max(0, int(frame.offered_at * 1_000_000)),
            len(frame.rgb),
        )

    def _disconnect(self) -> None:
        with self._condition:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _peer_is_owner(sock: socket.socket) -> bool:
        if not hasattr(socket, "SO_PEERCRED"):
            return True
        size = struct.calcsize("3i")
        credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return bool(uid == os.geteuid())

    def _connect(self) -> bool:
        now = time.monotonic()
        if now < self._retry_at:
            return False
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.send_timeout)
            sock.connect(self.path)
            if not self._peer_is_owner(sock):
                raise PermissionError("frame tap peer has a different owner")
        except OSError:
            if sock is not None:
                sock.close()
            self.failures += 1
            self._retry_at = now + self.retry_seconds
            return False
        with self._condition:
            if self._closed:
                sock.close()
                return False
            self._socket = sock
        self._retry_at = float("-inf")
        return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    break
                frame, self._pending = self._pending, None
            if frame is None:
                continue
            if self._socket is None and not self._connect():
                self.frames_dropped += 1
                continue
            with self._condition:
                sock = self._socket
                closed = self._closed
            if closed:
                break
            if sock is None:
                self.frames_dropped += 1
                continue
            try:
                sock.sendall(self._encode_header(frame))
                sock.sendall(frame.rgb)
                self.frames_sent += 1
            except OSError:
                self.failures += 1
                self.frames_dropped += 1
                self._disconnect()
                self._retry_at = time.monotonic() + self.retry_seconds
        self._disconnect()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            if self._pending is not None:
                self.frames_dropped += 1
                self._pending = None
            self._condition.notify()
        self._disconnect()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=self.send_timeout + 0.25)

    def __enter__(self) -> FrameSocketTap:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass
class _Request:
    rgb: bytes
    width: int
    height: int
    columns: int
    rows: int
    origin_row: int
    origin_column: int
    content_key: object
    force_full: bool
    scroll: tuple[int, int] | None
    damage: tuple[Rect, ...] | None
    offered_at: float


class FramePresenter:
    """Stateful, newest-frame-wins Kitty RGB presenter."""

    def __init__(
        self,
        terminal: TerminalWriter,
        image_id: int = 1,
        *,
        stream: bool = False,
        in_tmux: bool = False,
        max_fps: float = 0,
        shm_slots: int = 3,
        enable_scroll: bool | None = None,
        stream_keyframe_seconds: float = 5.0,
        stream_warmup_seconds: float = 4.0,
        tap: Callable[[TappedFrame], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        image_id = _image_id(image_id)
        if not callable(getattr(terminal, "write", None)):
            raise ValueError("terminal must provide write(str)")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.terminal = terminal
        self.image_id = image_id
        self.stream = bool(stream)
        self.in_tmux = bool(in_tmux)
        self.max_fps = _number(max_fps, "max_fps")
        self.frame_interval = 1.0 / self.max_fps if self.max_fps else 0.0
        if not math.isfinite(self.frame_interval):
            raise ValueError("max_fps is too small to produce a finite interval")
        self.enable_scroll = (
            os.environ.get("KITTY_KILIX_RENDERING") == "1"
            if enable_scroll is None
            else bool(enable_scroll)
        )
        self.stream_keyframe_seconds = _number(
            stream_keyframe_seconds, "stream keyframe interval"
        )
        self.stream_warmup_seconds = _number(
            stream_warmup_seconds, "stream warmup interval"
        )
        self.clock = clock
        self.started_at = _time_value(clock(), "clock result")
        self._last_emit_at = float("-inf")
        self._last_full_at = float("-inf")
        self._busy_retry_at = float("-inf")
        self._base_signature: tuple[int, int, int, int, int, int, object] | None = None
        self._previous: bytes | None = None
        self._pending: _Request | None = None
        self.tap = tap
        self.tap_failures = 0
        self.stats = PresenterStats()
        self.shm = None if self.stream else PosixShmRing(shm_slots)
        self.closed = False

    @property
    def next_deadline(self) -> float | None:
        if self._pending is None:
            return None
        return max(
            self._last_emit_at + self.frame_interval,
            self._busy_retry_at,
        )

    def invalidate(self) -> None:
        # A queued request describes the old placement.  Sending it after a
        # resize or external clear can briefly restore stale geometry, so make
        # the caller offer a frame for the new state instead.
        if self._pending is not None:
            self.stats.frames_dropped += 1
            self._pending = None
        self._base_signature = None
        self._previous = None

    def _request(
        self,
        rgb: object,
        width: object,
        height: object,
        columns: object,
        rows: object,
        origin_row: object,
        origin_column: object,
        content_key: object,
        force_full: object,
        scroll: object,
        damage: object,
        offered_at: object,
    ) -> _Request:
        width, height, columns, rows, origin_row, origin_column = _placement_geometry(
            width, height, columns, rows, origin_row, origin_column
        )
        data = _as_bytes(rgb)
        expected = width * height * FRAME_BYTES
        if len(data) != expected:
            raise ValueError(f"RGB frame length must be {expected} bytes")
        if scroll is not None:
            scroll = _scroll_hint(scroll)
        damage = _damage_hint(damage, width, height)
        return _Request(
            data,
            width,
            height,
            columns,
            rows,
            origin_row,
            origin_column,
            content_key,
            bool(force_full),
            scroll,
            damage,
            _time_value(offered_at),
        )

    def present(
        self,
        rgb: object,
        width: int,
        height: int,
        columns: int,
        rows: int,
        *,
        origin_row: int = 1,
        origin_column: int = 1,
        content_key: object = None,
        force_full: bool = False,
        scroll: tuple[int, int] | None = None,
        damage: Rect | Iterable[Rect] | None = None,
        now: float | None = None,
    ) -> PresentResult:
        if self.closed:
            raise RuntimeError("frame presenter is closed")
        now = self.clock() if now is None else now
        request = self._request(
            rgb,
            width,
            height,
            columns,
            rows,
            origin_row,
            origin_column,
            content_key,
            force_full,
            scroll,
            damage,
            now,
        )
        now = request.offered_at
        self.stats.frames_offered += 1
        self._offer_to_tap(request)
        if self._pending is not None:
            self.stats.frames_dropped += 1
            pending = self._pending
            if (
                pending.damage is None
                or request.damage is None
                or (pending.width, pending.height) != (request.width, request.height)
            ):
                request.damage = None
            else:
                request.damage = _coalesce_rects(
                    (*pending.damage, *request.damage), _MAX_DAMAGE_RECTS
                )
            # Both hints describe transitions through the dropped frame, not
            # directly from the last emitted frame to this request.
            request.scroll = None
            request.force_full = request.force_full or pending.force_full
            self._pending = None
        if now < self._last_emit_at + self.frame_interval:
            self._pending = request
            return PresentResult("queued")
        result = self._emit(request, now)
        if result.kind == "busy":
            self._pending = request
            self._busy_retry_at = now + _SHM_RETRY_SECONDS
            return PresentResult("queued")
        return result

    def _offer_to_tap(self, request: _Request) -> None:
        """Hand the frame to a second consumer, if one is attached.

        Every offered frame is seen, including ones this terminal is about to
        drop, because a remote consumer has its own pacing.

        A tap that raises is disconnected rather than propagated: a second
        consumer must never be able to break the terminal that is actually in
        front of the user.
        """
        if self.tap is None:
            return
        try:
            self.tap(
                TappedFrame(
                    rgb=request.rgb,
                    width=request.width,
                    height=request.height,
                    columns=request.columns,
                    rows=request.rows,
                    scroll=request.scroll,
                    content_key=request.content_key,
                    offered_at=request.offered_at,
                )
            )
        # A secondary sink cannot break the primary presentation path.
        except Exception:
            self.tap_failures += 1
            self.tap = None

    def flush(self, now: float | None = None) -> PresentResult:
        if self._pending is None:
            if self.shm is not None:
                self.shm.reap()
            return PresentResult("idle")
        now = _time_value(self.clock() if now is None else now)
        deadline = self.next_deadline
        if deadline is not None and now < deadline:
            return PresentResult("queued")
        request = self._pending
        result = self._emit(request, now)
        if result.kind != "busy":
            self._pending = None
        else:
            self._busy_retry_at = now + _SHM_RETRY_SECONDS
        return PresentResult("queued") if result.kind == "busy" else result

    def _full_required(self, request: _Request, now: float) -> bool:
        signature = (
            request.width,
            request.height,
            request.columns,
            request.rows,
            request.origin_row,
            request.origin_column,
            request.content_key,
        )
        if request.force_full or signature != self._base_signature:
            return True
        if self.stream:
            if (
                self.stream_warmup_seconds
                and now - self.started_at < self.stream_warmup_seconds
            ):
                return True
            if (
                self.stream_keyframe_seconds
                and now - self._last_full_at >= self.stream_keyframe_seconds
            ):
                return True
        return False

    def _write(self, sequence: str) -> int:
        if sequence:
            self.terminal.write(sequence)
        # Every sequence assembled here is ASCII.  Encoding it solely to count
        # wire bytes doubled peak storage for large inline frames.
        return len(sequence)

    def _finish(
        self,
        request: _Request,
        now: float,
        kind: str,
        rects: Iterable[Rect],
        pixel_bytes: int,
        wire_bytes: int,
        full: bool = False,
        scroll: bool = False,
    ) -> PresentResult:
        rect_tuple = tuple(rects)
        self._previous = request.rgb
        self._base_signature = (
            request.width,
            request.height,
            request.columns,
            request.rows,
            request.origin_row,
            request.origin_column,
            request.content_key,
        )
        self._last_emit_at = now
        self._busy_retry_at = float("-inf")
        if full:
            self._last_full_at = now
            self.stats.full_frames += 1
        self.stats.frames_emitted += 1
        self.stats.rect_updates += 0 if full else len(rect_tuple)
        self.stats.scroll_updates += int(scroll)
        self.stats.pixel_bytes += pixel_bytes
        self.stats.wire_bytes += wire_bytes
        latency = max(0.0, (now - request.offered_at) * 1000.0)
        self.stats.latency_samples += 1
        self.stats.latency_total_ms += latency
        self.stats.latency_max_ms = max(self.stats.latency_max_ms, latency)
        self.stats.latencies_ms.append(latency)
        return PresentResult(kind, True, rect_tuple, pixel_bytes, wire_bytes)

    def _emit_full(self, request: _Request, now: float) -> PresentResult:
        names: tuple[str, ...] = ()
        if self.stream:
            sequence = build_direct(
                request.rgb,
                request.width,
                request.height,
                request.columns,
                request.rows,
                self.image_id,
                request.origin_row,
                request.origin_column,
                self.in_tmux,
            )
        else:
            shm = self.shm
            if shm is None:
                raise RuntimeError("local presenter has no shared-memory transport")
            try:
                names = shm.put_many((request.rgb,))
            except ShmBusy:
                return PresentResult("busy")
            sequence = build_full_shm(
                names[0],
                request.width,
                request.height,
                request.columns,
                request.rows,
                self.image_id,
                request.origin_row,
                request.origin_column,
            )
        try:
            wire = self._write(_SYNC_BEGIN + sequence + _SYNC_END)
        except Exception:
            # The terminal never received the command naming these objects,
            # so nobody can consume and unlink them.  Roll the slots back or
            # each failed write would strand ring capacity forever.
            if names and self.shm is not None:
                self.shm.revoke(names)
            raise
        return self._finish(request, now, "full", (), len(request.rgb), wire, full=True)

    def _emit_rects(
        self,
        request: _Request,
        now: float,
        rects: Sequence[Rect],
        prefix: str = "",
        kind: str = "rect",
        scrolled: bool = False,
    ) -> PresentResult:
        payloads = tuple(
            extract_rect(request.rgb, request.width, request.height, rect)
            for rect in rects
        )
        names: tuple[str, ...] = ()
        if self.stream:
            edits = "".join(
                build_frame_edit(
                    payload,
                    rect[2],
                    rect[3],
                    rect[0],
                    rect[1],
                    self.image_id,
                    self.in_tmux,
                )
                for payload, rect in zip(payloads, rects)
            )
        else:
            shm = self.shm
            if shm is None:
                raise RuntimeError("local presenter has no shared-memory transport")
            try:
                names = shm.put_many(payloads)
            except ShmBusy:
                return PresentResult("busy")
            edits = "".join(
                build_frame_edit_shm(
                    name, rect[2], rect[3], rect[0], rect[1], self.image_id
                )
                for name, rect in zip(names, rects)
            )
        try:
            wire = self._write(_SYNC_BEGIN + prefix + edits + _SYNC_END)
        except Exception:
            # Same rollback contract as the full-frame path: an undelivered
            # command must not leave slots waiting on a consumer that will
            # never see their names.
            if names and self.shm is not None:
                self.shm.revoke(names)
            raise
        return self._finish(
            request, now, kind, rects, sum(map(len, payloads)), wire, scroll=scrolled
        )

    def _scroll_candidate(
        self, request: _Request, normal: Rect
    ) -> tuple[Rect, Rect, tuple[Rect, ...]] | None:
        previous = self._previous
        if previous is None:
            return None
        shift = request.scroll
        if shift is None:
            # Cursor/caret-sized damage cannot be a useful screen scroll.  The
            # guard also avoids hashing every row for the common small-update
            # path.  Explicit metadata hints are still evaluated regardless
            # of damage size.
            normal_area = normal[2] * normal[3]
            frame_area = request.width * request.height
            if normal_area < frame_area * 0.20:
                return None
            shift = detect_vertical_scroll(
                previous, request.rgb, request.width, request.height
            )
        if not shift or shift == (0, 0):
            return None
        dx, dy = shift
        try:
            predicted, source, destination = _shift_prediction(
                previous, request.width, request.height, dx, dy
            )
        except ValueError:
            return None
        max_rects = 2
        if self.shm is not None:
            max_rects = min(max_rects, self.shm.capacity)
        residual = diff_rects(
            predicted,
            request.rgb,
            request.width,
            request.height,
            max_rects=max_rects,
        )
        normal_area = normal[2] * normal[3]
        residual_area = sum(rect[2] * rect[3] for rect in residual)
        # Composition is worthwhile only when it materially reduces copied
        # pixels.  The command itself carries no pixel payload.
        if residual_area >= normal_area * 0.85:
            return None
        return source, destination, residual

    def _emit(self, request: _Request, now: float) -> PresentResult:
        if self._full_required(request, now) or self._previous is None:
            return self._emit_full(request, now)
        max_rects = 8 if self.stream else 1
        if self.shm is not None:
            max_rects = self.shm.capacity
        rects: tuple[Rect, ...]
        if request.damage is None:
            normal = diff_rect(
                self._previous, request.rgb, request.width, request.height
            )
            rects = () if normal is None else (normal,)
        else:
            rects = diff_damage_rects(
                self._previous,
                request.rgb,
                request.width,
                request.height,
                request.damage,
                max_rects=max_rects,
            )
        if not rects:
            self._previous = request.rgb
            self.stats.frames_unchanged += 1
            return PresentResult("unchanged")
        normal = rects[0]
        for rect in rects[1:]:
            normal = _rect_union(normal, rect)
        if self.enable_scroll:
            candidate = self._scroll_candidate(request, normal)
            if candidate is not None:
                source, destination, residual = candidate
                compose = build_compose(
                    self.image_id, source, destination, self.in_tmux
                )
                if not residual:
                    wire = self._write(_SYNC_BEGIN + compose + _SYNC_END)
                    return self._finish(
                        request, now, "scroll", (), 0, wire, scroll=True
                    )
                return self._emit_rects(
                    request, now, residual, compose, "scroll", scrolled=True
                )
        return self._emit_rects(request, now, rects)

    def close(self, *, discard: bool = False) -> None:
        if self.closed:
            return
        if self._pending is not None:
            self.stats.frames_dropped += 1
            self._pending = None
        try:
            if self.shm is not None:
                self.shm.close(discard=discard)
        finally:
            self.closed = True

    def __enter__(self) -> FramePresenter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
