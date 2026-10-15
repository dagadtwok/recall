# Performance patch research (October 2026)

*Status, October 16, 2026: candidates 1 (MSync adaptive spin) and 3 (fast
yield) now exist as off-by-default patches — `patches/wine-msync-spin.patch`
(`WINE_MSYNC_SPIN`, launcher `--msync-spin N`, harness `tests/msync_spin`) and
`patches/wine-fast-yield.patch` (`WINE_FAST_YIELD`, launcher `--fast-yield`).
Candidate 4's config knob exists as `launch_cx26.py --prewarm-limit`. The
research below describes why; adopt only after the A/B measurement.*

Research only. The code changes that grow out of this folder live outside it
(`patches/`, `scripts/`, `docs/`); sources here stay inline.

The question: which performance patches shipped by Valve's Proton, Proton-GE,
wine-tkg, proton-cachyos, GameNative's Proton fork and upstream DXMT could still
improve Overwatch on Recall's stack (CodeWeavers Wine 26.3.0 + Rosetta + patched
DXMT)? Verified on October 15, 2026 against the published trees listed in
[Sources](#sources).

## Summary

CrossOver 26.3 (Recall's Wine base) is Wine 11.0 with CodeWeavers' **MSync**
(macOS fast synchronization, `WINEMSYNC=1`, already set in
`scripts/launch_cx26.py:113`) — the macOS counterpart of Proton's esync/fsync/
ntsync. Recall's DXMT base (NerRobDog's `ow2-pack` fork, pinned at `c5dc3a0`)
already contains the community shader-IR memory work. Most of the famous Linux
patch set is therefore already harvested or inapplicable. What remains:

| # | Candidate | Where it comes from | Expected value | Effort | Risk |
|---|---|---|---|---|---|
| 1 | Adaptive spin-before-block for MSync waits | ntsync-android / wine-tkg spin counts | Frame-time smoothing under Rosetta where a mach wake is expensive | Small | Low (env-gated) |
| 2 | Multi-object waits stay out of the wineserver | ntsync kernel design; msync's Mach round trip | Cuts one Mach message per `WaitForMultipleObjects` | Medium–high | Medium |
| 3 | Fast `NtYieldExecution` (`WINE_FAST_YIELD`) | GameNative proton-wine | Tiny; only matters if the game yields per frame | Trivial | Low |
| 4 | Resync Recall's DXMT base with 3Shain `main` | 3Shain/dxmt, 65 commits since the fork | Correctness + a few latency/robustness wins in paths Recall touches | Small per cherry-pick | Low |
| 5 | MSync wait-multiple spin-poll cleanup | CX `msync_wait_multiple` spin loop | Bounded gain; only if profiling shows it hot | Small | Low |

Not applicable on this stack (checked, see [Not applicable](#not-applicable)):
esync/fsync-futex2 kernel machinery, ntsync kernel driver, FEX/ARM64EC work,
WoW64/LAA patches, wine-wayland, FSR4/DLSS-v4 pipelines, Proton's DXVK or
vkd3d-proton tuning, GameNative's `WINEVMEMMAXSIZE`.

## Already covered — the Linux→Recall map

| Linux/Proton patch | Recall equivalent |
|---|---|
| esync / fsync / futex2 / ntsync | CodeWeavers MSync, enabled (`WINEMSYNC=1`); CrossOver 26 dropped esync in favor of ntsync (Linux) + MSync (Mac) |
| DXVK async pipeline compilation / gplasync | DXMT prepare-shaders on creation (`DXMT_PREPARE_SHADERS=1`), 8-thread prewarm, learned pipeline pack (`dxmt-shader-pack.patch`) |
| `dxgi.maxFrameLatency` tricks | 3 drawables + `dxgi.presentPacing` mode 3 |
| wineserver registry-save stalls | `wine-server-registry-save.patch` |
| Loader/virtual-memory lock stalls | `wine-ntdll-syscall-log.patch` |
| Cursor re-center / pointer-rate cost | `wine-mouselook.patch` (raw Game Controller input) |
| Fullscreen activation frame drops | `wine-winemac-activation.patch` |
| MetalFX/FSR-type upscalers | `dxmt-metalfx-upscaling.patch` (MetalFX + FidelityFX RCAS sharpening) |
| NVIDIA Reflex path | Not implemented; see [Future ideas](#future-ideas-noted-not-ranked) below |

## Wine-side candidates

### 1. Adaptive spin-before-block in MSync waits (rank 1)

**Provenance.** ntsync-android (userspace ntsync backend shipped by
GameNative/proton-wine) gained "adaptive spin-before-block
(`NTSYNC_SPIN_ITERS`, default off)": a waiter spins briefly on a read-only
acquire check before sleeping on the futex; a signal landing in the window
avoids a sleep+wake pair entirely. Per-thread adaptive credit self-throttles
long-waiter threads. Measured on device: Persona 5 Royal avoided 25–28% of
blocked-wait sleeps during sync bursts (60–76% spin win rate); other games
correctly self-throttle to ~0.2% CPU. wine-tkg ships the same idea for fsync as
`WINEFSYNC_SPINCOUNT`.

**Why it applies here.** CX 26.3's MSync has no spin knob.
`dlls/ntdll/unix/msync.c` (`msync_wait_single`, ~line 301) goes straight from
the atomic check to `ulock_wait(UL_COMPARE_AND_WAIT_SHARED…)` — a Mach trap
that costs more to enter/exit than a few atomic loads. Under Rosetta every such
trap is translated, so short sync bursts (the render loop of a DX11 game pumps
dozens of waits per frame) pay the sleep+wake pair repeatedly whenever the
original Windows code would have taken the fast lock after microseconds.
A short, env-tunable, credit-throttled spin before `ulock_wait` mirrors what the
Android backend validated and can only reduce wake latency in the win window.

**Port sketch.** In `msync_wait_single` (and the block phase of
`msync_wait_multiple`), before the first `ulock_wait`: up to
`WINE_MSYNC_SPIN` iterations re-check the shm word with plain atomic loads
framed by `yield`-style pauses on arm64 (or `_mm_pause` on x86), honoring the
deadline math (`update_timeout`), then fall through unchanged. Default off; the
app's game profile turns it on for `Overwatch.exe` via a new env, e.g.
`WINE_MSYNC_SPIN=64`.

**Conflicts.** None with existing Recall patches (they don't touch
`ntdll/unix/msync.c`).

**Measurement.** Compare off/on with the off-screen replay chain: the same
recorded match replayed twice (`scripts/` has the call recorder and replay
tools), instrumented by `tests/shader_trace` and the frame-log CSV
(`DXMT_FRAME_LOG` in the fork adds per-frame timing), then
`scripts/summarize_frames.py`-style aggregation if relevant. Metrics: median/
99th frame time, idle gap between frames (Recall's pacing already measures
this), CPU time of the game thread. Success = 1% lows up, median flat, game
thread CPU not climbing beyond ~1% core.

### 2. Remove the Mach round trip from multi-object waits (rank 2)

**Provenance.** The upstream ntsync design (kernel-backed) handles
`WaitForMultipleObjects` entirely inside one device call; GameNative's userspace
ntsync-android does the equivalent with signal ops and wait-path sweeps in shm,
"wait paths run a rate-limited region-wide sweep… (a dead-process scan can
never land on a latency-critical signaler)".

**Why it applies here.** In CX 26.3, `msync_wait_multiple` (msync.c ~line 359)
sends `server_register_wait` — a Mach message to the wineserver naming every
object's shm index — then spins on a per-tid shm word until the server accepts,
blocks on `ulock_wait`, and on wake/timeout sends a second Mach message
(`server_remove_wait`). Single-object waits do all of this in-process. So every
multi-wait costs two translated Mach messages + a registration spin, per call.
Whether this is measurable for Overwatch depends on how often the game crosses
multiple waits (DX11 games with queue-driven render loops do it regularly, but
exact frequency is unknown until instrumented).

**Port sketch.** Two stages, measured separately: (a) smaller — post only the
in-wait bookkeeping through shm once per object (`wakeup` bits owned by the
signaler), replacing the spin-on-ack with the signal-handoff that ntsync's
wait paths use; (b) larger — move the wait registration entirely into shm like
ntsync-android's wait path. Both keep the current fallback.

**Conflicts.** None directly; server/`msync.[ch]` is untouched by Recall's Wine
patches, but wineserver-protocol changes must survive Recall's registry/save and
syscall-log patches (independent files).

**Measurement.** Instrument first: count `server_register_wait` /
`server_remove_wait` calls per second during the recorded replay (a one-line
atomic counter exported through the existing `WINE_DEBUG`-style log infra used
by `wine-ntdll-syscall-log.patch` decides whether this is worth it). If it's
tens per second, proceed; if hundreds with per-call real-time Mach overhead
measured in the hundreds of µs, promote to the (b) rewrite.

### 3. Fast `NtYieldExecution` / `WINE_FAST_YIELD` (rank 3)

**Provenance.** GameNative's proton-wine release notes: "`WINE_FAST_YIELD`
fast-yield hook in `NtYieldExecution`" (their ongoing port to the Android
runtime). The idea is older: a yield should not cost a measurement syscall, let
alone two.

**Why it applies here.** Wine 11.0's `NtYieldExecution`
(`dlls/ntdll/unix/sync.c:2548`) costs two `getrusage(RUSAGE_THREAD)` calls per
call just to detect "did the yield switch". That path goes
through `proc_info`, a heavier trap than `sched_yield()`, and under Rosetta each
is translated. `SwitchToThread()`-style spin loops in game threads call this
path potentially thousands of times a frame. Cheap not to pay.

**Port sketch.** In `NtYieldExecution`, read the switch counter once
(`getrusage` once, or `thread_info(THREAD_BASIC_INFO)`); only compute
`STATUS_NO_YIELD_PERFORMED` when `WINE_FAST_YIELD` is unset — when set, run a
bare `sched_yield()` and always report `STATUS_SUCCESS` (the caller must not
rely on the status). Env-gated, default off.

**Conflicts.** None.

**Measurement.** First count calls per frame during the replay (same
instrumentation pattern). If below ~50 per frame, park the idea. Otherwise A/B
frame times; expect no visible change — this targets CPU overhead, so accept
the patch only if the game thread's CPU time drops measurably.

### MSync wait-multiple spin cleanup (rank 5)

`msync_wait_multiple`'s pre-registration loop (`while addr == 2:
check_shm_contention`) is a bounded spin that re-reads each object's shm word
every iteration until the server acks. A cheap improvement is to cap those
re-reads before falling into the ulock wait using the same deadline logic as
`msync_wait_single` already does. Revisit only once candidate 1's
instrumentation exists, since both share it.

## DXMT candidates (rank 4)

Recall's DXMT tree (`runtime/phase-2/dxmt-source`, `origin=NerRobDog/dxmt`,
HEAD `c5dc3a0`, tracked in `docs/SOURCES.json`) already **includes** the v0.80
release: comparing GitHub `main...c5dc3a0` shows the fork diverged from 3Shain
`main` around 2026-08-31 with 15 fork-specific commits (the full `ow2-pack`
feature set: shader-IR lifetime management `d3d11.releaseShaderIR`, per-frame
`DXMT_FRAME_LOG` CSV logger, `dxgi.customVideoMemory`) while upstream continued
for 65 commits. The community memory fix previewed on r/macgaming (13 GB→4 GB
footprint fix) is that fork's `d3d11.releaseShaderIR` — it is **already in
Recall's tree, default on** (`dxmt.conf:36`, `docs/ow2-pack/README.md:8`); no
port needed.

From the 65 upstream commits since the fork point, the ones that touch paths
Recall cares about (D3D11/winemetal/nvngx), worth a cherry-pick evaluation, in
order:

| Upstream commit | What | Why consider it for Recall |
|---|---|---|
| `ce79ad0` 2026-10-05 | fix(d3d11): fix logic flaws in dilated motion vector handling | Recall's MetalFX patch rewrites exactly this path (jitter cancellation pre-pass); compare against its implementation before rebasing |
| `0b63990` 2026-10-05 | fix(d3d11): lock device in `Signal()`/`Wait()` | Protects the fence wait Recall's pacing relies on from a race |
| `acda4f9` 2026-10-02 | fix(d3d11): remove `d3d11_device_mutex::try_lock()` | Mutex-path cleanup that may show in frame-time A/B |
| `e94c312` 2026-10-06 | fix(d3d11): avoid deadlock on reentrant `ResizeTarget()` | Fullscreen mode-switch robustness; Recall's canvas patch alters this same region of dxgi/d3d11 |
| `830eb41` 2026-10-02 | fix(d3d11): raise vertex buffer binding count to `D3D11_IA_VERTEX_INPUT_RESOURCE_SLOT_COUNT` | Correctness for games binding more slots than the old 16 limit |
| `e40c9d7` 2026-10-05 | fix(d3d11): allow copying between different texture types in `CopySubresourceRegion()` | Lands in the same files as `dxmt-v1-private.patch`; evaluate merge cost |
| `ef11653` 2026-09-14 | feat(d3d11): introduce `d3d11.maxTessFactor` option | New tunable; unlikely to move Overwatch but free to expose |
| `f9beaea` 2026-10-02 | fix(dxmt): pass mipped width & height to clear encoder | Minor GPU-work savings on clears |
| `654f547` 2026-09-15 | fix(winemetal): fix custom HUD metrics on macOS 27 | Future-proofing for the macOS 27 baseline |
| `d1c926c` 2026-09-10 | fix(airconv): properly handle vectorized `firstbit_shi` | Shader-transpiler correctness; check for compile-time perf side effects |
| `542ca17` + `59eb4c1` 2026-10-02 | nvngx fixes (default pre-exposure 1.0; correct 0xbad00000 error values) | Both already handled inside `dxmt-metalfx-upscaling.patch` itself (pre-exposure default and `0xbad00000`); treat upstream as confirming, not new work |
| `37e0453` + `20af9fc` 2026-09-13 | `DXMT_HOTPATCHABLE` patchable COM stubs | Not perf: hotpatch infrastructure, interesting for shipping A/B toggles without rebuilding |

Also settled: the upstream 0.74→0.80 highlights (MSAA performance, the
uncached-PSO AIR optimization that reduces first-run stutter, persistent user
PSO cache, fast-math airconv changes, and the buffer-binding remap by
`199e336`) are all in Recall's base — the fork point follows v0.80, so
upstream's 65 extra commits are all post-fork work (compare status `diverged`).

**Process recommendation.** The fork is only ~6 weeks behind a fast-moving
upstream. Rather than a big-bang rebase through Recall's five stacked patches
(`dxmt-v1-private`, `dxmt-v1-performance`, `dxmt-portable-metal`,
`dxmt-metalfx-upscaling`, `dxmt-shader-pack` — ~13,500 patch lines), cherry-pick
the ranked list one at a time on top of the existing stack, run the replay A/B
for each, and fold into the patches. Schedule quarterly.

## Not applicable (checked)

| Patch family | Reason |
|---|---|
| esync / fsync / futex2 / winesync | Superseded on macOS by MSync (verified enabled); on Linux ntsync/MSync have replaced esync in CX 26 |
| ntsync kernel driver | macOS has no `/dev/ntsync`; CX's `server/inproc_sync.c` is `#ifdef HAVE_LINUX_NTSYNC_H` |
| FEX / ARM64EC builds (`FEX-2604/2607/2610`) | Recall runs x86 via Rosetta |
| WoW64 / LAA patches (wine-tkg `LAA` wow64, GameNative `WINEVMEMMAXSIZE`) | Overwatch is native 64-bit; FEX-only env |
| wine-wayland patch sets (GE-Proton) | No Wayland driver in the Mac build |
| Proton's DXVK / vkd3d-proton tuning | Recall uses DXMT (Metal), not DXVK; Overwatch is a D3D11 game |
| FSR4 / DLSS-v4 pipelines (`PROTON_FSR4_UPGRADE`) | Recall already spoofs DLSS→MetalFX temporally |
| Proton 11 per-game Overwatch fixes (controller input registration, Battle.net self-update, focus-loss on KDE) | These fixes are either not applicable (KDE/X11) or already solved on Recall's stack (mouselook patch, Battle.net works) |
| CachyOS scheduler/tuning | Kernel-level, Linux-only |
| Xalia, Steam Input, gamescope hacks | Steam client runtime features Recall doesn't ship |

## Future ideas (noted, not ranked)

- **NVIDIA Reflex-style low-latency path.** Recall's DLSS stand-in already
  answers Streamline's D3D12 queries. If the game surfaces Reflex toggles on
  the spoofed RTX 4090, a Reflex stand-in could expose the low-latency hint to
  Recall's own `presentPacing` instead of no-op'ing. Large; only after candidates
  1–4.
- **`d3d12` dedicated-thread command-buffer wait (`fe89926`).** D3D12-only
  path; irrelevant while Overwatch runs D3D11, but Recall's DLSS stand-in's
  Streamline d3d12.dll stub must keep pretending these are unsupported.

## Sources

All verified October 15, 2026.

- CrossOver 26 changelog & release announce: Wine 11.0 + "NTSync support for Linux and MSync support for Mac, we no longer include ESync" — https://www.codeweavers.com/crossover/changelog ; Phoronix 2026-02-10.
- Recall runtime confirmed `WINEMSYNC=1`: `scripts/launch_cx26.py:113`, `runtime_policy` in `docs/SOURCES.json:115`.
- CX 26.3 MSync sources inspected (tarball `crossover-sources-26.3.0.tar.gz`, sha256 `ac99c8ca4b3848f3e81784135f023df266b61c2345726ea55a50b3e030dd6872`): `dlls/ntdll/unix/msync.c` (`msync_wait_single` ~L301, `msync_wait_multiple` ~L359, `do_msync` L422), `server/msync.c`, `server/inproc_sync.c` (Linux-only include guard).
- ntsync-android backend, adaptive spin values and measurements: GameNative/proton-wine release notes; backend at commit `7ce6435` (see https://github.com/GameNative/proton-wine/releases).
- `WINE_FAST_YIELD` hook: GameNative/proton-wine release notes (Proton 11.0-2 Android port series).
- Wine 11.0 `NtYieldExecution` cost: CX 26.3 `dlls/ntdll/unix/sync.c:2546-2560`.
- wine-tkg spin-count precedent: Frogging-Family/wine-tkg-git `customization.cfg` (`WINEFSYNC_SPINCOUNT`, `_fsync_spincounts`) — https://github.com/Frogging-Family/wine-tkg-git.
- DXMT divergence, commit contents: GitHub compare `3Shain/dxmt` `main...c5dc3a0` (`diverged`, ahead 15 / behind 65) and `c5dc3a0...main` commit list; fork docs `runtime/phase-2/dxmt-source/docs/ow2-pack/README.md`.
- releaseShaderIR community report: r/macgaming "[Fork release] DXMT tuned for Overwatch 2: memory fix (13→4 GB)…" 2026-09-01.
- Proton 11.0-3 RC changelog (no portable wine perf item for this stack): https://github.com/ValveSoftware/Proton/issues/10236.
- GE-Proton / wine mainstream status (ESYNC status, FSR4, per-game patch families): https://lab.trianta.dev/mirrors/GloriousEggroll.proton-ge-custom/wiki/Changelog ; https://github.com/ValveSoftware/Proton/wiki/Changelog.
