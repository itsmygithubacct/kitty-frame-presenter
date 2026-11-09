# kitty-frame-presenter

`kitty-frame-presenter` is a small, dependency-free Python library for
presenting changing RGB framebuffers through the
[Kitty graphics protocol](https://sw.kovidgoyal.net/kitty/graphics-protocol/). It
does not take over the terminal or parse input. It composes with an existing
TTY/event loop by accepting any object with a `write(str)` method.

Ships in Plebian-OS 0.2.3 as a submodule of
[Kilix](https://github.com/itsmygithubacct/kilix#readme)
(`third_party/kitty-frame-presenter`). Kilix's in-pane browser, application
panes and `kilix desktop` present their frames through it.

## What it does


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

# Capture backends can avoid a full-frame comparison by supplying a trusted
# superset of changed pixels. One rectangle and multiple rectangles work.
presenter.present(rgb, width, height, columns, rows, damage=((8, 12, 40, 20),))

# Call from the event loop; this sends the newest queued frame when pacing or
# shared-memory backpressure permits it.
presenter.flush()
presenter.close()
```

The package needs Python 3.9 or newer and nothing outside the standard
library. From a checkout, `PYTHONPATH=src` is enough to import it, or install
it with `pip install .`.

## Usage

| Call | What it does |
|---|---|
| `FramePresenter(term, image_id=1, *, stream=False, in_tmux=False, max_fps=0, tap=None, ...)` | Presenter bound to any object with `write(str)` |
| `present(rgb, width, height, columns, rows, *, damage=None, scroll=None, ...)` | Offer a complete RGB frame; returns a `PresentResult` |
| `flush()` / `next_deadline` | Send the newest held frame; the earliest useful time to call `flush()` |
| `invalidate()` | Forget the displayed base after a resize or clear; the next frame is a full placement |
| `close(discard=False)` | Release mappings; `discard=True` also unlinks unconsumed shared-memory payloads |
| `stats` (`PresenterStats`) | Paths taken, rectangles, pixel and wire bytes, drops, keyframes, latency |
| `FrameSocketTap(path, session_id)` | Newest-frame Unix-socket tap for a second consumer |

| Environment | Effect |
|---|---|
| `KITTY_KILIX_RENDERING=1` | Exported by the Kilix Kitty fork; enables overlapping same-frame scroll composition |
| `KILIX_FRAME_TAP_SOCKET`, `KILIX_FRAME_TAP_DIRECTORY` | Read only by `FrameSocketTap.from_environment()` |

## How it works

```
 application frame (RGB bytes) ──present()──> damage: exact rectangles or row bands
        │                                         │
        │ tap (every offered frame)               ▼
        ▼                                  local:  t=s shared-memory ring (3 slots)
  FrameSocketTap ──Unix socket──> remote   remote: t=d,o=z chunked inline + keyframes
                                           scroll: shift root texture, upload residual
                                                   │
                                     pacing: hold one pending frame, newest wins
                                                   ▼
                                           term.write(escape) ──> Kitty / Kilix
```

The presenter keeps one Kitty image placed and edits it in place. For each
frame it finds the changed pixels (from the caller's damage hint or a full
comparison), chooses the transport (a shared-memory slot that the terminal
unlinks when it has read it, or compressed inline data for a remote stream),
and writes one escape sequence to the caller's writer. With pacing enabled it
holds at most one frame and replaces it with newer work. It never reads from
the terminal or starts a thread; only `FrameSocketTap` uses a daemon thread.
Details: [docs/api.md](docs/api.md) and [docs/frame-tap.md](docs/frame-tap.md).

## Scope

The package intentionally contains no terminal lifecycle, keyboard/mouse
decoder, framebuffer renderer, X11 capture code, or application policy. Those
belong in `kitty-terminal-session`,
[`kitty-input`](https://github.com/itsmygithubacct/kitty-input), the
application, or its capture backend. The low-level keyboard decoder is bundled
under `kitty-input/third_party/kitty_keyboard`; it is not a standalone stack
repository.

The API is pre-1.0 and may change between minor releases.

## Documentation

| Document | Contents |
|---|---|
| [docs/api.md](docs/api.md) | Close and discard, the writer contract, input validation, damage hints, deadlines, statistics, scroll composition |
| [docs/frame-tap.md](docs/frame-tap.md) | The frame tap, `FrameSocketTap` and `invalidate()` |
| [docs/development.md](docs/development.md) | Tests and the benchmark |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

## Related repositories

- [kilix](https://github.com/itsmygithubacct/kilix#readme): pins this module
  for its browser, application panes and desktop provider.
- [kilix-multiplexer](https://github.com/itsmygithubacct/kilix-multiplexer#readme):
  remotes a Kilix session; a frame tap is how pixels reach it.
- [kitty-framebuffer](https://github.com/itsmygithubacct/kitty-framebuffer#readme):
  the C library that owns the whole terminal lifecycle instead.
- [kitty-input](https://github.com/itsmygithubacct/kitty-input#readme) and
  [kitty-terminal-session](https://github.com/itsmygithubacct/kitty-terminal-session#readme):
  input decoding and terminal lifecycle, which this package leaves out.

## Part of Plebian-OS

kitty-frame-presenter is one of the repositories behind
[Plebian-OS](https://github.com/itsmygithubacct/plebian-os#readme), a Debian
install whose desktop is [Kilix](https://github.com/itsmygithubacct/kilix#readme)
running the [Kilix 95](https://github.com/itsmygithubacct/kilix-95#readme)
desktop through [Pleb](https://github.com/itsmygithubacct/pleb#readme).
It ships inside Kilix as the `third_party/kitty-frame-presenter` submodule and
draws every pixel pane Kilix shows. The map of every repository and how it
ships is on the
[components page](https://github.com/itsmygithubacct/plebian-os/blob/main/docs/components.md).

## License

MIT. See [LICENSE](LICENSE).
