# Presenter API details

The rules behind `FramePresenter`: output ownership on close, the writer
contract, input validation, damage hints, pacing deadlines, statistics and
scroll composition. The README has the [quick start](../README.md#quick-start).

## Presenting frames

`close()` releases local mappings and descriptors but deliberately leaves any
in-flight shared-memory names available until Kitty consumes and unlinks them.
If buffered output is being abandoned and will never reach a terminal, call
`close(discard=True)` to unlink those payloads explicitly.

A writer must accept the entire string before returning and preserve output order.
If it raises after partial delivery, it must discard any unaccepted suffix before
accepting the next write. The presenter cancels pending graphics data before its
next full placement. After a shared-memory write failure it retains outstanding
payloads and uses inline output for the rest of its lifetime, keeping resource
use bounded without risking delayed readers. Unconsumed names remain available
on normal close; use `discard=True` only when no reader can still need them.
Released ring slots receive fresh generation names.

For a remote or tmux-forwarded stream, construct with `stream=True` and, when
appropriate, `in_tmux=True`.

`present()` accepts bytes or another buffer-protocol object and snapshots it
before retaining or tapping the frame. Widths, heights, placement fields, image
IDs, and scroll offsets must be exact integers in their protocol ranges; frame
times must be finite, non-negative values. Malformed input fails before state or
statistics advance. Calling `present()` after `close()` raises `RuntimeError`.
Both `FramePresenter` and `PosixShmRing` can be used as context managers.

`damage=(x, y, width, height)` or an iterable of such rectangles tells the
presenter where it needs to compare the new complete frame with the displayed
one. Every changed pixel must be covered. Passing `damage=()` explicitly marks
the complete frame unchanged; leaving `damage=None` uses the safe full-frame
comparison. The presenter shrinks hints to actual pixel changes, preserves
separate regions when transport capacity permits, and accumulates hints when
frame pacing replaces queued work. If any transition lacks trustworthy damage
metadata, that accumulated transition falls back to a full-frame comparison.

When work is queued, `next_deadline` is the earliest useful monotonic time for
`flush()`. It accounts for both frame pacing and a short future retry after
shared-memory saturation, so an event loop does not need to poll continuously.
`PresentResult` and `PresenterStats` report the chosen path, exact rectangles,
pixel and wire bytes, drops, keyframes, scrolls, and bounded latency history.

## Scroll composition

`scroll=(dx, dy)` describes where old screen pixels moved. For example, after
document content moves upward by 48 pixels, pass `scroll=(0, -48)`. The
presenter simulates the operation first and compares it with the real frame;
it uses composition only when the residual payload is materially smaller, so
fixed toolbars or an imperfect hint cannot corrupt the result.

Overlapping same-frame composition is enabled only when
`KITTY_KILIX_RENDERING=1` is exported by the Kilix Kitty fork. The command uses
the explicit `N=2` extension bit, leaving stock Kitty's protocol behavior
unchanged.
