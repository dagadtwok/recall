# DXMT fork (NerRobDog `ow2-pack`) — improvement opportunities

Research only; no code changes. Follow-up to `research/README.md` (October
2026), looking at the tree Recall actually builds:
`runtime/phase-2/dxmt-source` (origin `NerRobDog/dxmt`, HEAD `c5dc3a0`,
base upstream DXMT v0.80 @ `19e24ee` per `docs/ow2-pack/README.md`).

**Constraint that shapes this document.** The tree's `AGENTS.md` forbids AI
generated code for this project ("AI must not be used to generate code for
contributions to this project"). Everything below is therefore an anchor-based
spec (file:line) for a human implementer — descriptions of what to change and
how to measure it, not diffs.

Note: the working tree currently carries Recall's dxmt-v1-* patch set as
uncommitted modifications. Line numbers are from that state — the state that
builds the shipped runtime — which is the right basis for judging conflicts.

## What the fork already contributes

- `d3d11.releaseShaderIR` (default on) — the 13 GB→4 GB footprint fix, matching
  the r/macgaming announcement; `dxmt.conf` documents it.
- `dxgi.customVideoMemory` — optional VRAM cap reported to the game (unified
  memory budget lives at ~75% of system RAM otherwise).
- `DXMT_FRAME_LOG` per-frame CSV telemetry, plus `FrameTelemetry::Encode` and
  `FrameTelemetry::Completion` spans in the encode and finish threads of
  `src/dxmt/dxmt_command_queue.cpp` — the A/B instrument this list leans on.
- Fork-private compiler knobs `d3d11.shaderCompilerThreads` and
  `d3d11.shaderCompilerNormalPriority` (clamped 1..64 in the tree).
- A per-frame GPU timing ring: `gpu_timing_` in the finish thread records
  per-frame GPU busy, active and idle spans (`src/dxmt/dxmt_command_queue.cpp:244`),
  i.e. the fork has GPU-side counters to validate any improvement claim
  in-report with no extra instrumentation needed (built with `DXMT_BENCH=1`
  or pacing mode 2).

## Ranked improvement list

### Rank 1 — Implement `DiscardResource` / `DiscardView[1]` (GPU work, once per frame)

Anchor: `src/d3d11/d3d11_context_impl.cpp` — both are stubbed with
`ERR_ONCE("Not implemented")` and a FIXME naming the design at ~line 1256:

> FIXME: for render targets we can use this information: LoadActionDontCare

DX11 games discard their per-frame temporary render targets (post-process
buffers, lighting targets). A discard means "prior contents are undefined";
Metal can then set the render pass load action to `dontCare`, skipping the
tile load of the previous contents. On Overwatch several near-fullscreen
targets are re-rendered every frame, so the load is pure wasted bandwidth.
The load-action change is a pure gain when applied correctly, and it is
complementary to Recall's compression patch (which protects store behavior;
this item concerns the load at the top of a pass).

Design sketch (human):
- Flag a subresource "discard-pending" on `DiscardResource` /
  `DiscardView1`. `DiscardView1` with rects should stay a no-op until proven
  needed — Overwatch discards whole targets or not at all.
- On the next render pass encoded with that RTV bound, use
  `loadAction == DontCare`, guarded by the existing hazard/deptrack check the
  passes already run.
- The back buffer itself is a separate case (see Rank 2).

Verification: the CSV from `DXMT_FRAME_LOG` carries `FrameTelemetry::Encode`
spans and, with `DXMT_BENCH=1` or pacing mode 2, the `gpu_timing_` ring
reports per-frame GPU busy time. Expectation: GPU busy drops, pixels are
unchanged (checked via the off-screen replay determinism harness).

### Rank 2 — Try `dxgi.framebufferOnly = True` in the shipped runtime (config-only A/B)

Anchor: `src/dxmt/dxmt_presenter.cpp:31` (option exists, default false; the
inline comment records upstream's policy plus the direct-flip condition).
`launch_cx26.py:150` adds it as an opt-in developer toggle today; the shipped
profile `config/dxmt-source60-1200.conf` does not set it, and the app env
(the candidate contract) does not set it either. `CAMetalLayer.framebufferOnly =
YES` is the ticket to Core Animation's zero-copy direct flip path: layers
marked that way (as macOS native games do) skip compositor copy per frame.
Trade-offs: the layer's pixels cannot be read back (the Korean build's
screencap stub already returns black with `WINEMAC_SCREEN_READBACK=1`, so no
conflict); in-game photo/screenshot features that read the drawable would
see it as unavailable — the same trade every native macOS game makes.

Metrics: `DXMT_FRAME_LOG`'s drawable/present section + summary. Expected:
compositor CPU time off the render thread; medians unchanged.

### Rank 3 — Cap reported VRAM via `dxgi.customVideoMemory` (config-only A/B)

Anchor: documented in `dxmt.conf` of the fork; the shipped profile explicitly
leaves it 0 ("No video-memory override" in `config/dxmt-ow2.conf`). With
releaseShaderIR returning the process footprint to ~4 GB, the remaining
pressure is the games' own allocations: DX11 games size pools off
`DedicatedVideoMemory`. Today the budget = ~75% of system RAM (~12 GB on this
M1 Pro 16 GB), which lets Overwatch allocate generously and then fight the
OS for the last 4 GB. A cap of `8192` (or `6144`) makes the game stream more
eagerly instead of hoarding; the win condition is higher 1% lows in the same
replay, the loss condition is texture pop-in or slower loading.

### Rank 4 — Upstream cherry-picks (same list as research/README.md, plus conflict map)

Verified overlap with Recall's modified files (i.e. the working tree already
touches these files, meaning clean `git apply` is unlikely — human port
required, with the same A/B gate):

