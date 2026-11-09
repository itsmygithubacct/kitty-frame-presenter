# Development

## Tests

```sh
make test
```

The tests consume shared-memory escapes like a terminal, exercise saturation
and safe slot reuse, verify exact damage/scroll results, reconstruct randomized
inline presentation sequences, exercise socket and cleanup failure paths, and
pin newest-frame pacing behavior.

Run the deterministic performance workload with:

```sh
make benchmark
```

It reports JSON nanosecond medians for unchanged, small, full, split, and highly
fragmented damage; vertical prediction; full and rectangular inline encoding;
wire accounting; and a shared-memory round trip. Timings are comparative and
depend on the host, while the emitted checksum makes workload drift visible.
