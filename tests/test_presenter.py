import base64
import os
import random
import re
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kitty_frame_presenter import (  # noqa: E402
    CHUNK,
    FramePresenter,
    PosixShmRing,
    ShmBusy,
    build_compose,
    build_direct,
    build_frame_edit,
    build_frame_edit_shm,
    build_full_shm,
    detect_vertical_scroll,
    diff_band,
    diff_rect,
    diff_rects,
    extract_rect,
    wrap_tmux_passthrough,
)
from kitty_frame_presenter.presenter import _shift_prediction  # noqa: E402


class ConsumerTerm:
    """Fake Kitty that reads and unlinks every t=s object synchronously."""

    SHM = re.compile(r"\x1b_G([^;]*\bt=s\b[^;]*);([A-Za-z0-9+/=]+)\x1b\\")

    def __init__(self, consume=True):
        self.writes = []
        self.payloads = []
        self.consume = consume

    def write(self, value):
        self.writes.append(value)
        if not self.consume:
            return
        for match in self.SHM.finditer(value):
            name = base64.b64decode(match.group(2)).decode("ascii")
            path = "/dev/shm/" + name.lstrip("/")
            with open(path, "rb") as stream:
                self.payloads.append(stream.read())
            os.unlink(path)

    def output(self):
        return "".join(self.writes)


def frame(width, height, pixel=(0, 0, 0)):
    return bytes(pixel) * (width * height)


def row_frame(width, rows):
    return b"".join(bytes((value, value, value)) * width for value in rows)


class DamageTests(unittest.TestCase):
    def test_exact_rectangle(self):
        width, height = 80, 50
        before = bytearray(frame(width, height))
        after = bytearray(before)
        for y in range(17, 23):
            for x in range(11, 29):
                at = (y * width + x) * 3
                after[at:at + 3] = b"\x01\x02\x03"
        self.assertEqual(diff_rect(before, after, width, height),
                         (11, 17, 18, 6))
        self.assertEqual(diff_band(before, after, width, height), (17, 6))
        self.assertEqual(len(extract_rect(after, width, height,
                                          (11, 17, 18, 6))), 18 * 6 * 3)

    def test_disjoint_row_regions_stay_separate(self):
        width, height = 20, 20
        before = bytearray(frame(width, height))
        after = bytearray(before)
        after[0:3] = b"\xff\0\0"
        at = ((height - 1) * width + (width - 1)) * 3
        after[at:at + 3] = b"\0\xff\0"
        self.assertEqual(diff_rects(before, after, width, height),
                         ((0, 0, 1, 1), (19, 19, 1, 1)))

    def test_bad_sizes_are_full_damage_in_compat_helper(self):
        self.assertEqual(diff_band(b"x", b"y", 10, 5), (0, 5))
        with self.assertRaises(ValueError):
            diff_rect(b"x", b"y", 10, 5)

    def test_vertical_scroll_detection(self):
        width, height = 16, 40
        before = row_frame(width, range(height))
        after = row_frame(width, list(range(5, height)) + [200] * 5)
        self.assertEqual(detect_vertical_scroll(before, after, width, height),
                         (0, -5))


class SharedMemoryTests(unittest.TestCase):
    def test_ring_saturates_then_reuses_unlinked_name_safely(self):
        ring = PosixShmRing(2, prefix="kitty-frame-presenter-test")
        try:
            names = ring.put_many((b"first", b"second"))
            self.assertEqual(ring.in_flight, 2)
            with self.assertRaises(ShmBusy):
                ring.put_many((b"third",))
            os.unlink("/dev/shm/" + names[0].lstrip("/"))
            replacement = ring.put_many((b"third",))[0]
            self.assertEqual(replacement, names[0])
            with open("/dev/shm/" + replacement.lstrip("/"), "rb") as stream:
                self.assertEqual(stream.read(), b"third")
        finally:
            ring.close(discard=True)

    def test_partial_allocation_failure_leaves_no_object_or_busy_slot(self):
        ring = PosixShmRing(1, prefix="kitty-frame-presenter-failure-test")
        name = ring._slots[0].name
        try:
            with mock.patch("mmap.mmap", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    ring.put_many((b"payload",))
            self.assertEqual(ring.in_flight, 0)
            self.assertFalse(os.path.exists("/dev/shm/" + name.lstrip("/")))
        finally:
            ring.close()

    def test_close_keeps_unconsumed_payload_available(self):
        ring = PosixShmRing(1, prefix="kitty-frame-presenter-close-test")
        name = ring.put_many((b"deferred",))[0]
        path = "/dev/shm/" + name.lstrip("/")

        ring.close()

        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), b"deferred")
        os.unlink(path)

    def test_discard_explicitly_unlinks_unconsumed_payload(self):
        ring = PosixShmRing(1, prefix="kitty-frame-presenter-discard-test")
        name = ring.put_many((b"abandoned",))[0]
        path = "/dev/shm/" + name.lstrip("/")

        ring.close(discard=True)

        self.assertFalse(os.path.exists(path))