| Upstream | File | Recall's overlap with | Risk |
|---|---|---|---|
| `acda4f9` remove `d3d11_device_mutex::try_lock()` | `src/d3d11/d3d11_multithread.hpp` | none (clean pick) | low |
| `830eb41` vertex slot count | `src/d3d11/d3d11_context_state.hpp` | none (clean pick) | low |
| `0b63990` fence device lock in `Signal()`/`Wait()` | `src/d3d11/d3d11_context_imm.cpp` | +27 lines of Recall patch | low-medium |
| `e94c312` reentrant `ResizeTarget` deadlock | `src/d3d11/d3d11_swapchain.cpp` | +136 lines (canvas) | medium |
| `ce79ad0` dilated motion-vector cleanups | `src/d3d11/d3d11_context_impl.cpp` | +180 lines (MetalFX region, see `:5289-5306`) | medium |

Do the two clean picks first, then the low-medium one; for the two medium
items, measure whether the bug is observable in Overwatch at all before
paying down the port (the call recorder can answer that).

### Rank 5 — Remaining small code items (do during an existing session)

- `d3d11_context_impl.cpp` ~line 1258 FIXME: "A Map with D3D11_MAP_WRITE type
  could become D3D11_MAP_WRITE_DISCARD?" — dynamic Map without DISCARD waits
  for prior GPU use; the alternative grow-heap path (checking whether the
  resource's `dynamicUsage` was set at creation and GPU-last-write) could
  remove the wait entirely for hot dynamic buffers. Needs a per-Map GPU
  dep check (deptrack already does this elsewhere).
- `d3d11_context_impl.cpp:5155` TODO: "optimize by clearing only bound resource" —
  ClearState currently clears every bound resource rather than the bound view;
  micro-win, few lines.
- `d3d11_resource_helper.cpp:386` FIXME: `D3D11_USAGE_IMMUTABLE` resources get
  `Managed` storage (a CPU mirror + GPU private copy); upstream notes
  switching them to `Private` (GPU-only). Lower resident footprint per
  immutable resource, but every path that touches such a resource from the
  CPU (the resource-initializer/invalidation path and the replay harness)
  must be audited first.
- `d3d11_inspection.cpp:9` TODO "fake non-TBDR" — a TBDR-on/off knob could
  matter for a forward-looking MAC support matrix; not for Overwatch.

### Rank 6 — Not worth pursuing (checked so you don't have to)

- `d3d11.defuseFma` — perf-correctness knob; forces full shader recompilation
  (cache invalidation) and is a debugging aid for arithmetic correctness, not
  a throughput knob.
- `d3d11.ignoreMapFlagNoWait` — only changes behavior for buggy games; the
  fork already leaves it False for unlisted games. Overwatch honors errors.
- `dxmt.shaderMetalVersion` — A/B possible (310 vs 320), but Metal 3.2 support
  on macOS 26 is free and the 3.1 backend offers no specific Overwatch shader
  until a compiler profile needs it.
- `d3d12.preferredMaxFrameRate` — D3D12 nodes only.
- `dxgi.displaySyncEnabled` — upstream comment already documents "true → worse
  performance"; Recall keeps it for one diagnostic case only.
- anything in `d3d12`/`d3d12stub`/`airconv` for d3d12 paths — Overwatch runs
  D3D11; only relevant if Recall's Streamline d3d12 stub ever becomes real.

## Cross-link to the Wine-side report

DXMT's internal wait paths go through `WaitOnAddress` on Wine
(`src/util/util_address_wait.hpp:10` comment: notify_* resume "1–3 ms late"
without it, vs ~10 µs with). Under Wine on macOS that ends in MSync's
`ulock_wait` — exactly the code path targeted by `research/README.md`
candidate 1 (adaptive spin-before-block in MSync). A short spin before the
`UL_COMPARE_AND_WAIT_SHARED` sleep shrinks the wake window for these waits
too, so the per-wake cost on DXMT's threads (including the once-per-frame
pacing wake) could improve from the same patch. This makes the MSync spin
candidate more attractive than it already looked: one Wine-side patch can
benefit DXMT's own fast paths as well as Wine's internal waiters.

## Measurement workflow for every item above

1. Record: `scripts/launch_cx26.py --measure` (gives `DXMT_FRAME_LOG` +
   geometry/window/canvas/display logs) on the shipped config, one run off,
   one run on, same recorded input.
2. Compare with `scripts/summarize_frames.py` + the fork's GPU-timing ring
   (`gpu_timing_*`, enabled by `DXMT_BENCH=1` or pacing mode 2).
3. Pixel-identity check via the off-screen replay path (developer tool in the
   private patch: `d3d11-recorder` hooks + replay harness) — pixel output must
   match for load-action/discard changes that must be semantically invisible.
4. Acceptance: median FPS not worse, 1% lows better (or GPU busy lower at
   identical frames), fallback plan documented for the config knobs
   (`DXMT_CONFIG` line removal).

If any config knob turns out to win consistently, land it in the shipping
profile (`config/dxmt-source60-1200.conf`) with a one-line rationale comment
in the same style as the existing file; code-side improvements land as named
patches in `patches/` per repo conventions.

## Sources

- The fork's own docs: `runtime/phase-2/dxmt-source/docs/ow2-pack/README.md`,
  `dxmt.conf`, `AGENTS.md` (AI-code prohibition).
- Working-tree line references quoted above; patch state per
  `git status` of the vendored tree (uncommitted Recall patch set).
- Upstream commit/overlap data: GitHub `3Shain/dxmt` compare and per-commit
  file lists for `ce79ad0`, `0b63990`, `acda4f9`, `e94c312`, `830eb41`
  (verified October 15, 2026).
- The Wine-side merge plan and its measurements: `research/README.md`.
