# Overwatch "entering in-game" crash — analysis (October 16, 2026)

## What crashed

From the session at 00:34–00:39 on October 16 (data folder
`Overwatch2Mac/logs/wine-battlenet.log`, captured by the new Wine log):

- `wine-battlenet.log:179173` records process `07fc` (Overwatch.exe, main
  thread 0x7fc) taking `code=c0000005 (EXCEPTION_ACCESS_VIOLATION)`,
  `addr=0x1425641F9`, `info[1]=0x40` — a read of a member at offset 0x40
  through a null pointer.
- The game's own error report
  (`environment/.../Overwatch/Logs/errors/Overwatch_154088_Retailx64_enUS_1876_...
  _ErrorLog.txt`) agrees: build **2.25.0.0**, "EXCEPTION_ACCESS_VIOLATION
  reading from 0x0000000040", crashing thread 2044 with an all-internal
  stack (`0x1425641F9 → 0x1425621C8 → 0x14258B320 → 0x142706A01…` and Wine's
  unix boundary far above).
- Minidumps exist for three sessions (00:39, and 18:47/20:25 on October 15).
  Parsed locally: **all three share the faulting IP
  `Overwatch.exe + 0x25641F9` and the read-of-0x40 record**, with the same
  caller frame `+0x274BBC4`. Deterministic, not a random hardware fault, and
  identical across days.
- Blizzard's crash reporter (`wldp`/`imagehlp` CODEVIEW enumeration in the
  same log right after the fault) produced `Minidump.dmp` + `ErrorLog.txt`
  next to the game itself, so the game-side artifacts stay authoritative.

## Attribution

Not caused by Recall's stack or the sync experiments:

- The installed runtime is `phase2-20261014.3` (`state.json`),
  which predates `wine-msync-spin`/`wine-fast-yield` — both variables were
  inert strings for this session (the launcher's `engine_sync_patch` probe
  would have confirmed; the app env carries them but nothing compiles them).
- The exception stream's module map classes the faulting IP inside
  `Overwatch.exe` itself; no DXMT/winemetal/ntdll wine modules in the fault
  path, and the crash frame count is internal to the game.
- The same signature reaches Windows players: Blizzard forums (December
  2025, "Repeated crashes in matches only (EXCEPTION_ACCESS_VIOLATION inside
  Overwatch.exe)") describe `c0000005` inside Overwatch.exe at match start /
  mid-match load since Season 18, internal-only stacks, Practice Range
  stable — the same fingerprint.
- A second window — the Season 18 **Vivox voice chat** bug (Bug Report
  987058 and Technical Support threads) — matches what this machine shows:
  this session's ErrorLog lines 84–93 record Vivox cycling between
  `Default System Device` and `Scarlett Solo 4th Gen` around the crash, on a
  Mac exposing many audio endpoints (MacBook mic, iPhone mic, Microsoft
  Teams Audio, a virtual mic device, Scarlett). Vivox's device-switch loop
  is the specific correlated repro those threads document; match start is
  when Vivox reinitializes.

## Workaround order (what the community and this data suggest)

1. Audio hygiene around the machine: unplug the Scarlett (external audio
   interface), or temporarily disable extra virtual microphones (iPhone
   continuity mic, Teams audio device) so Overwatch sees one fixed pair.
   This is the Vivox-cycle workaround with the strongest data behind it.
2. In Overwatch: Settings › Sound → set Voice Chat Enabled off, pick a
   specific default output/input rather than the interface; Relaunch. Vivox
   still initializes, but with one stable device it does not cycle.
3. Practice-Range vs match test to check the "match-only" property.
4. Report upstream: the game's own Minidump + ErrorLog pair in
   `environment/.../Overwatch/Logs/errors/` (never post, only to a private
   Blizzard report if Blizzard asks) — transparent of Wine, identical to the
   Windows reports.

## Possible future Wine-side mitigation (not shipped)

A Wine patch candidate (Recall's own stack, e.g. `winecoreaudio`-side) could
filter the endpoint list supplied to a named game (`WINEMAC_AUDIO_DEVICES`
allowlist, mirroring the `WINE_APP_DPI` pattern in
`patches/wine-win32u-display-log.patch`) so Overwatch's audio enumeration
sees exactly one fixed device. That would remove the Vivox-cycle trigger
on Macs with many virtual/simultaneous inputs. Registered in
`research/dxmt-improvements.md` sibling — park until the Vivox workaround
is validated by the user.

## Sources

- Local artifacts (this Mac, session 2026-10-16 00:39):
  - `Overwatch2Mac/logs/wine-battlenet.log` (Wine SEH trail and the crash
    reporter's module scans),
  - `Overwatch2Mac/logs/battlenet.log` (previous session, old worker naming,
    before this change),
  - `Overwatch2Mac/environment/.../Overwatch/Logs/errors/Overwatch_..._Minidump.dmp`
    ×3 + matching `ErrorLog.txt` files (parsed locally).
- `state.json`: `active_runtime: phase2-20261014.3` (predates the sync
  patches), `stage: battlenet_started` at crash time.
- Blizzard forum threads (season-long, Windows): "Repeated crashes in
  matches only (EXCEPTION_ACCESS_VIOLATION inside Overwatch.exe)" 2025-12;
  "[Vivox Bug] Season 18 bug causes EXCEPTION_ACCESS_VIOLATION" (987058),
  Season 18 "Vivox Bug" technical-support thread; "Constantly crashing after
  Season 18 update" (979223).
