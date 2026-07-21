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
import mmap
import os
import secrets
import time
import zlib
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence, Tuple

CHUNK = 4096
FRAME_BYTES = 3
_SCAN_PIXELS = 32
_SYNC_BEGIN = "\x1b[?2026h"
_SYNC_END = "\x1b[?2026l"

Rect = Tuple[int, int, int, int]


def _as_bytes(value) -> bytes:
    return value if isinstance(value, bytes) else bytes(value)


def _valid_frames(prev, cur, width: int, height: int) -> Tuple[bytes, bytes]:
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    expected = width * height * FRAME_BYTES
    p, c = _as_bytes(prev), _as_bytes(cur)
    if len(p) != expected or len(c) != expected:
        raise ValueError(
            f"RGB frame length must be {expected} bytes for {width}x{height}")
    return p, c


def _row_bounds(p: bytes, c: bytes, offset: int, width: int) -> Optional[Tuple[int, int]]:
    """Return the first/last changed pixel columns for one RGB row."""
    stride = width * FRAME_BYTES
    if p[offset:offset + stride] == c[offset:offset + stride]:
        return None
    step = _SCAN_PIXELS * FRAME_BYTES
    left_block = 0
    while left_block < stride:
        end = min(stride, left_block + step)
        if p[offset + left_block:offset + end] != c[offset + left_block:offset + end]:
            break
        left_block = end
    left = left_block // FRAME_BYTES
    for x in range(left, min(width, left + _SCAN_PIXELS)):
        at = offset + x * FRAME_BYTES
        if p[at:at + FRAME_BYTES] != c[at:at + FRAME_BYTES]:
            left = x
            break

    right_block = stride
    while right_block > 0:
        start = max(0, right_block - step)
        if p[offset + start:offset + right_block] != c[offset + start:offset + right_block]:
            break
        right_block = start
    right = min(width, (right_block + FRAME_BYTES - 1) // FRAME_BYTES)
    for x in range(right - 1, max(left - 1, right - _SCAN_PIXELS - 1), -1):
        at = offset + x * FRAME_BYTES
        if p[at:at + FRAME_BYTES] != c[at:at + FRAME_BYTES]:
            right = x + 1
            break
    return left, right


def diff_rect(prev, cur, width: int, height: int) -> Optional[Rect]:
    """Return the exact bounding rectangle changed between two RGB frames."""
    p, c = _valid_frames(prev, cur, width, height)
    if prev is cur or p == c:
        return None
    stride = width * FRAME_BYTES
    top = 0
    while p[top * stride:(top + 1) * stride] == c[top * stride:(top + 1) * stride]:
        top += 1
    bottom = height
    while bottom > top + 1 and p[(bottom - 1) * stride:bottom * stride] == c[(bottom - 1) * stride:bottom * stride]:
        bottom -= 1
    left, right = width, 0
    for y in range(top, bottom):
        bounds = _row_bounds(p, c, y * stride, width)
        if bounds is None:
            continue
        left = min(left, bounds[0])
        right = max(right, bounds[1])
        if left == 0 and right == width:
            break
    return left, top, right - left, bottom - top


def diff_band(prev, cur, width: int, height: int):
    """Compatibility helper returning only the changed row band."""
    try:
        rect = diff_rect(prev, cur, width, height)
    except ValueError:
        return 0, height
    return None if rect is None else (rect[1], rect[3])


def diff_rects(prev, cur, width: int, height: int, max_rects: int = 2) -> Tuple[Rect, ...]:
    """Return up to ``max_rects`` exact rectangles split at clean row gaps.

    Scroll composition commonly leaves two disjoint regions: fixed chrome at
    one edge and a newly exposed strip at the other.  Keeping those separate
    avoids turning their bounding box back into an almost-full frame.
    """
    if max_rects < 1:
        raise ValueError("max_rects must be positive")
    p, c = _valid_frames(prev, cur, width, height)
    if prev is cur or p == c:
        return ()
    stride = width * FRAME_BYTES
    bands = []
    start = None
    for y in range(height):
        changed = p[y * stride:(y + 1) * stride] != c[y * stride:(y + 1) * stride]
        if changed and start is None:
            start = y
        elif not changed and start is not None:
            bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, height))

    while len(bands) > max_rects:
        # Merge the pair separated by the smallest clean gap.  This minimizes
        # overdraw while retaining the bounded-command/backpressure contract.
        at = min(range(len(bands) - 1), key=lambda i: bands[i + 1][0] - bands[i][1])
        bands[at:at + 2] = [(bands[at][0], bands[at + 1][1])]

    out = []
    for top, bottom in bands:
        left, right = width, 0
        for y in range(top, bottom):
            bounds = _row_bounds(p, c, y * stride, width)
            if bounds:
                left, right = min(left, bounds[0]), max(right, bounds[1])
        out.append((left, top, right - left, bottom - top))
    return tuple(out)


