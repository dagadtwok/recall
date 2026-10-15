# MSync adaptive-spin patch harness

`python3 tests/msync_spin/run.py` validates the logic inside
`patches/wine-msync-spin.patch` without building Wine. The runner extracts the
hunk the patch adds before the mach-message helpers in `dlls/ntdll/unix/msync.c`
(the spin helpers and `msync_single_satisfied`), compiles them into a host
harness with clang, and asserts:

- The window is off unless `WINE_MSYNC_SPIN` holds a positive integer; a bad
  or zero value is off, values above 16384 clamp there.
- A fresh thread probes at the short window (`min(window, 16)`); two
  consecutive waits settled inside the spin earn the full window, and a wait
  that exhausts the spin (a long wait) drops the thread back to the probe.
- `msync_single_satisfied` answers exactly as the top of
  `msync_wait_single`'s blocking loop does: a free (0) or abandoned (~0)
  mutex, a signaled object, and nothing in between; a mutex owned by another
  thread, a mutex owned by self, and an unsignaled object all stay unsatisfied
  with the right compare value left for `ulock_wait`.
- Both architecture guards compile: arm64 natively, x86_64 as a syntax-only
  second pass (`-arch x86_64 -fsyntax-only` sets up the bonus code path that
  clamps the probe thread's time budget).

This proves the patch's state machine and its semantic equivalence to the code
it refactors; it does not exercise the mach sleep/wake pair, which needs an
engine build. The A/B measurement method is in `research/shader-stutter.md`
and `research/README.md`.
