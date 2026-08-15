import base64
import errno
import os
import random
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import kitty_frame_presenter.presenter as presenter_module
from kitty_frame_presenter import (
    CHUNK,
    FramePresenter,
    FrameSocketTap,
    PosixShmRing,
    ShmBusy,
    TappedFrame,
    build_compose,
    build_direct,
    build_frame_edit,
    build_frame_edit_shm,
    build_full_shm,
    detect_vertical_scroll,
    diff_band,
    diff_damage_rects,
    diff_rect,
    diff_rects,
    extract_rect,
    wrap_tmux_passthrough,
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


class ModelTerm:
    """Apply inline full/edit/compose commands to a reference RGB image."""

    APC = re.compile(r"\x1b_G([^;]*);([A-Za-z0-9+/=]*)\x1b\\")

    def __init__(self):
        self.frame = None
        self.width = 0
        self.height = 0
        self._metadata = None
        self._payload = []

    @staticmethod
    def _controls(value):
        return dict(item.split("=", 1) for item in value.split(",") if "=" in item)

    def write(self, value):
        for match in self.APC.finditer(value):
            control = self._controls(match.group(1))
            if control.get("a") == "c":
                self._compose(control)
                continue
            if self._metadata is None:
                self._metadata = control
                self._payload = []
            self._payload.append(match.group(2))
            if control.get("m", "0") == "0":
                self._pixels()
                self._metadata = None
                self._payload = []

    def _pixels(self):
        metadata = self._metadata
        payload = zlib.decompress(base64.b64decode("".join(self._payload)))
        width, height = int(metadata["s"]), int(metadata["v"])
        if metadata["a"] == "T":
            self.width, self.height, self.frame = width, height, payload
            return
        result = bytearray(self.frame)
        x, y = int(metadata["x"]), int(metadata["y"])
        stride, row_bytes = self.width * 3, width * 3
        for row in range(height):
            source = row * row_bytes
            destination = (y + row) * stride + x * 3
            result[destination : destination + row_bytes] = payload[
                source : source + row_bytes
            ]
        self.frame = bytes(result)

    def _compose(self, metadata):
        original = self.frame
        result = bytearray(original)
        sx, sy = int(metadata["X"]), int(metadata["Y"])
        dx, dy = int(metadata["x"]), int(metadata["y"])
        width, height = int(metadata["w"]), int(metadata["h"])
        stride, row_bytes = self.width * 3, width * 3
        for row in range(height):
            source = (sy + row) * stride + sx * 3
            destination = (dy + row) * stride + dx * 3
            result[destination : destination + row_bytes] = original[
                source : source + row_bytes
            ]
        self.frame = bytes(result)


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
                after[at : at + 3] = b"\x01\x02\x03"
        self.assertEqual(diff_rect(before, after, width, height), (11, 17, 18, 6))
        self.assertEqual(diff_band(before, after, width, height), (17, 6))
        self.assertEqual(
            len(extract_rect(after, width, height, (11, 17, 18, 6))), 18 * 6 * 3
        )

    def test_disjoint_row_regions_stay_separate(self):
        width, height = 20, 20
        before = bytearray(frame(width, height))
        after = bytearray(before)
        after[0:3] = b"\xff\0\0"
        at = ((height - 1) * width + (width - 1)) * 3
        after[at : at + 3] = b"\0\xff\0"
        self.assertEqual(
            diff_rects(before, after, width, height), ((0, 0, 1, 1), (19, 19, 1, 1))
        )

    def test_damage_hints_scan_and_shrink_disjoint_regions(self):
        width, height = 20, 12
        before = frame(width, height)
        after = bytearray(before)
        for x, y, color in ((2, 3, b"\xff\0\0"), (17, 9, b"\0\xff\0")):
            at = (y * width + x) * 3
            after[at : at + 3] = color
        self.assertEqual(
            diff_damage_rects(
                before,
                bytes(after),
                width,
                height,
                ((0, 1, 6, 5), (14, 7, 6, 5)),
            ),
            ((2, 3, 1, 1), (17, 9, 1, 1)),
        )
        self.assertEqual(
            diff_damage_rects(before, before, width, height, ()),
            (),
        )

    def test_damage_hints_validate_bounds_shape_and_count(self):
        pixels = frame(2, 2)
        invalid = (
            None,
            (0, 0, 3, 1),
            (0, 0, 0, 1),
            object(),
            ((0, 0, 1, 1),) * 65,
        )
        for damage in invalid:
            with self.subTest(damage=damage), self.assertRaises(ValueError):
                diff_damage_rects(pixels, pixels, 2, 2, damage)

    def test_bad_sizes_are_full_damage_in_compat_helper(self):
        self.assertEqual(diff_band(b"x", b"y", 10, 5), (0, 5))
        with self.assertRaises(ValueError):
            diff_rect(b"x", b"y", 10, 5)

    def test_vertical_scroll_detection(self):
        width, height = 16, 40
        before = row_frame(width, range(height))
        after = row_frame(width, list(range(5, height)) + [200] * 5)
        self.assertEqual(detect_vertical_scroll(before, after, width, height), (0, -5))

    def test_random_damage_matches_naive_bounds_and_reconstructs(self):
        rng = random.Random(1906)
        for case in range(2000):
            width, height = rng.randrange(1, 24), rng.randrange(1, 18)
            before = rng.randbytes(width * height * 3)
            after = bytearray(before)
            points = []
            for _ in range(rng.randrange(0, min(20, width * height) + 1)):
                x, y = rng.randrange(width), rng.randrange(height)
                at = (y * width + x) * 3
                replacement = rng.randbytes(3)
                after[at : at + 3] = replacement
                if replacement != before[at : at + 3]:
                    points.append((x, y))
            after = bytes(after)
            expected = None
            if points:
                left = min(point[0] for point in points)
                right = max(point[0] for point in points) + 1
                top = min(point[1] for point in points)
                bottom = max(point[1] for point in points) + 1
                expected = (left, top, right - left, bottom - top)
            with self.subTest(case=case):
                self.assertEqual(diff_rect(before, after, width, height), expected)
                result = bytearray(before)
                for rect in diff_rects(before, after, width, height, max_rects=3):
                    x, y, rw, rh = rect
                    payload = extract_rect(after, width, height, rect)
                    for row in range(rh):
                        source = row * rw * 3
                        destination = ((y + row) * width + x) * 3
                        result[destination : destination + rw * 3] = payload[
                            source : source + rw * 3
                        ]
                self.assertEqual(bytes(result), after)

    def test_fragmented_bands_retain_the_largest_late_tie_gaps(self):
        width, height = 2, 10
        before = frame(width, height)
        after = bytearray(before)
        for row in range(0, height, 2):
            after[row * width * 3 : row * width * 3 + 3] = b"\xff\0\0"
        self.assertEqual(
            diff_rects(before, after, width, height, max_rects=2),
            ((0, 0, 1, 7), (0, 8, 1, 1)),
        )

    def test_damage_helpers_normalize_invalid_public_input(self):
        with self.assertRaises(ValueError):
            diff_rect(b"\0\0\0", b"\0\0\0", 1.0, 1)
        with self.assertRaises(ValueError):
            diff_rects(b"\0\0\0", b"\0\0\0", 1, 1, max_rects=True)
        with self.assertRaises(ValueError):
            extract_rect(b"\0\0\0", 1, 1, (0, 0, 2, 1))
        with self.assertRaises(ValueError):
            extract_rect(b"\0\0\0", 1, 1, (0, 0, 1))
        self.assertIsNone(
            detect_vertical_scroll(frame(8, 8), frame(8, 8), 8, 8, max_shift=0)
        )

    def test_scroll_detection_bounds_and_unrelated_frames(self):
        width, height = 40, 20
        before = row_frame(width, range(height))
        after = row_frame(width, [*range(2, height), 200, 201])
        self.assertIsNone(
            detect_vertical_scroll(before, after, width, height, max_shift=0)
        )
        with self.assertRaises(ValueError):
            detect_vertical_scroll(before, after, width, height, max_shift=-1)
        unrelated = random.Random(511).randbytes(width * height * 3)
        self.assertIsNone(
            detect_vertical_scroll(before, unrelated, width, height, max_shift=3)
        )

    def test_wide_row_scanning_and_horizontal_prediction_are_exact(self):
        width, height = 100, 4
        before = frame(width, height)
        after = bytearray(before)
        at = (2 * width + 73) * 3
        after[at : at + 3] = b"\1\2\3"
        self.assertEqual(diff_rect(before, after, width, height), (73, 2, 1, 1))

        shifted, source, destination = presenter_module._shift_prediction(
            before, width, height, 3, 0
        )
        self.assertEqual(source, (0, 0, 97, 4))
        self.assertEqual(destination, (3, 0, 97, 4))
        self.assertEqual(len(shifted), len(before))


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
            with (
                mock.patch("mmap.mmap", side_effect=OSError("injected")),
                self.assertRaises(OSError),
            ):
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

    def test_name_collision_never_unlinks_an_object_the_ring_did_not_create(self):
        ring = PosixShmRing(1, prefix="kitty-frame-presenter-collision-test")
        name = ring._slots[0].name
        descriptor = ring._open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL)
        os.close(descriptor)
        try:
            with self.assertRaises(OSError):
                ring.put_many((b"ours",))
            self.assertTrue(os.path.exists("/dev/shm/" + name.lstrip("/")))
            self.assertEqual(ring.in_flight, 0)
        finally:
            ring._unlink(name)
            ring.close(discard=True)

    def test_slot_count_and_prefix_are_bounded(self):
        for invalid in (0, -1, True, 65):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PosixShmRing(invalid)
        ring = PosixShmRing(1, prefix="x" * 1000 + "/unsafe")
        try:
            self.assertLessEqual(len(ring._slots[0].name.encode("ascii")), 255)
            self.assertNotIn("/", ring._slots[0].name[1:])
        finally:
            ring.close()

    def test_close_releases_every_slot_even_when_one_unlink_fails(self):
        ring = PosixShmRing(2, prefix="kitty-frame-presenter-close-failure-test")
        names = ring.put_many((b"first", b"second"))
        original = ring._unlink

        def fail_first(name, missing_ok=True):
            if name == names[0]:
                raise OSError("injected unlink failure")
            return original(name, missing_ok)

        try:
            with (
                mock.patch.object(ring, "_unlink", side_effect=fail_first),
                self.assertRaises(OSError),
            ):
                ring.close(discard=True)
            self.assertTrue(ring.closed)
            self.assertEqual(sum(slot.busy for slot in ring._slots), 0)
        finally:
            try:
                original(names[0])
            except OSError:
                pass

    def test_context_reaps_consumed_names_and_closed_ring_rejects_work(self):
        with PosixShmRing(1, prefix="kitty-frame-presenter-context-test") as ring:
            self.assertEqual(ring.put_many(()), ())
            name = ring.put_many((b"consumed",))[0]
            os.unlink("/dev/shm/" + name.lstrip("/"))
            self.assertEqual(ring.reap(), 1)
            self.assertEqual(ring.in_flight, 0)
        self.assertEqual(ring.reap(), 0)
        ring.close()
        with self.assertRaises(RuntimeError):
            ring.put_many((b"late",))

    def test_multi_payload_validation_failure_rolls_back_prior_objects(self):
        ring = PosixShmRing(2, prefix="kitty-frame-presenter-rollback-test")
        paths = ["/dev/shm/" + slot.name.lstrip("/") for slot in ring._slots]
        try:
            with self.assertRaises(ValueError):
                ring.put_many((b"made", b""))
            self.assertEqual(ring.in_flight, 0)
            self.assertFalse(any(os.path.exists(path) for path in paths))
        finally:
            ring.close(discard=True)

    def test_reap_propagates_unexpected_probe_errors(self):
        ring = PosixShmRing(1, prefix="kitty-frame-presenter-reap-error-test")
        try:
            ring.put_many((b"pending",))
            with (
                mock.patch.object(
                    ring, "_open", side_effect=OSError(errno.EACCES, "injected")
                ),
                self.assertRaises(OSError),
            ):
                ring.reap()
        finally:
            ring.close(discard=True)

    def test_missing_libc_and_non_string_prefix_are_normalized(self):
        with self.assertRaises(ValueError):
            PosixShmRing(1, prefix=None)
        with (
            mock.patch("ctypes.util.find_library", return_value=None),
            self.assertRaises(OSError) as caught,
        ):
            PosixShmRing(1)
        self.assertEqual(caught.exception.errno, errno.ENOSYS)

    def test_unlink_missing_ok_and_error_paths(self):
        ring = PosixShmRing(1, prefix="kitty-frame-presenter-unlink-test")
        try:
            with (
                mock.patch.object(ring._libc, "shm_unlink", return_value=-1),
                mock.patch("ctypes.get_errno", return_value=errno.ENOENT),
            ):
                ring._unlink(ring._slots[0].name)
                with self.assertRaises(OSError):
                    ring._unlink(ring._slots[0].name, missing_ok=False)
        finally:
            ring.close()


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

    def test_chunked_frame_edit_round_trips_and_retains_routing(self):
        data = random.Random(9).randbytes(100 * 200 * 3)
        value = build_frame_edit(data, 100, 200, 7, 11, 23)
        commands = re.findall(r"\x1b_G([^;]*);([A-Za-z0-9+/=]+)\x1b\\", value)
        self.assertGreater(len(commands), 1)
        self.assertTrue(all(len(payload) <= CHUNK for _control, payload in commands))
        self.assertTrue(all("a=f" in control for control, _payload in commands))
        self.assertTrue(all("i=23" in control for control, _payload in commands))
        self.assertTrue(all("r=1" in control for control, _payload in commands))
        payload = base64.b64decode("".join(part for _control, part in commands))
        self.assertEqual(zlib.decompress(payload), data)

    def test_builders_reject_invalid_protocol_fields(self):
        data = b"\0\0\0"
        invalid = (
            lambda: build_direct(data, 1, 1, 1, 1, 0),
            lambda: build_direct(data, 1, 1, 1.5, 1, 1),
            lambda: build_direct(data, 2, 1, 1, 1, 1),
            lambda: build_frame_edit(data, 1, 1, -1, 0, 1),
            lambda: build_frame_edit(data, 1, 1, 0, 0, "1,q=0"),
            lambda: build_full_shm("not-posix", 1, 1, 1, 1, 1),
            lambda: build_frame_edit_shm("/bad/name", 1, 1, 0, 0, 1),
            lambda: build_compose(1, (0, 0, 1, 1), (0, 0, 2, 1)),
            lambda: build_compose(1, (-1, 0, 1, 1), (0, 0, 1, 1)),
        )
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()

    def test_builders_keep_empty_sentinel_and_reject_bad_payloads_and_names(self):
        self.assertEqual(build_direct(b"", 0, 0, 0, 0, 0), "")
        self.assertEqual(build_frame_edit(b"", 0, 0, 0, 0, 0), "")
        invalid = (
            lambda: build_direct(b"\0\0", 1, 1, 1, 1, 1),
            lambda: build_frame_edit(b"\0\0", 1, 1, 0, 0, 1),
            lambda: build_full_shm("/snowman-\N{SNOWMAN}", 1, 1, 1, 1, 1),
            lambda: build_full_shm("/" + "x" * 256, 1, 1, 1, 1, 1),
            lambda: build_compose(
                1,
                ((1 << 32) - 1, 0, 1, 1),
                ((1 << 32) - 1, 0, 1, 1),
            ),
            lambda: extract_rect(b"\0\0", 1, 1, (0, 0, 1, 1)),
            lambda: diff_rect(object(), b"\0\0\0", 1, 1),
        )
        for operation in invalid:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation()


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
                    changed[at : at + 3] = b"\x10\x20\x30"
            result = presenter.present(bytes(changed), width, height, 8, 5)
            self.assertEqual(result.kind, "rect")
            self.assertEqual(result.rects, ((5, 4, 4, 3),))
            self.assertEqual(result.pixel_bytes, 4 * 3 * 3)
            self.assertIn("t=s", term.output())
            self.assertNotIn("t=t", term.output())
            self.assertEqual(
                term.payloads[-1], extract_rect(changed, width, height, result.rects[0])
            )
        finally:
            presenter.close()

    def test_trusted_damage_bypasses_full_frame_diff_and_emits_multiple_rects(self):
        width, height = 12, 8
        before = frame(width, height)
        after = bytearray(before)
        changes = ((2, 1, b"\xff\0\0"), (10, 6, b"\0\xff\0"))
        for x, y, color in changes:
            at = (y * width + x) * 3
            after[at : at + 3] = color
        terminal = ModelTerm()
        presenter = FramePresenter(
            terminal,
            stream=True,
            enable_scroll=False,
            stream_warmup_seconds=0,
            stream_keyframe_seconds=0,
        )
        try:
            presenter.present(before, width, height, 6, 4, now=1.0)
            with mock.patch.object(
                presenter_module,
                "diff_rect",
                side_effect=AssertionError("full-frame diff was used"),
            ):
                result = presenter.present(
                    bytes(after),
                    width,
                    height,
                    6,
                    4,
                    damage=((0, 0, 5, 4), (8, 4, 4, 4)),
                    now=2.0,
                )
            self.assertEqual(result.kind, "rect")
            self.assertEqual(result.rects, ((2, 1, 1, 1), (10, 6, 1, 1)))
            self.assertEqual(terminal.frame, bytes(after))
        finally:
            presenter.close()

    def test_empty_damage_marks_an_unchanged_complete_frame(self):
        pixels = frame(4, 3)
        presenter = FramePresenter(
            ConsumerTerm(),
            stream=True,
            stream_warmup_seconds=0,
            stream_keyframe_seconds=0,
        )
        try:
            presenter.present(pixels, 4, 3, 2, 2, now=1.0)
            with mock.patch.object(
                presenter_module,
                "diff_rect",
                side_effect=AssertionError("full-frame diff was used"),
            ):
                result = presenter.present(pixels, 4, 3, 2, 2, damage=(), now=2.0)
            self.assertEqual(result.kind, "unchanged")
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
            result = presenter.present(after, width, height, 8, 5, scroll=(0, -6))
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
            result = presenter.present(after, width, height, 4, 3, scroll=(0, -2))
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
            self.assertEqual(
                presenter.present(
                    frame(width, height, (1, 1, 1)), width, height, 2, 2
                ).kind,
                "queued",
            )
            now[0] += 0.01
            self.assertEqual(
                presenter.present(
                    frame(width, height, (2, 2, 2)), width, height, 2, 2
                ).kind,
                "queued",
            )
            self.assertEqual(presenter.stats.frames_dropped, 1)
            now[0] = 10.11
            result = presenter.flush()
            self.assertTrue(result.emitted)
            self.assertEqual(term.payloads[-1], frame(width, height, (2, 2, 2)))
        finally:
            presenter.close()

    def test_pacing_accumulates_damage_across_dropped_frames(self):
        now = [10.0]
        width, height = 8, 6
        current = frame(width, height)
        first = bytearray(current)
        first[(1 * width + 1) * 3 : (1 * width + 1) * 3 + 3] = b"\xff\0\0"
        newest = bytearray(first)
        newest[(4 * width + 6) * 3 : (4 * width + 6) * 3 + 3] = b"\0\xff\0"
        terminal = ModelTerm()
        presenter = FramePresenter(
            terminal,
            stream=True,
            max_fps=10,
            stream_warmup_seconds=0,
            stream_keyframe_seconds=0,
            clock=lambda: now[0],
        )
        try:
            presenter.present(current, width, height, 4, 3)
            now[0] = 10.01
            presenter.present(
                bytes(first), width, height, 4, 3, damage=(1, 1, 1, 1)
            )
            now[0] = 10.02
            presenter.present(
                bytes(newest), width, height, 4, 3, damage=(6, 4, 1, 1)
            )
            self.assertEqual(
                presenter._pending.damage,
                ((1, 1, 1, 1), (6, 4, 1, 1)),
            )
            now[0] = 10.11
            result = presenter.flush()
            self.assertEqual(result.rects, ((1, 1, 1, 1), (6, 4, 1, 1)))
            self.assertEqual(terminal.frame, bytes(newest))
        finally:
            presenter.close()

    def test_dropped_unknown_damage_forces_safe_full_frame_diff(self):
        now = [1.0]
        presenter = FramePresenter(
            ConsumerTerm(), stream=True, max_fps=10, clock=lambda: now[0]
        )
        try:
            presenter.present(frame(2, 2), 2, 2, 1, 1)
            now[0] = 1.01
            presenter.present(
                frame(2, 2, (1, 1, 1)), 2, 2, 1, 1, damage=(0, 0, 2, 2)
            )
            now[0] = 1.02
            presenter.present(frame(2, 2, (2, 2, 2)), 2, 2, 1, 1)
            self.assertIsNone(presenter._pending.damage)
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
            presenter.present(frame(width, height, (1, 1, 1)), width, height, 2, 2)
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
            after[(12 * width + 17) * 3 : (12 * width + 17) * 3 + 3] = b"\xff\0\0"
            with mock.patch(
                "kitty_frame_presenter.presenter.detect_vertical_scroll",
                side_effect=AssertionError("small damage was hashed"),
            ):
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
            result = presenter.present(
                frame(width, height, (2, 2, 2)), width, height, 2, 2
            )
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
                presenter.present(bytes((pixel, pixel, pixel)), 1, 1, 1, 1, now=now[0])
            self.assertEqual(presenter.stats.latency_samples, 300)
            self.assertEqual(len(presenter.stats.latencies_ms), 256)
            self.assertEqual(presenter.stats.latency_total_ms, 0.0)
            self.assertEqual(presenter.stats.latency_max_ms, 0.0)
        finally:
            presenter.close()

    def test_single_slot_scroll_falls_back_instead_of_queueing_forever(self):
        width, height = 10, 10
        before = row_frame(width, range(height))
        predicted, _source, _destination = presenter_module._shift_prediction(
            before, width, height, 0, -2
        )
        after = bytearray(predicted)
        after[: width * 3] = b"\xfa\xfa\xfa" * width
        after[-width * 3 :] = b"\xfb\xfb\xfb" * width
        presenter = FramePresenter(
            ConsumerTerm(), shm_slots=1, enable_scroll=True, clock=lambda: 1.0
        )
        try:
            presenter.present(before, width, height, 5, 5, now=1.0)
            result = presenter.present(
                bytes(after), width, height, 5, 5, scroll=(0, -2), now=2.0
            )
            self.assertTrue(result.emitted)
            self.assertEqual(result.kind, "rect")
            self.assertEqual(presenter.flush(now=3.0).kind, "idle")
        finally:
            presenter.close(discard=True)

    def test_shared_memory_backpressure_has_a_future_retry_deadline(self):
        terminal = ConsumerTerm(consume=False)
        presenter = FramePresenter(terminal, shm_slots=1, clock=lambda: 10.0)
        try:
            presenter.present(frame(2, 2), 2, 2, 1, 1, now=10.0)
            result = presenter.present(frame(2, 2, (1, 1, 1)), 2, 2, 1, 1, now=11.0)
            self.assertEqual(result.kind, "queued")
            self.assertGreater(presenter.next_deadline, 11.0)
            with mock.patch.object(
                presenter.shm,
                "put_many",
                side_effect=AssertionError("retried before deadline"),
            ):
                self.assertEqual(presenter.flush(now=11.001).kind, "queued")
            name = presenter.shm._slots[0].name
            os.unlink("/dev/shm/" + name.lstrip("/"))
            self.assertTrue(presenter.flush(now=11.01).emitted)
        finally:
            presenter.close(discard=True)

    def test_stream_output_reconstructs_every_randomized_frame(self):
        rng = random.Random(4411)
        width, height = 24, 18
        terminal = ModelTerm()
        presenter = FramePresenter(
            terminal,
            stream=True,
            enable_scroll=True,
            stream_warmup_seconds=0,
            stream_keyframe_seconds=0,
            clock=lambda: 0.0,
        )
        current = rng.randbytes(width * height * 3)
        try:
            presenter.present(current, width, height, 6, 3, now=0.0)
            self.assertEqual(terminal.frame, current)
            for case in range(500):
                if case % 4 == 0:
                    dy = -1 if case % 8 else 1
                    offered, _source, _destination = presenter_module._shift_prediction(
                        current, width, height, 0, dy
                    )
                    offered = bytearray(offered)
                    exposed = 0 if dy > 0 else height - 1
                    start = exposed * width * 3
                    offered[start : start + width * 3] = rng.randbytes(width * 3)
                    offered = bytes(offered)
                    scroll = (0, dy)
                elif case % 7 == 0:
                    offered = current
                    scroll = None
                else:
                    offered = bytearray(current)
                    x, y = rng.randrange(width), rng.randrange(height)
                    at = (y * width + x) * 3
                    offered[at : at + 3] = rng.randbytes(3)
                    offered, scroll = bytes(offered), None
                result = presenter.present(
                    offered,
                    width,
                    height,
                    6,
                    3,
                    scroll=scroll,
                    now=(case + 1) / 1000,
                )
                with self.subTest(case=case, kind=result.kind):
                    self.assertEqual(terminal.frame, offered)
                current = offered
        finally:
            presenter.close()

    def test_public_geometry_time_and_lifecycle_are_strict(self):
        terminal = ConsumerTerm()
        constructors = (
            lambda: FramePresenter(terminal, image_id=1.5, stream=True),
            lambda: FramePresenter(terminal, image_id=1 << 32, stream=True),
            lambda: FramePresenter(terminal, stream=True, max_fps=float("inf")),
            lambda: FramePresenter(terminal, stream=True, clock=lambda: float("nan")),
            lambda: FramePresenter(object(), stream=True),
        )
        for constructor in constructors:
            with self.subTest(constructor=constructor), self.assertRaises(ValueError):
                constructor()

        presenter = FramePresenter(terminal, stream=True)
        invalid = (
            lambda: presenter.present(b"\0\0\0", 1, 1, 1.5, 1),
            lambda: presenter.present(b"\0\0\0", 1, 1, 1, 1, scroll=(1,)),
            lambda: presenter.present(b"\0\0\0", 1, 1, 1, 1, scroll=(0.5, 1)),
            lambda: presenter.present(b"\0\0\0", 1, 1, 1, 1, now=float("nan")),
            lambda: presenter.present(
                b"\0\0\0", 1, 1, 1, 1, damage=(0, 0, 2, 1)
            ),
        )
        try:
            for operation in invalid:
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    operation()
        finally:
            presenter.close()
        with self.assertRaises(RuntimeError):
            presenter.present(b"\0\0\0", 1, 1, 1, 1)
        self.assertEqual(presenter.flush().kind, "idle")

    def test_failed_terminal_write_does_not_advance_presenter_state(self):
        class BrokenTerminal:
            def write(self, _value):
                raise BrokenPipeError("injected")

        presenter = FramePresenter(BrokenTerminal(), stream=True)
        try:
            with self.assertRaises(BrokenPipeError):
                presenter.present(b"\0\0\0", 1, 1, 1, 1, now=1.0)
            self.assertEqual(presenter.stats.frames_emitted, 0)
            self.assertIsNone(presenter._previous)
            presenter.terminal = ConsumerTerm()
            self.assertEqual(
                presenter.present(b"\0\0\0", 1, 1, 1, 1, now=2.0).kind,
                "full",
            )
        finally:
            presenter.close()

    def test_close_accounts_for_a_pending_frame_and_is_idempotent(self):
        presenter = FramePresenter(
            ConsumerTerm(), stream=True, max_fps=1, clock=lambda: 1.0
        )
        presenter.present(b"\0\0\0", 1, 1, 1, 1, now=1.0)
        presenter.present(b"\1\1\1", 1, 1, 1, 1, now=1.1)
        self.assertEqual(presenter.stats.frames_dropped, 0)
        presenter.close()
        self.assertEqual(presenter.stats.frames_dropped, 1)
        presenter.close()
        self.assertEqual(presenter.stats.frames_dropped, 1)

    def test_stream_warmup_and_periodic_keyframes_follow_deadlines(self):
        terminal = ConsumerTerm()
        presenter = FramePresenter(
            terminal,
            stream=True,
            stream_warmup_seconds=2,
            stream_keyframe_seconds=5,
            clock=lambda: 0.0,
        )
        try:
            self.assertEqual(
                presenter.present(b"\0\0\0", 1, 1, 1, 1, now=0).kind,
                "full",
            )
            self.assertEqual(
                presenter.present(b"\1\1\1", 1, 1, 1, 1, now=1).kind,
                "full",
            )
            self.assertEqual(
                presenter.present(b"\2\2\2", 1, 1, 1, 1, now=2).kind,
                "rect",
            )
            self.assertEqual(
                presenter.present(b"\3\3\3", 1, 1, 1, 1, now=6).kind,
                "full",
            )
            self.assertEqual(presenter.stats.full_frames, 3)
        finally:
            presenter.close()

    def test_automatic_and_payload_free_scroll_paths_reconstruct(self):
        width, height = 16, 30
        before = row_frame(width, range(height))
        after = row_frame(width, list(range(4, height)) + [200] * 4)
        terminal = ModelTerm()
        presenter = FramePresenter(
            terminal,
            stream=True,
            enable_scroll=True,
            stream_warmup_seconds=0,
            stream_keyframe_seconds=0,
            clock=lambda: 0.0,
        )
        try:
            presenter.present(before, width, height, 8, 5, now=0.0)
            result = presenter.present(after, width, height, 8, 5, now=1.0)
            self.assertEqual(result.kind, "scroll")
            self.assertEqual(terminal.frame, after)

            predicted, _source, _destination = presenter_module._shift_prediction(
                after, width, height, 0, 1
            )
            result = presenter.present(
                predicted, width, height, 8, 5, scroll=(0, 1), now=2.0
            )
            self.assertEqual(result.kind, "scroll")
            self.assertEqual(result.pixel_bytes, 0)
            self.assertEqual(result.rects, ())
            self.assertEqual(terminal.frame, predicted)
        finally:
            presenter.close()

    def test_invalid_scroll_shift_falls_back_to_exact_damage(self):
        width, height = 4, 4
        terminal = ConsumerTerm()
        presenter = FramePresenter(
            terminal,
            stream=True,
            enable_scroll=True,
            stream_warmup_seconds=0,
            stream_keyframe_seconds=0,
        )
        try:
            presenter.present(frame(width, height), width, height, 2, 2, now=1.0)
            result = presenter.present(
                frame(width, height, (1, 1, 1)),
                width,
                height,
                2,
                2,
                scroll=(width, 0),
                now=2.0,
            )
            self.assertEqual(result.kind, "rect")
            self.assertEqual(result.rects, ((0, 0, width, height),))
        finally:
            presenter.close()

    def test_context_lifecycle_unchanged_and_empty_write_paths(self):
        terminal = ConsumerTerm()
        with FramePresenter(
            terminal,
            stream=True,
            stream_warmup_seconds=0,
            stream_keyframe_seconds=0,
        ) as presenter:
            self.assertIsNone(presenter.next_deadline)
            self.assertEqual(presenter._write(""), 0)
            presenter.present(b"\0\0\0", 1, 1, 1, 1, now=1.0)
            self.assertEqual(
                presenter.present(b"\0\0\0", 1, 1, 1, 1, now=2.0).kind,
                "unchanged",
            )
            self.assertEqual(presenter.stats.frames_unchanged, 1)
        self.assertTrue(presenter.closed)

    def test_numeric_limits_reject_non_finite_deadlines(self):
        terminal = ConsumerTerm()
        constructors = (
            lambda: FramePresenter(terminal, stream=True, max_fps=5e-324),
            lambda: FramePresenter(terminal, stream=True, clock=1),
            lambda: FramePresenter(terminal, stream=True, max_fps=True),
        )
        for constructor in constructors:
            with self.subTest(constructor=constructor), self.assertRaises(ValueError):
                constructor()

        presenter = FramePresenter(terminal, stream=True)
        try:
            for invalid in (-1.0, float(1 << 64) / 1_000_000):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    presenter.present(b"\0\0\0", 1, 1, 1, 1, now=invalid)
            with self.assertRaises(ValueError):
                presenter.present(b"\0\0", 1, 1, 1, 1, now=1.0)
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
            self._without_shm_names(tapped.output()),
        )

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

    def test_socket_tap_carries_session_geometry_and_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "frame.tap")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(1)
            listener.settimeout(2)
            tap = FrameSocketTap(path, "pane-session", retry_seconds=0.05)
            presenter = self._presenter(ConsumerTerm(), tap=tap)
            rgb = bytes(range(36))
            try:
                presenter.present(rgb, 4, 3, 2, 1, scroll=(0, -8), now=1.25)
                source, _ = listener.accept()
                with source:
                    source.settimeout(2)
                    received = bytearray()
                    while len(received) < 112 + len(rgb):
                        block = source.recv(112 + len(rgb) - len(received))
                        self.assertTrue(block)
                        received.extend(block)
                fields = struct.unpack("!4sHH64sIIIIiiQQ", received[:112])
                self.assertEqual(fields[0:3], (b"KFT1", 1, 0))
                self.assertEqual(fields[3].rstrip(b"\0"), b"pane-session")
                self.assertEqual(fields[4:8], (4, 3, 2, 1))
                self.assertEqual(fields[8:10], (0, -8))
                self.assertEqual(fields[10], 1_250_000)
                self.assertEqual(fields[11], len(rgb))
                self.assertEqual(bytes(received[112:]), rgb)
            finally:
                presenter.close()
                tap.close()
                listener.close()

    def test_absent_socket_never_breaks_local_presentation(self):
        with tempfile.TemporaryDirectory() as directory:
            tap = FrameSocketTap(
                os.path.join(directory, "absent.tap"),
                "pane-session",
                retry_seconds=0.05,
                send_timeout=0.05,
            )
            terminal = ConsumerTerm()
            presenter = self._presenter(terminal, tap=tap)
            try:
                presenter.present(bytes(36), 4, 3, 2, 1)
                deadline = time.monotonic() + 1
                while not tap.frames_dropped and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(terminal.output())
                self.assertGreaterEqual(tap.frames_dropped, 1)
            finally:
                presenter.close()
                tap.close()

    def test_socket_tap_rejects_unencodable_geometry_before_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            tap = FrameSocketTap(
                os.path.join(directory, "absent.tap"),
                "pane-session",
                retry_seconds=0.05,
                send_timeout=0.05,
            )
            try:
                invalid = (
                    {"columns": 1 << 40},
                    {"width": 1.0},
                    {"rgb": object()},
                    {"offered_at": "1"},
                    {"offered_at": float("nan")},
                    {"scroll": (1,)},
                    {"scroll": (0, 1 << 40)},
                    {"scroll": (0.0, 1)},
                )
                for changes in invalid:
                    values = {
                        "rgb": b"\0\0\0",
                        "width": 1,
                        "height": 1,
                        "columns": 1,
                        "rows": 1,
                        "scroll": None,
                        "content_key": None,
                        "offered_at": 1.0,
                    }
                    values.update(changes)
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        tap(TappedFrame(**values))
                self.assertEqual(tap.frames_offered, 0)
            finally:
                tap.close()

    def test_socket_configuration_is_finite_bounded_and_path_safe(self):
        invalid = (
            ("relative", "session", {}),
            ("/tmp/" + "x" * 108, "session", {}),
            ("/tmp/tap", "bad/session", {}),
            ("/tmp/tap", "snowman-☃", {}),
            ("/tmp/tap", "session", {"send_timeout": float("inf")}),
            ("/tmp/tap", "session", {"send_timeout": 61}),
            ("/tmp/tap", "session", {"retry_seconds": float("nan")}),
            ("/tmp/tap", "session", {"retry_seconds": 3601}),
            ("/tmp/\ud800", "session", {}),
        )
        for path, session, options in invalid:
            with (
                self.subTest(path=path, session=session, options=options),
                self.assertRaises(ValueError),
            ):
                FrameSocketTap(path, session, **options)

    def test_socket_tap_snapshots_mutable_pixel_input(self):
        with (
            mock.patch.object(threading.Thread, "start"),
            mock.patch.object(threading.Thread, "join"),
        ):
            tap = FrameSocketTap("/tmp/absent-kfp-snapshot", "session")
            pixels = bytearray(b"\1\2\3")
            frame_value = TappedFrame(pixels, 1, 1, 1, 1, None, None, 1.0)
            tap(frame_value)
            pixels[:] = b"\4\5\6"
            self.assertIsInstance(tap._pending.rgb, bytes)
            self.assertEqual(tap._pending.rgb, b"\1\2\3")
            tap.close()

    def test_socket_tap_context_closes_and_discards_pending_frame(self):
        frame_value = TappedFrame(b"\1\2\3", 1, 1, 1, 1, None, None, 1.0)
        with (
            mock.patch.object(threading.Thread, "start"),
            mock.patch.object(threading.Thread, "join"),
        ):
            with FrameSocketTap("/tmp/absent-kfp-context", "session") as tap:
                tap(frame_value)
                header = tap._encode_header(frame_value)
                fields = tap._HEADER.unpack(header)
                self.assertEqual(fields[8:10], (-(1 << 31), -(1 << 31)))
            self.assertEqual(tap.frames_dropped, 1)
            offered = tap.frames_offered
            tap(frame_value)
            tap.close()
            self.assertEqual(tap.frames_offered, offered)

    def test_socket_tap_rejects_foreign_peer_and_honors_retry_deadline(self):
        fake_socket = mock.Mock(spec=socket.socket)
        with (
            mock.patch.object(threading.Thread, "start"),
            mock.patch.object(threading.Thread, "join"),
            mock.patch.object(socket, "socket", return_value=fake_socket),
            mock.patch.object(FrameSocketTap, "_peer_is_owner", return_value=False),
            mock.patch.object(time, "monotonic", return_value=10.0),
        ):
            tap = FrameSocketTap("/tmp/absent-kfp-peer", "session", retry_seconds=0.5)
            try:
                self.assertFalse(tap._connect())
                self.assertEqual(tap.failures, 1)
                fake_socket.close.assert_called_once()
                self.assertFalse(tap._connect())
                self.assertEqual(tap.failures, 1)
            finally:
                tap.close()

    def test_socket_worker_recovers_from_send_failure(self):
        with (
            mock.patch.object(threading.Thread, "start"),
            mock.patch.object(threading.Thread, "join"),
        ):
            tap = FrameSocketTap("/tmp/absent-kfp-send", "session")
            fake_socket = mock.Mock(spec=socket.socket)

            def fail_send(_value):
                tap._closed = True
                raise OSError("injected send failure")

            fake_socket.sendall.side_effect = fail_send
            tap._socket = fake_socket
            tap(TappedFrame(b"\1\2\3", 1, 1, 1, 1, None, None, 1.0))
            tap._run()
            self.assertEqual(tap.failures, 1)
            self.assertEqual(tap.frames_dropped, 1)
            self.assertIsNone(tap._socket)
            fake_socket.close.assert_called_once()

    def test_environment_factory_requires_complete_safe_configuration(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(FrameSocketTap.from_environment())
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {
                    "KITTY_PTY_BROKER_SESSION": "0123456789abcdef",
                    "KILIX_FRAME_TAP_DIRECTORY": directory,
                },
                clear=True,
            ),
        ):
            tap = FrameSocketTap.from_environment()
            try:
                self.assertEqual(
                    tap.path, os.path.join(directory, "0123456789abcdef.tap")
                )
            finally:
                tap.close()


if __name__ == "__main__":
    unittest.main()
