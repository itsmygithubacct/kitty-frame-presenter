# Changelog

All notable user-visible changes are recorded here. The public API remains
pre-1.0.

## 0.2.0 - 2026-08-08

### Added

- Context-manager support for the asynchronous frame socket tap.
- Typed-package metadata and a maintained damage, encoding, and transport
  benchmark.
- Randomized displayed-frame reconstruction and failure-path regression tests.

### Changed

- Validate protocol identifiers, frame and placement geometry, rectangles,
  shared-memory names, frame times, scroll hints, socket paths, sessions, and
  bounded socket timeouts before emitting or enqueueing work.
- Coalesce fragmented damage bands in linear space after sorting their gaps,
  instead of repeatedly splicing the list quadratically.
- Encode inline transfers in protocol-sized base64 blocks, avoiding duplicate
  full-payload buffers while retaining byte-identical wire output.
- Copy vertical scroll predictions directly from the immutable prior frame and
  count ASCII wire data without allocating a redundant encoded copy.
- Distribution metadata now uses SPDX licensing and public artifacts remain
  readable when built from a private checkout. `SOURCE_DATE_EPOCH` builds are
  byte reproducible.

### Fixed

- Preserve a pre-existing POSIX shared-memory object when an exclusive name
  collision occurs, and finish cleanup after partial multi-slot allocation
  failures.
- Bound a scroll update by the available shared-memory slots so a one-slot
  presenter cannot queue the same two-rectangle residual indefinitely.
- Give shared-memory backpressure a future retry deadline, preventing event
  loops from spinning on a deadline already in the past.
- Reject presentation after close and account for pending frames discarded by
  invalidation or shutdown.
- Snapshot mutable socket-tap input, reject unsafe or unencodable headers, check
  the connected peer owner, and contain connect, send, close, and worker races.
- Honor a zero stream-warmup interval even when callers supply an earlier clock
  origin.
