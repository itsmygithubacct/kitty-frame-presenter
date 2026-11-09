# Frame tap

How a second consumer, such as a remote multiplexer, receives every offered
frame without affecting the terminal in front of the user.

A second consumer can receive the frames without affecting the first:

```python
presenter = FramePresenter(term, tap=encoder.offer)
```

The tap sees **every offered frame**, including ones the presenter is about to
drop for pacing, because a remote consumer paces itself and must not inherit
this terminal's drop decisions. It receives a `TappedFrame`: the pixels, the
geometry, and the scroll hint as the caller supplied them — not this
presenter's damage arithmetic, which belongs to this terminal's own state.

A tap that raises is disconnected rather than propagated. A second consumer
must never be able to break the terminal that is actually in front of the
user; `tap_failures` records that it happened.

With no tap, output is byte-identical to before.

`FrameSocketTap` is the ready-made local transport for a multiplexer. It keeps
one newest-frame slot and performs Unix-socket I/O on a daemon thread, so an
absent or stalled receiver cannot delay the local terminal:

```python
from kitty_frame_presenter import FrameSocketTap

tap = FrameSocketTap("/run/user/1000/example.frames", session_id)
presenter = FramePresenter(term, tap=tap)
```

The socket stream is framed, carries the originating broker session on every
frame, and checks the connected peer's effective user ID on Linux. A receiver
should still create the socket in an owner-private directory and validate the
session in every header. Paths must be absolute and fit Linux's Unix-socket
limit; sessions use 1–64 safe ASCII characters. Frames are bounded to 64 MiB,
send timeouts to 60 seconds, and retry intervals to one hour. The shared package
does not enable socket discovery by default; a host can use
`FrameSocketTap.from_environment()` when it deliberately provides
`KILIX_FRAME_TAP_SOCKET` or `KILIX_FRAME_TAP_DIRECTORY`.

`invalidate()` discards any queued frame and forgets the displayed base. Call
it after a resize, screen clear, or other event that invalidates the Kitty
placement; the next offered frame will be a complete placement.