class BuilderTests(unittest.TestCase):
    def test_tmux_passthrough_is_public(self):
        value = wrap_tmux_passthrough("\x1b_Ga=d,d=A\x1b\\")
        self.assertTrue(value.startswith("\x1bPtmux;"))
        self.assertIn("\x1b\x1b_G", value)

    def test_inline_full_frame_chunks_at_protocol_limit(self):
        rng = random.Random(7)
        data = bytes(rng.randrange(256) for _ in range(100 * 200 * 3))
        value = build_direct(data, 100, 200, 10, 10, 7)
        payloads = re.findall(r";([A-Za-z0-9+/=]+)\x1b\\", value)
        self.assertGreater(len(payloads), 1)
        self.assertTrue(all(len(payload) <= CHUNK for payload in payloads))
        self.assertIn("a=T", value)
        self.assertIn("o=z", value)


def reference_rect(prev, cur, width, height):
    """Per-pixel oracle for diff_rect's exact bounding rectangle."""
    columns, rows = [], []
    for y in range(height):
        for x in range(width):
            at = (y * width + x) * 3
            if prev[at:at + 3] != cur[at:at + 3]:
                columns.append(x)
                rows.append(y)
    if not columns:
        return None
    left, right = min(columns), max(columns) + 1
    top, bottom = min(rows), max(rows) + 1
    return left, top, right - left, bottom - top


def reference_rects(prev, cur, width, height, max_rects=2):
    """Per-pixel oracle for diff_rects, sharing only the band-merge policy."""
    stride = width * 3
    bands = []
    start = None
    for y in range(height):
        changed = prev[y * stride:(y + 1) * stride] != cur[y * stride:(y + 1) * stride]
        if changed and start is None:
            start = y
        elif not changed and start is not None:
            bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, height))
    if not bands:
        return ()
    while len(bands) > max_rects:
        at = min(range(len(bands) - 1), key=lambda i: bands[i + 1][0] - bands[i][1])
        bands[at:at + 2] = [(bands[at][0], bands[at + 1][1])]
    out = []
    for top, bottom in bands:
        columns = [x for y in range(top, bottom) for x in range(width)
                   if prev[(y * width + x) * 3:(y * width + x) * 3 + 3]
                   != cur[(y * width + x) * 3:(y * width + x) * 3 + 3]]
        left, right = min(columns), max(columns) + 1
        out.append((left, top, right - left, bottom - top))
    return tuple(out)


def reference_shift(prev, width, height, dx, dy):
    """Pixel-grid oracle for _shift_prediction's simulated compose."""
    copy_w, copy_h = width - abs(dx), height - abs(dy)
    src_x, src_y = max(0, -dx), max(0, -dy)
    dst_x, dst_y = max(0, dx), max(0, dy)
    pixels = [[prev[(y * width + x) * 3:(y * width + x) * 3 + 3]
               for x in range(width)] for y in range(height)]
    result = [row[:] for row in pixels]
    for row in range(copy_h):
        for col in range(copy_w):
            result[dst_y + row][dst_x + col] = pixels[src_y + row][src_x + col]
    flat = b"".join(b"".join(row) for row in result)
    return (flat, (src_x, src_y, copy_w, copy_h), (dst_x, dst_y, copy_w, copy_h))


