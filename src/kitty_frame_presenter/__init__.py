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
    FrameSocketTap,
    PosixShmRing,
    PresenterStats,
    PresentResult,
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

__version__ = "0.2.0"

__all__ = [
    "CHUNK",
    "FRAME_BYTES",
    "FrameSocketTap",
    "FramePresenter",
    "PresentResult",
    "PresenterStats",
    "PosixShmRing",
    "ShmBusy",
    "TappedFrame",
    "build_compose",
    "build_direct",
    "build_frame_edit",
    "build_frame_edit_shm",
    "build_full_shm",
    "detect_vertical_scroll",
    "diff_band",
    "diff_damage_rects",
    "diff_rect",
    "diff_rects",
    "extract_rect",
    "wrap_tmux_passthrough",
]
