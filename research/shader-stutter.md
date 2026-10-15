# Stutters & shader compilation — what the DXMT fork still offers

Research only; no code changes. Companion to `research/dxmt-improvements.md`
(same tree, same AGENTS.md constraint: anchors for a human implementer, no
AI-generated diffs for the upstream project). Line numbers are the working
tree state Recall builds.

*Status, October 16, 2026: of this list, only the config-level piece (#2) is
wired — `scripts/launch_cx26.py --prewarm-limit N` (default 128) — which
needs a new Wine/MSync engine build to take effect. The `releaseShaderIR`/
prewarm machinery referenced here shipped in the fork; candidates #1, #3-#5
remain dxmt-side human work. The Wine-side spin candidate's patch
(`patches/wine-msync-spin.patch`) and the fast-yield patch are implemented
off-by-default; see `patches/README.md`.*

## The machinery already in place (verified in code)

| Mechanism | Anchor | What it does |
|---|---|---|
| Background prepare, shaders only | `src/d3d11/d3d11_pipeline_cache.cpp:364-396`, `:186` | With `DXMT_PREPARE_SHADERS=1`, two LOWEST-priority workers translate predicted variants when shaders + input layouts are created: pixel shaders as the plain variant, vertex shaders once per input-layout signature; layouts and shaders are matched by a hash of the DXBC input-signature chunk |
| Variant dedupe | `d3d11_shader.cpp:81` (`find_cached_variant(variant_digest_)`) | Variants translating identically share one cache entry |
| Cache-only prepare | `d3d11_shader.cpp:140` (`SetCacheOnly`, "no Metal function kept") | Prepare warms the translate + cache write without materializing an MTLFunction |
| On-demand compile pool | `d3d11_pipeline_cache.cpp:700-701` + `dxmt_tasks.hpp` | 8 normal-priority workers in the shipped profile (4 previously saturated, queue waits 223 ms per `config/dxmt-source60-1200.conf:6`) |
| Per-frame GPU timing ring | `src/dxmt/dxmt_command_queue.cpp:244-258` (`DXMT_BENCH=1`) | busy / active / idle spans per frame — measures whether stutter work landed |
| Pack + learned pipelines | `src/winemetal/unix/pipeline_cache.c:251-266, 484-544, 651-700` | Startup budget/deadline (`DXMT_PIPELINE_CACHE_PREWARM_MS`), archives checked on all cores, slowest-first order, `load.json` budget from Recall's Shader memory setting; archive population happens only inside the prewarm window (`:673`) |
| Worst-stall prevention | releaseShaderIR + v0.80 uncached-PSO AIR fix | Footprint stays ~4 GB; first-run per-pipeline compiles no longer take seconds |
| Trace writer | `tests/shader_trace/run.py` (sanitizer harness), `run_wine.py` | ASan/UBSan-checked queue/drop accounting for `DXMT_SHADER_LOG`-style traces, survives `_Exit`/`TerminateProcess`; ready-publication and wait-return use one monotonic clock |

The structural fact that defines what remains: **`GetPipeline` blocks the game
thread on first use** (`src/d3d11/d3d11_pipeline.hpp:148`: "the current thread
is blocked if it's not ready"). Everything the stack does is arranging for that
block to never execute during a match: pack hits (ms), variant cache hits (ms),
and only a genuinely novel pipeline/variant pays the full compile.

## Remaining improvement candidates, ranked

### 1. Predict pipelines, not just shader variants (rank 1)

What prepare covers today is the *shader arm* of a pipeline. The resident
state that actually keys a Metal PSO — blend/raster state, topology class,
sample count, RTV/DSV formats, sample mask — still needs a first draw before
its descriptor is seen, so its first use blocks. Since the trace data Recall
collects in sessions (and `prepare_dxmt_pipelines.py`'s offline batches)
already records which descriptor distribution a map/hero produces, the
reasonable extension is: during loading screens, warm the top-N descriptors of
the outgoing distribution in the cache-only pool, so the first in-match draw
finds an archive/PSO hit instead of compiling from a cold path.

Human port notes:
- Source candidates from the pack's own learned descriptors
  (`pipeline_cache.c:remember` entries) rather than a second prediction
  model — no new data format.
- Cap by the same startup deadline/budget machinery, submit through the main
  scheduler (not the prepare pool) only while no match is loading.
- Accept only if pack-hit+work on the first match window improves: measure
  via `FrameTelemetry` completion spans + GPU busy in the custom-match replay.

### 2. Widen the prewarm cap (config-only A/B, rank 2)

`DXMT_PIPELINE_CACHE_PREWARM_LIMIT=128` (shipping env) with a **10 s** budget.
A pack learned over weeks holds far more than 128 pipelines; the
slowest-first loader means the tail is exactly what causes the first stutter
of a session. A/B: limit 128 → 256 (budget unchanged), comparing prewarm
counts (`prewarm`/`prewarm_complete` events, `deadline_exceeded` flag) and the
first minute's hitch rate of the practice-range scenario. The memory budget
already bounds resident cost (`load.json`), so the knob is safe to raise; the
risk is only slower app start.

### 3. In-session capture of novel pipelines (rank 3, off by default)

Verified: archive population happens **only** inside the prewarm window
(`pipeline_cache.c:673-695`); post-deadline novel PSOs are never archived live
— only the standalone preparation tool writes the pack afterwards. That means
a session that crashes mid-match, or a descriptor first seen while the machine
is busy, costs a full first-use compile again next launch unless the prep tool
runs. Candidate: on the compile pool's completion path, a rate-limited
`addRenderPipelineFunctionsWithDescriptor` for novel keys (same `remember`
ledger), env-gated (e.g. `DXMT_PIPELINE_CACHE_LIVE_ADD=200`). Off by default
because Recall normally runs the prep tool between sessions; value appears
only for prep-less flows or crash mid-session recovery.

### 4. More prepare/rescan workers (rank 4, env knob)

`prepare_scheduler_{2, THREAD_PRIORITY_LOWEST}` is hard-coded. If
`DXMT_SHADER_LOG` shows the prepare backlog outliving loading screens, expose
`DXMT_PREPARE_WORKERS` (still LOWEST priority; the two-pool strict separation
is what keeps prepare from hurting the time-critical path — preserve it).
Cheap to try right after Rank 2's A/B since the same replay harness runs both.

### 5. IR re-materialization hot spots (rank 5, measure first)

releaseShaderIR re-parses DXBC→IR on every cache *miss* where the IR was
released (`d3d11_shader.cpp:111-121` `ir_ensure()`; the trace already stamps
`ir_restore_us` separately from `translate_us`/`metal_us`). If the burst of
first-match misses shows `ir_restore_us` dominating the residual worst frames,
a small bounded LRU of recently used IR (the same DXBC backing store, just
delayed release) is the minimal countermeasure. Do not touch without the
per-stage numbers; the instrumentation to decide already exists.

### 6. Parked / cross-links

- **DiscardResource → `LoadActionDontCare`** (`research/dxmt-improvements.md`
  rank 1): GPU work is a stutter second-order term; a lighter frame queue
  collides with fewer hitches. Not a shader item, keep on its own track.
- **MSync adaptive spin** (`research/README.md` rank 1): DXMT's waits land in
  MSync's `ulock_wait` (`util_address_wait.hpp`, "notify_* resume 1–3 ms
  late"), so the same Wine patch tightens both engine-level and game-level
  wake latency.
- **`d3d11.shaderCompilerThreads` 8 → 9/10**: M1 Pro is 10-core; the shipped
  pool leaves headroom for the game + encode threads. Only worth trying
  during the loading-screen window; expected effect tiny. Keep 8 unless
  trace shows idle-queue ends followed by still-unmet demand.
- **TIME_CRITICAL priority in the game thread**: the historic worst design
  (Wine fan note: upstream DXMT's default) — the shipped profile already
  sets `shaderCompilerNormalPriority = True`; never regress that.

## Measurement plan (shared)

1. `scripts/launch_cx26.py --measure` (it sets `DXMT_FRAME_LOG` +
   `DXMT_SHADER_LOG` + geometry/window/display logs, per `launch_cx26.py:160-166`)
   on the shipped profile with the recorded replay input.
2. Split trace rows by stage from `ShaderCompileTrace` — per-source stamps
   (`cache_read_us`, `ir_restore_us`, `translate_us`, `metal_us`,
   `cache_write_us`) already segregate where the blockage time goes; check
   with `tests/shader_trace` accounting invariants (queue/drop counts).
3. Frame-level: `scripts/summarize_frames.py` + the GPU busy/idle ring to see
   whether a light shader path also frees the GPU (Tier 1 metric: worst-frame
   duration; Tier 2: 1% lows of the same replay).
4. Determinism: run loopback via the off-screen replay (developer recorder)
   and require pixel-identical output before accepting any change to what
   caches choose to load.

## Sources

- Working-tree anchors quoted above (`runtime/phase-2/dxmt-source`, HEAD
  `c5dc3a0` + uncommitted Recall patch stack).
- `config/dxmt-source60-1200.conf`, `config/dxmt-ow2.conf`, and the candidate
  contract (`app/... candidate_contract.generated.h`) for the shipping env.
- `tests/shader_trace/README.md` (trace-writer guarantees), `tests/pipeline_*`
  and `scripts/prepare_dxmt_pipelines.py` (offline pipeline batch tooling).
- `research/README.md` and `research/dxmt-improvements.md` (Wine-side and
  broader DXMT candidates).
- Prior measurements: `docs/PERFORMANCE.md`, `docs/HOW-IT-WORKS.md` (worst
  frame 470→148 ms from shader preparation work), `docs/SOURCES.json`
  runtime_policy.
