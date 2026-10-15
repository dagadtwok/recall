#!/usr/bin/env python3
"""Validate the logic of patches/wine-msync-spin.patch in a host harness.

Extracts the helper block that the patch adds to dlls/ntdll/unix/msync.c
(the adaptive-spin scheduler and msync_single_satisfied), compiles it with
clang against the engine's real struct layout, and asserts the state
transitions and the predicate's equivalence to the loop it refactors.
Run: python3 tests/msync_spin/run.py
"""
from pathlib import Path
from subprocess import run as run_cmd
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/wine-msync-spin.patch"

PREAMBLE = '''#include <stdlib.h>
#include <stdio.h>
#include <string.h>

typedef int BOOLEAN;
#define STATUS_PENDING 259

/* The engine's layout (msync.c), mirrored so (int *)obj_shm is the same word
 * the patched helpers sleep on. A static assert keeps the mirror honest. */
struct mutex
{
    int tid;
    int count;
    unsigned short msync_type;
    unsigned short refcount;
    int multiple_waiters;
};
enum msync_type
{
    MSYNC_SEMAPHORE = 1,
    MSYNC_AUTO_EVENT,
    MSYNC_MANUAL_EVENT,
    MSYNC_MUTEX,
    MSYNC_AUTO_SERVER,
    MSYNC_MANUAL_SERVER,
};

static int self_tid(void) { return 0x501; }

'''

HARNESS_MAIN = '''int main(int argc, char **argv)
{
    const char *mode = argc > 1 ? argv[1] : "";
    int val;

    if (!strcmp(mode, "off"))
    {
        const char *cases[] = {"0", "abc", ""};
        if (msync_spin_iters_for() != 0) return 1;
        msync_pause(); /* the wait function calls it from its spin loop */
        for (unsigned i = 0; i < sizeof(cases) / sizeof(*cases); i++)
        {
            if (setenv("WINE_MSYNC_SPIN", cases[i], 1)) return 7;
            if (msync_spin_iters_for() != 0) return 2;
        }
        return 0;
    }

    if (!strcmp(mode, "window"))
    {
        if (setenv("WINE_MSYNC_SPIN", "64", 1)) return 7;
        if (msync_spin_iters_for() != 16) return 1;   /* fresh thread probes */
        msync_spin_win();
        if (msync_spin_iters_for() != 16) return 2;   /* one win still probes */
        msync_spin_win();
        if (msync_spin_iters_for() != 64) return 3;   /* two wins earned it */
        msync_spin_win();
        if (msync_spin_iters_for() != 64) return 4;   /* counter capping is fine */
        return 0;
    }

    if (!strcmp(mode, "window_small"))
    {
        if (setenv("WINE_MSYNC_SPIN", "12", 1)) return 7;
        if (msync_spin_iters_for() != 12) return 1;   /* under the probe cap */
        msync_spin_win();
        if (msync_spin_iters_for() != 12) return 2;
        return 0;
    }

    if (!strcmp(mode, "window_huge"))
    {
        if (setenv("WINE_MSYNC_SPIN", "999999", 1)) return 7;
        if (msync_spin_iters_for() != 16) return 1;
        msync_spin_win();
        msync_spin_win();
        if (msync_spin_iters_for() != 16384) return 2;   /* clamped */
        return 0;
    }

    if (!strcmp(mode, "window_loses"))
    {
        if (setenv("WINE_MSYNC_SPIN", "64", 1)) return 7;
        msync_spin_win();
        msync_spin_win();
        if (msync_spin_iters_for() != 64) return 1;
        msync_spin_lose();
        if (msync_spin_iters_for() != 16) return 2;   /* long wait throttles */
        return 0;
    }

    if (!strcmp(mode, "predicate"))
    {
        struct mutex free_mutex = {.msync_type = MSYNC_MUTEX};          /* tid 0 */
        struct mutex my_mutex = {.msync_type = MSYNC_MUTEX};
        struct mutex other_mutex = {.msync_type = MSYNC_MUTEX};
        struct mutex abandoned_mutex = {.msync_type = MSYNC_MUTEX};
        struct mutex signaled = {.msync_type = MSYNC_AUTO_EVENT};
        struct mutex un_signaled = {.msync_type = MSYNC_AUTO_EVENT};
        int tid = self_tid();

        __atomic_store_n(&my_mutex.tid, tid, __ATOMIC_SEQ_CST);
        __atomic_store_n(&other_mutex.tid, 0x777, __ATOMIC_SEQ_CST);
        __atomic_store_n(&abandoned_mutex.tid, ~0, __ATOMIC_SEQ_CST);
        __atomic_store_n(&signaled.tid, 5, __ATOMIC_SEQ_CST);

        if (!msync_single_satisfied(&free_mutex, tid, &val) || val != tid) return 1;
        if (!msync_single_satisfied(&abandoned_mutex, tid, &val) || val != tid) return 2;
        if (msync_single_satisfied(&my_mutex, tid, &val) || val != tid) return 3;
        if (msync_single_satisfied(&other_mutex, tid, &val) || val != 0x777) return 4;
        if (!msync_single_satisfied(&signaled, tid, &val)) return 5;
        if (msync_single_satisfied(&un_signaled, tid, &val) || val != 0) return 6;
        return 0;
    }
    return 100;
}
'''

MODES = ("off", "window", "window_small", "window_huge", "window_loses", "predicate")


def helper_block_from_patch():
    """The helpers live entirely in the patch's first hunk (the second hunk
    replaces msync_wait_single). Hunk one's added lines are the block."""
    text = PATCH.read_text()
    parts = text.split("\n@@ ")
    if len(parts) < 3:
        raise AssertionError("patch lost its second hunk header")
    first, rest = parts[1], parts[2:]
    for hunk in rest:
        if any("msync_wait_single" in line for line in hunk.splitlines()):
            break
    else:
        raise AssertionError("no hunk touches msync_wait_single")
    added = [line[1:] for line in ("\n@@ " + first).splitlines()
             if line.startswith("+")]
    block = "\n".join(added)
    if "msync_single_satisfied" not in block:
        raise AssertionError("first hunk lacks the helper block")
    if "static inline NTSTATUS" in block:
        raise AssertionError("first hunk leaked the wait function")
    return block


def main():
    if not PATCH.is_file():
        sys.exit(f"{PATCH} is missing")
    with tempfile.TemporaryDirectory(prefix="msync-spin-") as tmp:
        tmp = Path(tmp)
        source = tmp / "harness.c"
        source.write_text(PREAMBLE + helper_block_from_patch() + HARNESS_MAIN)
        run_cmd(["cc", "-Wall", "-Wextra", "-Werror", "-arch", "arm64",
                 str(source), "-o", str(tmp / "harness")], check=True)
        run_cmd(["cc", "-Wall", "-Wextra", "-Werror", "-arch", "x86_64",
                 "-fsyntax-only", str(source)], check=True)
        print("harness compiled (arm64 object, x86_64 syntax)")
        for mode in MODES:
            proc = run_cmd([str(tmp / "harness"), mode])
            if proc.returncode != 0:
                sys.exit(f"case {mode} failed with exit {proc.returncode}")
            print(f"case {mode}: ok")


if __name__ == "__main__":
    main()