def damaged_copy(rng, base, width, height):
    """Return base with a randomized damage pattern applied."""
    out = bytearray(base)

    def poke(x, y):
        at = (y * width + x) * 3
        out[at:at + 3] = bytes(rng.randrange(256) for _ in range(3))

    pattern = rng.randrange(5)
    if pattern == 0:
        for _ in range(rng.randrange(1, 9)):
            poke(rng.randrange(width), rng.randrange(height))
    elif pattern == 1:
        x = rng.randrange(width)
        for y in range(height):
            poke(x, y)
    elif pattern == 2:
        y = rng.randrange(height)
        for x in range(width):
            poke(x, y)
    elif pattern == 3:
        x0, y0 = rng.randrange(width), rng.randrange(height)
        for y in range(y0, min(height, y0 + rng.randrange(1, 8))):
            for x in range(x0, min(width, x0 + rng.randrange(1, 12))):
                poke(x, y)
    else:
        for y in range(height):
            if rng.random() < 0.4:
                for x in range(rng.randrange(width),
                               rng.randrange(1, width + 1)):
                    poke(x, y)
    return bytes(out)


class ReferenceDiffTests(unittest.TestCase):
    """Anchor the damage arithmetic to independent per-pixel oracles.

    The diff functions promise exact rectangles; these tests pin that
    promise on shapes chosen to stress the column-bound scans (thin
    full-height columns, ragged text, margins around an established span)
    and on randomized frames, so any rewrite of the scanning internals
    must reproduce the output byte for byte.
    """

    def _anchor_frames(self):
        width, height = 120, 90
        rng = random.Random(1207)
        base = bytes(rng.randrange(256) for _ in range(width * height * 3))
        shapes = []
        # A caret-thin full-height column.
        column = bytearray(base)
        for y in range(height):
            at = (y * width + 61) * 3
            column[at:at + 6] = b"\xff\xff\xff\xff\xff\xff"
        shapes.append(bytes(column))
        # A centered block.
        block = bytearray(base)
        for y in range(25, 65):
            for x in range(30, 90):
                at = (y * width + x) * 3
                block[at:at + 3] = b"\x01\x02\x03"
        shapes.append(bytes(block))
        # Ragged line-editor damage: rows of differing widths.
        ragged = bytearray(base)
        for y in range(20, 70):
            span = 10 + (y * 7) % 60
            for x in range(8, 8 + span):
                at = (y * width + x) * 3
                ragged[at:at + 3] = b"\x30\x40\x50"
        shapes.append(bytes(ragged))
        # Damage touching every frame edge.
        edges = bytearray(base)
        for x, y in ((0, 0), (width - 1, 0), (0, height - 1),
                     (width - 1, height - 1)):
            at = (y * width + x) * 3
            edges[at:at + 3] = b"\x99\x99\x99"
        shapes.append(bytes(edges))
        # A full-frame change.
        shapes.append(bytes((value + 1) % 256 for value in base))
        return width, height, base, shapes

    def test_diff_rect_matches_oracle_on_anchor_shapes(self):
        width, height, base, shapes = self._anchor_frames()
        self.assertIsNone(diff_rect(base, bytes(base), width, height))
        for shape in shapes:
            self.assertEqual(diff_rect(base, shape, width, height),
                             reference_rect(base, shape, width, height))

    def test_diff_rects_matches_oracle_on_anchor_shapes(self):
        width, height, base, shapes = self._anchor_frames()
        self.assertEqual(diff_rects(base, bytes(base), width, height), ())
        for shape in shapes:
            for max_rects in (1, 2, 3):
                self.assertEqual(
                    diff_rects(base, shape, width, height, max_rects),
                    reference_rects(base, shape, width, height, max_rects))

    def test_diff_functions_match_oracle_on_randomized_frames(self):
        rng = random.Random(41)
        for _ in range(120):
            width = rng.randrange(1, 70)
            height = rng.randrange(1, 40)
            base = bytes(rng.randrange(256)
                         for _ in range(width * height * 3))
            cur = damaged_copy(rng, base, width, height)
            self.assertEqual(diff_rect(base, cur, width, height),
                             reference_rect(base, cur, width, height))
            max_rects = rng.randrange(1, 4)
            self.assertEqual(
                diff_rects(base, cur, width, height, max_rects),
                reference_rects(base, cur, width, height, max_rects))


class ReferenceShiftTests(unittest.TestCase):
    def test_shift_prediction_matches_oracle(self):
        rng = random.Random(53)
        for _ in range(120):
            width = rng.randrange(2, 26)
            height = rng.randrange(2, 22)
            prev = bytes(rng.randrange(256)
                         for _ in range(width * height * 3))
            if rng.random() < 0.6:
                dx = 0
                dy = rng.choice([-1, 1]) * rng.randrange(1, height)
            else:
                dx = rng.randrange(-(width - 1), width)
                dy = rng.randrange(-(height - 1), height)
                if not dx and not dy:
                    dx = 1
            self.assertEqual(
                _shift_prediction(prev, width, height, dx, dy),
                reference_shift(prev, width, height, dx, dy))


