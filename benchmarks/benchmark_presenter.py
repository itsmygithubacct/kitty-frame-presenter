#!/usr/bin/env python3
"""Repeatable damage, composition, encoding, and transport benchmark."""

from __future__ import annotations

import json
import os
import random
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import kitty_frame_presenter.presenter as implementation
from kitty_frame_presenter import (
    FramePresenter,
    PosixShmRing,
    build_direct,
    build_frame_edit,
    diff_rect,
    diff_rects,
)


def median_ns(
    operation: Callable[[], object], iterations: int, rounds: int = 7
) -> float:
    samples = []
    for _ in range(rounds):
        started = time.perf_counter_ns()
        for _iteration in range(iterations):
            operation()
        samples.append((time.perf_counter_ns() - started) / iterations)
    return statistics.median(samples)


class NullTerminal:
    def write(self, _value: str) -> None:
        pass


def main() -> None:
    width, height = 1024, 640
    rng = random.Random(0x4B4650)
    previous = rng.randbytes(width * height * 3)
    unchanged = bytes(bytearray(previous))
    small = bytearray(previous)
    for row in range(20):
        start = ((height // 2 + row) * width + width // 2) * 3
        small[start : start + 32 * 3] = b"\x12\x34\x56" * 32
    small_bytes = bytes(small)
    full = bytes(value ^ 0xFF for value in previous)
    two = bytearray(previous)
    two[: 64 * 3] = b"\xaa\xbb\xcc" * 64
    two[-64 * 3 :] = b"\x11\x22\x33" * 64
    two_bytes = bytes(two)
    fragmented = bytearray(previous)
    for row in range(0, height, 2):
        fragmented[row * width * 3 : row * width * 3 + 3] = b"\xde\xad\xbe"
    fragmented_bytes = bytes(fragmented)
    edit = previous[(height // 2) * width * 3 : (height // 2 + 64) * width * 3]
    sequence = "A" * (2 * 1024 * 1024)
    terminal = NullTerminal()
    presenter = FramePresenter(terminal, stream=True)
    ring = PosixShmRing(1, prefix="kitty-frame-presenter-benchmark")

    def shift() -> None:
        implementation._shift_prediction(previous, width, height, 0, -48)

    def write_count() -> None:
        if presenter._write(sequence) != len(sequence):
            raise AssertionError("wire count changed")

    def shm_roundtrip() -> None:
        name = ring.put_many((previous,))[0]
        os.unlink("/dev/shm/" + name.lstrip("/"))
        ring.reap()

    try:
        results = {
            "diff_unchanged_ns": median_ns(
                lambda: diff_rect(previous, unchanged, width, height), 100
            ),
            "diff_small_ns": median_ns(
                lambda: diff_rect(previous, small_bytes, width, height), 30
            ),
            "diff_full_ns": median_ns(
                lambda: diff_rect(previous, full, width, height), 20
            ),
            "diff_rects_two_ns": median_ns(
                lambda: diff_rects(previous, two_bytes, width, height), 20
            ),
            "diff_rects_fragmented_ns": median_ns(
                lambda: diff_rects(previous, fragmented_bytes, width, height), 3
            ),
            "shift_vertical_ns": median_ns(shift, 15),
            "build_direct_ns": median_ns(
                lambda: build_direct(previous, width, height, 128, 40, 7),
                2,
                rounds=5,
            ),
            "build_edit_ns": median_ns(
                lambda: build_frame_edit(edit, width, 64, 0, 0, 7), 10
            ),
            "write_count_ns": median_ns(write_count, 30),
            "shm_roundtrip_ns": median_ns(shm_roundtrip, 10, rounds=5),
            "checksum": sum(
                map(sum, (previous, small_bytes, full, two_bytes, fragmented_bytes))
            ),
        }
    finally:
        ring.close(discard=True)
        presenter.close()
    print(json.dumps(results, sort_keys=True))


if __name__ == "__main__":
    main()
