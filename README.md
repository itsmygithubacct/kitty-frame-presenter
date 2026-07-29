# kitty-frame-presenter

`kitty-frame-presenter` is a small, dependency-free Python library for
presenting changing RGB framebuffers through the Kitty graphics protocol. It
does not take over the terminal or parse input. It composes with an existing
TTY/event loop by accepting any object with a `write(str)` method.

The presenter keeps one displayed image stable and updates it in place:

- exact rectangular damage replaces full-width row bands;
- local pixels travel through a bounded three-slot POSIX shared-memory ring
  (`t=s`), with Kitty's unlink acting as consumption acknowledgement;
- remote pixels use zlib-compressed, chunked inline transport (`t=d,o=z`);
- scrolling can shift the existing root texture and upload only residual
  regions when the Kilix Kitty extension is available;
- optional frame pacing holds one pending frame and replaces it with newer
  work, so a slow terminal costs dropped frames instead of growing latency;
- periodic remote keyframes keep late-attached clients recoverable.

Each `present()` call is synchronous only through creation of the graphics
escape. The presenter never reads from the terminal, changes descriptor flags,
or starts a worker thread. Call `flush()` at the next event-loop deadline to
release the newest frame held by pacing or shared-memory backpressure.

The similarly named
[`kitty-framebuffer`](https://github.com/itsmygithubacct/kitty-framebuffer)
has a different contract: it is a C library that owns raw mode, terminal
probing, alternate-screen lifecycle, and a full RGBA framebuffer. This package
is the presentation-only layer for Python applications that already own those
concerns.

## Quick start

```python
from kitty_frame_presenter import FramePresenter

presenter = FramePresenter(term, image_id=7, max_fps=30)
presenter.present(rgb, width, height, columns, rows)

# Call from the event loop; this sends the newest queued frame when pacing or
# shared-memory backpressure permits it.
presenter.flush()
presenter.close()
```

`close()` releases local mappings and descriptors but deliberately leaves any
in-flight shared-memory names available until Kitty consumes and unlinks them.
If buffered output is being abandoned and will never reach a terminal, call
`close(discard=True)` to unlink those payloads explicitly.

For a remote or tmux-forwarded stream, construct with `stream=True` and, when
appropriate, `in_tmux=True`.

## Frame tap

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
frame, and is owner-checked by the receiver. The shared package does not enable
socket discovery by default; a host can use
`FrameSocketTap.from_environment()` when it deliberately provides
`KILIX_FRAME_TAP_SOCKET` or `KILIX_FRAME_TAP_DIRECTORY`.

`invalidate()` discards any queued frame and forgets the displayed base. Call
it after a resize, screen clear, or other event that invalidates the Kitty
placement; the next offered frame will be a complete placement.

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

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests consume shared-memory escapes like a terminal, exercise saturation
and safe slot reuse, verify exact damage/scroll results, and pin newest-frame
pacing behavior.

## Scope

The package intentionally contains no terminal lifecycle, keyboard/mouse
decoder, framebuffer renderer, X11 capture code, or application policy. Those
belong in `kitty-terminal-session`, `kitty-keyboard`, the application, or its
capture backend.

The API is pre-1.0 and may change between minor releases.

## License

MIT. See [LICENSE](LICENSE).