class WireAccountingTests(unittest.TestCase):
    def test_builders_emit_ascii_where_length_equals_encoded_size(self):
        rng = random.Random(99)
        rgb = bytes(rng.randrange(256) for _ in range(24 * 16 * 3))
        name = "/kitty-frame-presenter-407-c0ffee-0"
        pieces = (
            build_direct(rgb, 24, 16, 8, 4, 5),
            build_direct(rgb, 24, 16, 8, 4, 5, in_tmux=True),
            build_frame_edit(rgb, 24, 16, 3, 2, 5),
            build_frame_edit(rgb, 24, 16, 3, 2, 5, in_tmux=True),
            build_full_shm(name, 24, 16, 8, 4, 5),
            build_frame_edit_shm(name, 24, 16, 3, 2, 5),
            build_compose(5, (0, 0, 4, 4), (1, 2, 4, 4)),
            build_compose(5, (0, 0, 4, 4), (1, 2, 4, 4), in_tmux=True),
            wrap_tmux_passthrough("\x1b_Ga=d,d=A\x1b\\"),
        )
        for piece in pieces:
            self.assertTrue(piece.isascii())
            self.assertEqual(len(piece), len(piece.encode("utf-8")))

    def test_wire_bytes_equals_encoded_terminal_output(self):
        rng = random.Random(4)
        width, height = 20, 12
        first = bytes(rng.randrange(256) for _ in range(width * height * 3))
        changed = bytearray(first)
        changed[300:303] = bytes((first[300] + 1) % 256 for _ in range(3))
        for stream in (False, True):
            term = ConsumerTerm()
            presenter = FramePresenter(term, image_id=2, stream=stream)
            try:
                presenter.present(first, width, height, 5, 3)
                presenter.present(bytes(changed), width, height, 5, 3)
                self.assertEqual(
                    presenter.stats.wire_bytes,
                    sum(len(write.encode("utf-8")) for write in term.writes))
            finally:
                presenter.close(discard=True)


