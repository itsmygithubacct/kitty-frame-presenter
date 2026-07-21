"""Damage-aware RGB presentation through the Kitty graphics protocol.

The package deliberately does not own terminal modes or input.  A caller gives
``FramePresenter`` an object with a ``write(str)`` method and supplies complete
RGB frames.  The presenter chooses full placements, rectangular frame edits,
or a scroll-compose plus exposed damage.  Local terminals receive pixels from
a bounded POSIX shared-memory ring; remote/tmux sessions receive compressed,
chunked inline data.
"""

from .presenter import (
    CHUNK,
    FRAME_BYTES,
    FramePresenter,
    PresentResult,
    PresenterStats,
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
)

__all__ = [
    "CHUNK",
    "FRAME_BYTES",
    "FramePresenter",
    "PresentResult",
    "PresenterStats",
    "PosixShmRing",
    "ShmBusy",
    "build_compose",
    "build_direct",
    "build_frame_edit",
    "build_frame_edit_shm",
    "build_full_shm",
    "detect_vertical_scroll",
    "diff_band",
    "diff_rect",
    "diff_rects",
    "extract_rect",
]

