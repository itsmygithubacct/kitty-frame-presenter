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
    build_direct,
    detect_vertical_scroll,
    diff_band,
    diff_rect,
    diff_rects,
    extract_rect,
)


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
            ring.close()

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


class BuilderTests(unittest.TestCase):
    def test_inline_full_frame_chunks_at_protocol_limit(self):
        rng = random.Random(7)
        data = bytes(rng.randrange(256) for _ in range(100 * 200 * 3))
        value = build_direct(data, 100, 200, 10, 10, 7)
        payloads = re.findall(r";([A-Za-z0-9+/=]+)\x1b\\", value)
        self.assertGreater(len(payloads), 1)
        self.assertTrue(all(len(payload) <= CHUNK for payload in payloads))
        self.assertIn("a=T", value)
        self.assertIn("o=z", value)


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
            presenter.close()


if __name__ == "__main__":
    unittest.main()