class PresenterTests(unittest.TestCase):
    def test_full_then_exact_rect_over_shared_memory(self):
        term = ConsumerTerm()
        presenter = FramePresenter(term, image_id=9)
        try:
            width, height = 32, 20
            first = frame(width, height)
            result = presenter.present(first, width, height, 8, 5)
            self.assertEqual(result.kind, "full")
            changed = bytearray(first)
            for y in range(4, 7):
                for x in range(5, 9):
                    at = (y * width + x) * 3
                    changed[at:at + 3] = b"\x10\x20\x30"
            result = presenter.present(bytes(changed), width, height, 8, 5)
            self.assertEqual(result.kind, "rect")
            self.assertEqual(result.rects, ((5, 4, 4, 3),))
            self.assertEqual(result.pixel_bytes, 4 * 3 * 3)
            self.assertIn("t=s", term.output())
            self.assertNotIn("t=t", term.output())
            self.assertEqual(term.payloads[-1],
                             extract_rect(changed, width, height, result.rects[0]))
        finally:
            presenter.close()

    def test_scroll_compose_sends_only_exposed_strip(self):
        term = ConsumerTerm()
        presenter = FramePresenter(term, image_id=3, enable_scroll=True)
        try:
            width, height = 24, 30
            before = row_frame(width, range(height))
            presenter.present(before, width, height, 8, 5)
            term.writes.clear()
            after = row_frame(width, list(range(6, height)) + [190] * 6)
            result = presenter.present(after, width, height, 8, 5,
                                       scroll=(0, -6))
            self.assertEqual(result.kind, "scroll")
            self.assertEqual(result.rects, ((0, 24, width, 6),))
            self.assertEqual(result.pixel_bytes, width * 6 * 3)
            self.assertIn("a=c", term.output())
            self.assertIn("N=2", term.output())
            self.assertIn("a=f", term.output())
        finally:
            presenter.close()

    def test_bad_scroll_hint_falls_back_without_corruption(self):
        term = ConsumerTerm()
        presenter = FramePresenter(term, enable_scroll=True)
        try:
            width, height = 12, 10
            before = frame(width, height)
            presenter.present(before, width, height, 4, 3)
            term.writes.clear()
            after = frame(width, height, (20, 30, 40))
            result = presenter.present(after, width, height, 4, 3,
                                       scroll=(0, -2))
            self.assertEqual(result.kind, "rect")
            self.assertNotIn("a=c", term.output())
            self.assertEqual(result.rects, ((0, 0, width, height),))
        finally:
            presenter.close()

    def test_pacing_keeps_newest_pending_frame(self):
        now = [10.0]
        term = ConsumerTerm()
        presenter = FramePresenter(term, max_fps=10, clock=lambda: now[0])
        try:
            width, height = 4, 3
            presenter.present(frame(width, height), width, height, 2, 2)
            now[0] += 0.01
            self.assertEqual(presenter.present(
                frame(width, height, (1, 1, 1)), width, height, 2, 2).kind,
                "queued")
            now[0] += 0.01
            self.assertEqual(presenter.present(
                frame(width, height, (2, 2, 2)), width, height, 2, 2).kind,
                "queued")
            self.assertEqual(presenter.stats.frames_dropped, 1)
            now[0] = 10.11
            result = presenter.flush()
            self.assertTrue(result.emitted)
            self.assertEqual(term.payloads[-1], frame(width, height, (2, 2, 2)))
        finally:
            presenter.close()

    def test_invalidate_discards_stale_pending_geometry(self):
        now = [10.0]
        term = ConsumerTerm()
        presenter = FramePresenter(term, max_fps=10, clock=lambda: now[0])
        try:
            width, height = 4, 3
            presenter.present(frame(width, height), width, height, 2, 2)
            now[0] += 0.01
            presenter.present(frame(width, height, (1, 1, 1)),
                              width, height, 2, 2)
            presenter.invalidate()
            now[0] += 1
            self.assertEqual(presenter.flush().kind, "idle")
            self.assertEqual(presenter.stats.frames_dropped, 1)
            result = presenter.present(frame(6, 4), 6, 4, 3, 2)
            self.assertEqual(result.kind, "full")
        finally:
            presenter.close()

    def test_small_damage_skips_automatic_scroll_inference(self):
        term = ConsumerTerm()
        presenter = FramePresenter(term, enable_scroll=True)
        try:
            width, height = 40, 30
            before = frame(width, height)
            presenter.present(before, width, height, 8, 5)
            after = bytearray(before)
            after[(12 * width + 17) * 3:(12 * width + 17) * 3 + 3] = b"\xff\0\0"
            with mock.patch(
                    "kitty_frame_presenter.presenter.detect_vertical_scroll",
                    side_effect=AssertionError("small damage was hashed")):
                result = presenter.present(bytes(after), width, height, 8, 5)
            self.assertEqual(result.kind, "rect")
            self.assertEqual(result.rects, ((17, 12, 1, 1),))
        finally:
            presenter.close()

    def test_saturated_ring_queues_without_emitting_partial_transaction(self):
        term = ConsumerTerm(consume=False)
        presenter = FramePresenter(term, shm_slots=1)
        try:
            width, height = 4, 3
            presenter.present(frame(width, height), width, height, 2, 2)
            count = len(term.writes)
            result = presenter.present(frame(width, height, (2, 2, 2)),
                                       width, height, 2, 2)
            self.assertEqual(result.kind, "queued")
            self.assertEqual(len(term.writes), count)
        finally:
            presenter.close(discard=True)

    def test_write_failure_releases_shared_memory_slots(self):
        class IntermittentTerm:
            """Terminal whose write raises a few times, then recovers."""

            def __init__(self, failures):
                self.failures = failures
                self.inner = ConsumerTerm()

            def write(self, value):
                if self.failures:
                    self.failures -= 1
                    raise BlockingIOError("output would block")
                self.inner.write(value)

        term = IntermittentTerm(failures=3)
        presenter = FramePresenter(term, shm_slots=3)
        try:
            width, height = 4, 3
            rgb = frame(width, height)
            # A transient write failure must not strand ring capacity: the
            # terminal never saw the object names, so nothing can consume
            # them and only a local rollback can free the slots.
            for _ in range(3):
                with self.assertRaises(BlockingIOError):
                    presenter.present(rgb, width, height, 2, 2)
                self.assertEqual(presenter.shm.in_flight, 0)
            for slot in presenter.shm._slots:
                self.assertFalse(
                    os.path.exists("/dev/shm/" + slot.name.lstrip("/")))
            result = presenter.present(rgb, width, height, 2, 2)
            self.assertEqual(result.kind, "full")
            self.assertTrue(result.emitted)
            # The rectangle path shares the same rollback contract.
            changed = bytearray(rgb)
            changed[0:3] = b"\x09\x08\x07"
            term.failures = 1
            with self.assertRaises(BlockingIOError):
                presenter.present(bytes(changed), width, height, 2, 2)
            self.assertEqual(presenter.shm.in_flight, 0)
            result = presenter.present(bytes(changed), width, height, 2, 2)
            self.assertEqual(result.kind, "rect")
        finally:
            presenter.close(discard=True)

    def test_latency_history_is_bounded_but_aggregates_are_complete(self):
        now = [10.0]
        term = ConsumerTerm()
        presenter = FramePresenter(term, stream=True, clock=lambda: now[0])
        try:
            for index in range(300):
                now[0] += 0.001
                pixel = index % 2
                presenter.present(bytes((pixel, pixel, pixel)), 1, 1, 1, 1,
                                  now=now[0])
            self.assertEqual(presenter.stats.latency_samples, 300)
            self.assertEqual(len(presenter.stats.latencies_ms), 256)
            self.assertEqual(presenter.stats.latency_total_ms, 0.0)
            self.assertEqual(presenter.stats.latency_max_ms, 0.0)
        finally:
            presenter.close()


