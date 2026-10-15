# Overwatch main-menu freezes — attributed to the Vivox/Scarlett audio cycle

Research note (October 16, 2026). Companion to `overwatch-crash-0x40.md`;
same tree, same sourcing rules, user-path-free.

## Sessions and instruments

Session 5 (`logs/parity-E83F510C-…`, apps launch root, lean Wine trace
`-all,err+all,+seh,+timestamp,+loaddll`) plus the auto-sampled host stacks
(`Overwatch2Mac/freezes-samples/sample-wineserver-*`,
`sample-parity-E-93671-*`, from `scripts/watch_game_freezes.py`) and the
system unified log for the exact freeze second (`/usr/bin/log show`).

The user reported two stutters; the watcher captured both automatically:
gap 1.64 s at boundary 1091 and 1.69 s at boundary 1249. The frame CSV for
the same game instance (`frame-1864-run2.csv`) recorded 12 gaps > 100 ms of
which one was the giant: **boundary 1092, 7624.7 ms with zero waits,
zero compiles, zero pacing** — the game thread simply did not call Present.

## What the freeze second looked like

| Source | Observation |
|---|---|
| Frame telemetry | 7624.7 ms gap, no internal waits — the stall is above the translator |
| Host sample (game, 2 ~22 MB captures) | every game thread parked inside one unexported ntdll helper (`ntdll.so+0x36f55`, between `_NtCompareTokens`/`_NtSetContextThread`), while the only thread not parked was winemetal's own CoreAnimation run loop waiting normally in `mach_msg` — nothing was spinning |
| Host sample (wineserver) | main thread parked ~96% in its normal kernel-side receive wait — the server was not saturated |
| Wine trace (lean) | trivial churn just before, **absolutely nothing logged inside the 7.6 s window** — the whole process tree went silent at once |
| unified log (`log show` 01:22:04–01:22:22) | CoreAudio overload storm for `AppleUSBAudioEngine:Focusrite:Scarlett Solo 4th Gen` starts 01:22:04, `lateness: 256`/`smallest_buffer_frame_size: 256`, then a **ClientTimeout cascade at ~200/second from 01:22:10**, matching the frame gap's open minute — the game resumed about 01:22:17 |
| `vm_stat` / `sysctl vm.swapusage` | zero swap, zero throttled pages — memory pressure is not the mechanism |

## Attribution

**The audio path, not graphics or the engine.** The chain:

1. The Season-18+ Vivox device-cycling bug (see `overwatch-crash-0x40.md`)
   re-initializes input/output around the Scarlett Solo interface
   (the ErrorLog from the in-match crash shows the same cycling).
2. Wine's audio client (`winecoreaudio.so`) sits in the CoreAudio realtime
   IO path; when the USB endpoint's 256-frame stream goes late, `coreaudiod`
   fires overload + `ClientTimeout` floods — visible in the unified log at
   the exact freeze second.
3. While the audio client is wedged, every thread of the translated
   Overwatch process sits at the same host wait, the whole game freezes at
   once (display and Metal HUD included), and it self-recovers seconds
   later when the audio cycle completes.

This explains all three observed patterns: menu hiding 1.6–11.5 s complete
freezes (several sessions), the in-match `0x40` crash family (same audio
cycle around match start), and the "Practice Range stable, matches
crash/freeze" property (Vivox engages per-match).

## What to do now

Immediate user-level workaround with the strongest data support:
- **Do not play Overwatch with the Scarlett Solo attached** (unplug it)-
  and reduce the other endpoints (iPhone continuity mic, Microsoft Teams
  audio device, virtual microphones) so Overwatch enumerates a single
  fixed pair. Vivox's cycling then has nothing to flip between; the
  incomprehensible cycle storms and the freeze disappear together.

## Follow-up idea registered (needs an engine rebuild, not shipped)

A Wine patch candidate in Recall's own stack, mirroring the
`WINE_APP_DPI` naming pattern from `patches/wine-win32u-display-log.patch`:
**`WINEMAC_AUDIO_DEVICES`** — a per-game allowlist honored by the audio
driver so Overwatch sees exactly one named input/output pair on any Mac
with many audio endpoints.

*Status, October 16 2026, later session: the single-endpoint view is
implemented as the Wine patch on the **`audio-device-filter` branch**
(`WINEMAC_AUDIO_DEVICES` env → one endpoint per direction, off unless the
variable is set). This branch carries the analysis that led to it. The
second user report — the same freeze while playing with **AirPods** —
removes any hardware-specific reading: Bluetooth endpoints switching
hands-free/A2DP roles or output formats go through the exact same reinit
cycle, so the single-endpoint view is the general fix. Validation needs a
rebuilt engine carrying the patch; the hold switch stays the env var until
the A/B: freeze reproduced on → kept on → freeze gone.*

## Sources

- Local session 5 artifacts as shysted above; the watcher's two automatic
  samples (kept in `Overwatch2Mac/freezes-samples/`), frame/geometry/window
  CSVs in `logs/parity-E83F510C-…`; the 2.7 GB deep-trace log of session 4
  (kept at `logs/wine-battlenet.deep-trace-20261016.log`) for the prior API
  threading audit.
- Unified log window 01:22:04–01:22:22 (19,467 lines; audio storms
  in seconds mapped above).
- Blizzard-side context: the Vivox cycle threads (Season 18, Windows) as
  cited in `overwatch-crash-0x40.md`.