def extract_rect(rgb, width: int, height: int, rect: Rect) -> bytes:
    """Copy one rectangular region from a tightly packed RGB frame."""
    frame = _as_bytes(rgb)
    expected = width * height * FRAME_BYTES
    if len(frame) != expected:
        raise ValueError(f"RGB frame length must be {expected}")
    x, y, rw, rh = rect
    if rw <= 0 or rh <= 0 or x < 0 or y < 0 or x + rw > width or y + rh > height:
        raise ValueError("damage rectangle is outside the frame")
    if x == 0 and rw == width:
        stride = width * FRAME_BYTES
        return frame[y * stride:(y + rh) * stride]
    stride = width * FRAME_BYTES
    row_bytes = rw * FRAME_BYTES
    out = bytearray(row_bytes * rh)
    for row in range(rh):
        source = (y + row) * stride + x * FRAME_BYTES
        target = row * row_bytes
        out[target:target + row_bytes] = frame[source:source + row_bytes]
    return bytes(out)


def detect_vertical_scroll(prev, cur, width: int, height: int,
                           max_shift: Optional[int] = None) -> Optional[Tuple[int, int]]:
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
    limit = min(max_shift or max(1, height // 3), height - 2)
    old_rows = {}
    for y in range(height):
        checksum = zlib.crc32(memoryview(p)[y * stride:(y + 1) * stride])
        old_rows.setdefault(checksum, []).append(y)
    votes = {}
    sample_count = min(17, height)
    for index in range(sample_count):
        y = index * (height - 1) // max(1, sample_count - 1)
        checksum = zlib.crc32(memoryview(c)[y * stride:(y + 1) * stride])
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
            matches += p[po:po + stride] == c[co:co + stride]
        if checks and matches / checks >= 0.60:
            return 0, dy
    return None


def _shift_prediction(prev: bytes, width: int, height: int,
                      dx: int, dy: int) -> Tuple[bytes, Rect, Rect]:
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
    # Snapshot the overlap before assigning because source/destination overlap.
    moved = bytearray(row_bytes * copy_h)
    for row in range(copy_h):
        at = (src_y + row) * stride + src_x * FRAME_BYTES
        moved[row * row_bytes:(row + 1) * row_bytes] = prev[at:at + row_bytes]
    for row in range(copy_h):
        at = (dst_y + row) * stride + dst_x * FRAME_BYTES
        result[at:at + row_bytes] = moved[row * row_bytes:(row + 1) * row_bytes]
    return bytes(result), source, dest


def _tmux_wrap(apc: str) -> str:
    return "\x1bPtmux;" + apc.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def _apc(control: str, payload: str = "", in_tmux: bool = False) -> str:
    value = f"\x1b_G{control};{payload}\x1b\\"
    return _tmux_wrap(value) if in_tmux else value


def build_direct(rgb: bytes, width: int, height: int, columns: int, rows: int,
                 image_id: int, origin_row: int = 1, origin_column: int = 1,
                 in_tmux: bool = False) -> str:
    """Build a compressed, chunked inline full-frame placement."""
    if not rgb:
        return ""
    payload = base64.b64encode(zlib.compress(rgb, 1))
    chunks = [payload[i:i + CHUNK] for i in range(0, len(payload), CHUNK)]
    out = [f"\x1b[{origin_row};{origin_column}H"]
    for index, chunk in enumerate(chunks):
        more = int(index + 1 < len(chunks))
        if index == 0:
            control = (f"a=T,i={image_id},p=1,z=-1,t=d,f=24,o=z,N=1,"
                       f"s={width},v={height},c={columns},r={rows},q=2,C=1,m={more}")
        else:
            control = f"m={more}"
        out.append(_apc(control, chunk.decode("ascii"), in_tmux))
    return "".join(out)


def build_frame_edit(rgb: bytes, width: int, height: int, x: int, y: int,
                     image_id: int, in_tmux: bool = False) -> str:
    """Build a compressed, chunked inline root-frame edit."""
    if not rgb:
        return ""
    payload = base64.b64encode(zlib.compress(rgb, 1))
    chunks = [payload[i:i + CHUNK] for i in range(0, len(payload), CHUNK)]
    out = []
    for index, chunk in enumerate(chunks):
        more = int(index + 1 < len(chunks))
        if index == 0:
            control = (f"a=f,i={image_id},r=1,x={x},y={y},t=d,f=24,o=z,N=1,"
                       f"s={width},v={height},q=2,m={more}")
        else:
            control = f"a=f,i={image_id},r=1,q=2,m={more}"
        out.append(_apc(control, chunk.decode("ascii"), in_tmux))
    return "".join(out)


def build_full_shm(name: str, width: int, height: int, columns: int, rows: int,
                   image_id: int, origin_row: int = 1,
                   origin_column: int = 1) -> str:
    payload = base64.b64encode(name.encode("ascii")).decode("ascii")
    return (f"\x1b[{origin_row};{origin_column}H" +
            _apc(f"a=T,i={image_id},p=1,z=-1,t=s,f=24,N=1,s={width},v={height},"
                 f"c={columns},r={rows},q=2,C=1", payload))


def build_frame_edit_shm(name: str, width: int, height: int, x: int, y: int,
                         image_id: int) -> str:
    payload = base64.b64encode(name.encode("ascii")).decode("ascii")
    return _apc(f"a=f,i={image_id},r=1,x={x},y={y},t=s,f=24,N=1,"
                f"s={width},v={height},q=2", payload)


def build_compose(image_id: int, source: Rect, destination: Rect,
                  in_tmux: bool = False) -> str:
    """Build the Kilix-fork overlapping root-frame replacement command."""
    sx, sy, sw, sh = source
    dx, dy, dw, dh = destination
    if (sw, sh) != (dw, dh):
        raise ValueError("compose source and destination sizes differ")
    # N=2 opts into the fork's safe same-frame-overlap extension.  Stock Kitty
    # retains the protocol-mandated EINVAL behavior for overlap.
    return _apc(f"a=c,i={image_id},r=1,c=1,x={dx},y={dy},X={sx},Y={sy},"
                f"w={sw},h={sh},C=1,N=2,q=2", "", in_tmux)


class ShmBusy(RuntimeError):
    """Raised when every bounded shared-memory slot is still in flight."""


@dataclass
class _ShmSlot:
    name: str
    fd: int = -1
    mapping: Optional[mmap.mmap] = None
    busy: bool = False


class PosixShmRing:
    """A bounded, unlink-acknowledged POSIX shared-memory transport.

    Kitty unlinks a ``t=s`` object immediately after opening it.  An absent
    name is therefore a consumption acknowledgement.  Reusing that *name*
    creates a new object; Kitty's mapping of the old unlinked object remains
    valid, so a fast producer cannot tear an older frame.
    """

    def __init__(self, slots: int = 3, prefix: str = "kitty-frame-presenter"):
        if slots < 1:
            raise ValueError("slots must be positive")
        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            raise OSError(errno.ENOSYS, "C library not found")
        self._libc = ctypes.CDLL(libc_name, use_errno=True)
        self._libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
        self._libc.shm_open.restype = ctypes.c_int
        self._libc.shm_unlink.argtypes = [ctypes.c_char_p]
        self._libc.shm_unlink.restype = ctypes.c_int
        token = secrets.token_hex(6)
        base = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in prefix)
        self._slots = [_ShmSlot(f"/{base}-{os.getpid()}-{token}-{i}")
                       for i in range(slots)]
        self.closed = False

    def _open(self, name: str, flags: int, mode: int = 0o600) -> int:
        fd = self._libc.shm_open(name.encode("ascii"), flags, mode)
        if fd < 0:
            value = ctypes.get_errno()
            raise OSError(value, os.strerror(value), name)
        return fd

    def _unlink(self, name: str, missing_ok: bool = True) -> None:
        if self._libc.shm_unlink(name.encode("ascii")) != 0:
            value = ctypes.get_errno()
            if not (missing_ok and value == errno.ENOENT):
                raise OSError(value, os.strerror(value), name)

    @staticmethod
    def _release(slot: _ShmSlot) -> None:
        if slot.mapping is not None:
            slot.mapping.close()
        if slot.fd >= 0:
            os.close(slot.fd)
        slot.mapping, slot.fd, slot.busy = None, -1, False

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

    def put_many(self, payloads: Sequence[bytes]) -> Tuple[str, ...]:
        if self.closed:
            raise RuntimeError("shared-memory ring is closed")
        if not payloads:
            return ()
        self.reap()
        free = [slot for slot in self._slots if not slot.busy]
        if len(free) < len(payloads):
            raise ShmBusy("shared-memory ring is saturated")
        made = []
        try:
            for slot, payload in zip(free, payloads):
                data = _as_bytes(payload)
                if not data:
                    raise ValueError("shared-memory payload cannot be empty")
                fd = self._open(slot.name, os.O_RDWR | os.O_CREAT | os.O_EXCL)
                os.ftruncate(fd, len(data))
                mapping = mmap.mmap(fd, len(data), access=mmap.ACCESS_WRITE)
                mapping[:] = data
                slot.fd, slot.mapping, slot.busy = fd, mapping, True
                made.append(slot)
            return tuple(slot.name for slot in made)
        except Exception:
            for slot in made:
                self._unlink(slot.name)
                self._release(slot)
            raise

    def close(self) -> None:
        if self.closed:
            return
        for slot in self._slots:
            if slot.busy:
                self._unlink(slot.name)
                self._release(slot)
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
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
    latencies_ms: list = field(default_factory=list)


@dataclass(frozen=True)
class PresentResult:
    kind: str
    emitted: bool = False
    rects: Tuple[Rect, ...] = ()
    pixel_bytes: int = 0
    wire_bytes: int = 0


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
    scroll: Optional[Tuple[int, int]]
    offered_at: float


class FramePresenter:
    """Stateful, newest-frame-wins Kitty RGB presenter."""

    def __init__(self, terminal, image_id: int = 1, *, stream: bool = False,
                 in_tmux: bool = False, max_fps: float = 0,
                 shm_slots: int = 3, enable_scroll: Optional[bool] = None,
                 stream_keyframe_seconds: float = 5.0,
                 stream_warmup_seconds: float = 4.0,
                 clock=time.monotonic):
        if image_id <= 0:
            raise ValueError("image_id must be positive")
        self.terminal = terminal
        self.image_id = image_id
        self.stream = stream
        self.in_tmux = in_tmux
        self.max_fps = max(0.0, float(max_fps))
        self.frame_interval = 1.0 / self.max_fps if self.max_fps else 0.0
        self.enable_scroll = (os.environ.get("KITTY_KILIX_RENDERING") == "1"
                              if enable_scroll is None else bool(enable_scroll))
        self.stream_keyframe_seconds = max(0.0, stream_keyframe_seconds)
        self.stream_warmup_seconds = max(0.0, stream_warmup_seconds)
        self.clock = clock
        self.started_at = clock()
        self._last_emit_at = float("-inf")
        self._last_full_at = float("-inf")
        self._base_signature = None
        self._previous = None
        self._pending: Optional[_Request] = None
        self.stats = PresenterStats()
        self.shm = None if stream else PosixShmRing(shm_slots)

    @property
    def next_deadline(self) -> Optional[float]:
        if self._pending is None:
            return None
        return self._last_emit_at + self.frame_interval

    def invalidate(self) -> None:
        self._base_signature = None
        self._previous = None

    def _request(self, rgb, width, height, columns, rows, origin_row,
                 origin_column, content_key, force_full, scroll, offered_at):
        data = _as_bytes(rgb)
        expected = width * height * FRAME_BYTES
        if width <= 0 or height <= 0 or len(data) != expected:
            raise ValueError(f"RGB frame length must be {expected} bytes")
        if columns <= 0 or rows <= 0 or origin_row <= 0 or origin_column <= 0:
            raise ValueError("placement geometry must be positive")
        if scroll is not None:
            scroll = (int(scroll[0]), int(scroll[1]))
        return _Request(data, width, height, columns, rows, origin_row,
                        origin_column, content_key, force_full, scroll, offered_at)

    def present(self, rgb, width: int, height: int, columns: int, rows: int,
                *, origin_row: int = 1, origin_column: int = 1,
                content_key=None, force_full: bool = False,
                scroll: Optional[Tuple[int, int]] = None,
                now: Optional[float] = None) -> PresentResult:
        now = self.clock() if now is None else now
        request = self._request(rgb, width, height, columns, rows, origin_row,
                                origin_column, content_key, force_full, scroll, now)
        self.stats.frames_offered += 1
        if self._pending is not None:
            self.stats.frames_dropped += 1
            self._pending = None
        if now < self._last_emit_at + self.frame_interval:
            self._pending = request
            return PresentResult("queued")
        result = self._emit(request, now)
        if result.kind == "busy":
            self._pending = request
            return PresentResult("queued")
        return result

    def flush(self, now: Optional[float] = None) -> PresentResult:
        if self._pending is None:
            if self.shm is not None:
                self.shm.reap()
            return PresentResult("idle")
        now = self.clock() if now is None else now
        if now < self._last_emit_at + self.frame_interval:
            return PresentResult("queued")
        request = self._pending
        result = self._emit(request, now)
        if result.kind != "busy":
            self._pending = None
        return PresentResult("queued") if result.kind == "busy" else result

    def _full_required(self, request: _Request, now: float) -> bool:
        signature = (request.width, request.height, request.columns, request.rows,
                     request.origin_row, request.origin_column, request.content_key)
        if request.force_full or signature != self._base_signature:
            return True
        if self.stream:
            if now - self.started_at < self.stream_warmup_seconds:
                return True
            if self.stream_keyframe_seconds and now - self._last_full_at >= self.stream_keyframe_seconds:
                return True
        return False

    def _write(self, sequence: str) -> int:
        if sequence:
            self.terminal.write(sequence)
        return len(sequence.encode("utf-8"))

    def _finish(self, request: _Request, now: float, kind: str,
                rects: Iterable[Rect], pixel_bytes: int, wire_bytes: int,
                full: bool = False, scroll: bool = False) -> PresentResult:
        rect_tuple = tuple(rects)
        self._previous = request.rgb
        self._base_signature = (
            request.width, request.height, request.columns, request.rows,
            request.origin_row, request.origin_column, request.content_key)
        self._last_emit_at = now
        if full:
            self._last_full_at = now
            self.stats.full_frames += 1
        self.stats.frames_emitted += 1
        self.stats.rect_updates += 0 if full else len(rect_tuple)
        self.stats.scroll_updates += int(scroll)
        self.stats.pixel_bytes += pixel_bytes
        self.stats.wire_bytes += wire_bytes
        self.stats.latencies_ms.append(max(0.0, (now - request.offered_at) * 1000.0))
        return PresentResult(kind, True, rect_tuple, pixel_bytes, wire_bytes)

    def _emit_full(self, request: _Request, now: float) -> PresentResult:
        if self.stream:
            sequence = build_direct(
                request.rgb, request.width, request.height,
                request.columns, request.rows, self.image_id,
                request.origin_row, request.origin_column, self.in_tmux)
        else:
            try:
                name = self.shm.put_many((request.rgb,))[0]
            except ShmBusy:
                return PresentResult("busy")
            sequence = build_full_shm(
                name, request.width, request.height, request.columns,
                request.rows, self.image_id, request.origin_row,
                request.origin_column)
        wire = self._write(_SYNC_BEGIN + sequence + _SYNC_END)
        return self._finish(request, now, "full", (), len(request.rgb), wire,
                            full=True)

    def _emit_rects(self, request: _Request, now: float,
                    rects: Sequence[Rect], prefix: str = "",
                    kind: str = "rect", scrolled: bool = False) -> PresentResult:
        payloads = tuple(extract_rect(request.rgb, request.width,
                                      request.height, rect) for rect in rects)
        if self.stream:
            edits = "".join(build_frame_edit(
                payload, rect[2], rect[3], rect[0], rect[1], self.image_id,
                self.in_tmux) for payload, rect in zip(payloads, rects))
        else:
            try:
                names = self.shm.put_many(payloads)
            except ShmBusy:
                return PresentResult("busy")
            edits = "".join(build_frame_edit_shm(
                name, rect[2], rect[3], rect[0], rect[1], self.image_id)
                for name, rect in zip(names, rects))
        wire = self._write(_SYNC_BEGIN + prefix + edits + _SYNC_END)
        return self._finish(request, now, kind, rects,
                            sum(map(len, payloads)), wire, scroll=scrolled)

    def _scroll_candidate(self, request: _Request, normal: Rect):
        shift = request.scroll
        if shift is None:
            shift = detect_vertical_scroll(self._previous, request.rgb,
                                           request.width, request.height)
        if not shift or shift == (0, 0):
            return None
        dx, dy = shift
        try:
            predicted, source, destination = _shift_prediction(
                self._previous, request.width, request.height, dx, dy)
        except ValueError:
            return None
        residual = diff_rects(predicted, request.rgb, request.width,
                              request.height, max_rects=2)
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
        normal = diff_rect(self._previous, request.rgb,
                           request.width, request.height)
        if normal is None:
            self._previous = request.rgb
            self.stats.frames_unchanged += 1
            return PresentResult("unchanged")
        if self.enable_scroll:
            candidate = self._scroll_candidate(request, normal)
            if candidate is not None:
                source, destination, residual = candidate
                compose = build_compose(self.image_id, source, destination,
                                        self.in_tmux)
                if not residual:
                    wire = self._write(_SYNC_BEGIN + compose + _SYNC_END)
                    return self._finish(request, now, "scroll", (), 0, wire,
                                        scroll=True)
                return self._emit_rects(request, now, residual, compose,
                                        "scroll", scrolled=True)
        return self._emit_rects(request, now, (normal,))

    def close(self) -> None:
        self._pending = None
        if self.shm is not None:
            self.shm.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