class FrameTapTests(unittest.TestCase):
    """A second consumer of the frames, for a remote encoder.

    The tap exists so a multiplexer can encode for the network without
    re-deriving what the presenter already has, and without the terminal in
    front of the user paying for it.
    """

    def _presenter(self, terminal, **kwargs):
        return FramePresenter(terminal, image_id=9, clock=lambda: 0.0, **kwargs)

    @staticmethod
    def _without_shm_names(text):
        """Blank the shared-memory object names, which are unique per
        presenter by design and so always differ between two instances."""
        return re.sub(r"(t=s[^;]*;)[A-Za-z0-9+/=]+", r"\1<name>", text)

    def test_absent_tap_changes_nothing(self):
        plain, tapped = ConsumerTerm(), ConsumerTerm()
        rgb = bytes(3 * 4 * 3)
        self._presenter(plain).present(rgb, 4, 3, 2, 1)
        self._presenter(tapped, tap=None).present(rgb, 4, 3, 2, 1)
        self.assertEqual(
            self._without_shm_names(plain.output()),
            self._without_shm_names(tapped.output()))

    def test_tap_sees_every_offered_frame(self):
        seen = []
        terminal = ConsumerTerm()
        presenter = self._presenter(terminal, max_fps=1, tap=seen.append)
        first = bytes(3 * 4 * 3)
        second = bytes([1]) * (3 * 4 * 3)
        presenter.present(first, 4, 3, 2, 1, now=0.0)
        # Offered inside the frame interval, so the terminal never sees it -
        # but a remote consumer paces itself and must still be told.
        presenter.present(second, 4, 3, 2, 1, now=0.01)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].rgb, first)
        self.assertEqual(seen[1].rgb, second)
        self.assertEqual(seen[1].width, 4)
        self.assertEqual(seen[1].height, 3)

    def test_tap_receives_the_scroll_hint(self):
        seen = []
        terminal = ConsumerTerm()
        presenter = self._presenter(terminal, tap=seen.append)
        presenter.present(bytes(3 * 4 * 3), 4, 3, 2, 1, scroll=(0, -8))
        self.assertEqual(seen[0].scroll, (0, -8))

    def test_a_failing_tap_is_disconnected_not_propagated(self):
        calls = []

        def broken(frame):
            calls.append(frame)
            raise RuntimeError("the remote encoder fell over")

        terminal = ConsumerTerm()
        presenter = self._presenter(terminal, tap=broken)
        rgb = bytes(3 * 4 * 3)
        # The local terminal must be unharmed by a second consumer failing.
        presenter.present(rgb, 4, 3, 2, 1)
        self.assertTrue(terminal.output())
        self.assertIsNone(presenter.tap)
        self.assertEqual(presenter.tap_failures, 1)
        # And it is not called again.
        presenter.present(rgb, 4, 3, 2, 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
