#!/usr/bin/env python3
"""Build Recall the way the worker does, then test it by running it.

Phase 1 (build): a fresh, unsigned bundle through build_native_app.build —
the same routine a release build calls — with the pinned qualified runtime
archive resolved from release.json. Phase 2 (run):
  - bundle checks: Info.plist, helper binaries, the embedded candidate
    contract matches the repository's (the sync environment ships inside it),
    the app binary is arm64;
  - the setup worker, standalone: its read-only preflight and status commands
    against a disposable root, events parsed as JSON;
  - the app itself: opened as a bundle, process must stay alive, window
    phase ends with a clean quit. Skipped with --no-gui.

Reviews only the selftest bundle it created and never touches a previously
installed Recall. Exit nonzero on the first real failure.
"""
from pathlib import Path
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import build_native_app as builder

DEFAULT_OUTPUT = ROOT / 'runtime/build/Recall-selftest.app'
# Ids the helper reported since 1.0; all mean "the run reached a documented
# decision", none of them is a smoke failure. Rosetta covers M-series machines
# before Apple's translator is installed; the macOS gate guards 26.5.
EXPECTED_HELPER_CODES = {'apple_silicon_required', 'macos_26_5_required',
                         'rosetta_required'}


def announce(step, detail):
    print(f'{step}: {detail}', flush=True)


def resolve_runtime_archive():
    release = json.loads((ROOT / 'app/Resources/release.json').read_text())
    wanted = f'{release["runtimeVersion"]}.tar.gz'
    for candidate in (ROOT / 'runtime/build/inputs' / wanted,
                      ROOT / 'runtime/phase-2/artifacts' / wanted,
                      builder.__dict__.get('DEFAULT', None) or
                      ROOT / 'runtime/phase-2/artifacts/phase2-20260927.1.tar.gz'):
        if candidate.is_file():
            return candidate
    raise SystemExit(f'Runtime archive {wanted} is missing from its usual folders')


def build(output):
    if output.exists():
        shutil.rmtree(output)
        announce('build', 'removed the previous selftest bundle')
    # Building the app without an Xcode copy fails in Command Line Tools-only
    # environments: SwiftUI macros need the SDK's macro plugins (they ship with
    # Xcode's platform SDK, missing from the CLT SDK). Honor an explicit
    # DEVELOPER_DIR; otherwise let a locally installed Xcode win.
    xcode = '/Applications/Xcode.app/Contents/Developer'
    if 'DEVELOPER_DIR' not in os.environ and os.path.isdir(xcode):
        os.environ['DEVELOPER_DIR'] = xcode
        # An inherited SDKROOT pinning Xcode's run to the CLT SDK kills macro
        # plugins (SwiftUI @State fails to expand). Point it at Xcode's own SDK.
        os.environ['SDKROOT'] = xcode + '/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'
        announce('build', 'swiftc runs with DEVELOPER_DIR=' + xcode)
    workspace = ROOT / 'runtime/phase-2'
    builder.build(output.resolve(), workspace.resolve(),
                  resolve_runtime_archive().resolve())
    manifest = json.loads(output.with_suffix('.build.json').read_text())
    if manifest['signed']:
        raise SystemExit('Selftest build unexpectedly signs; review the script')
    announce('build', f'{output.name} ({manifest["app_version"]} build '
                      f'{manifest["app_build"]}, runtime {manifest["runtime_sha256"][:12]})')


def bundle_checks(output):
    info = plistlib.loads((output / 'Contents/Info.plist').read_bytes())
    for key, expected in (('CFBundleIdentifier', 'org.overwatch2mac.launcher'),
                          ('CFBundleExecutable', 'Recall'),
                          ('CFBundlePackageType', 'APPL')):
        if info.get(key) != expected:
            raise SystemExit(f'Info.plist {key} is {info.get(key)!r}, not {expected!r}')
    release_repo = json.loads((ROOT / 'app/Resources/release.json').read_text())
    release_bundle = json.loads((output / 'Contents/Resources/release.json').read_text())
    if release_bundle['appVersion'] != release_repo['appVersion']:
        raise SystemExit('The bundle embeds a different app version')
    helpers = output / 'Contents/Helpers'
    for name in ('ow2-setup', 'ow2-pipeline', 'pipeline-prepare', 'pipeline-warm',
                 'libpipeline_prepare.dylib', 'Rosetta Check.app/Contents/MacOS/RosettaCheck'):
        if not (helpers / name).is_file():
            raise SystemExit(f'Helper missing: {name}')
    # The candidate contract is what a launch hands the translator, so a stale
    # embedded copy silently changes game behavior. Compare bytes.
    contract = ROOT / 'docs/candidate-parity-contract.json'
    embedded = output / 'Contents/Resources/candidate-parity-contract.json'
    if embedded.read_bytes() != contract.read_bytes():
        raise SystemExit('Embedded candidate contract differs from the repository')
    shipped = json.loads(embedded.read_text())['environment']
    missing = [key for key in ('WINE_MSYNC_SPIN', 'WINE_FAST_YIELD') if key not in shipped]
    if missing:
        raise SystemExit(f'Contract environment lost the sync options: {missing}')
    languages = info.get('CFBundleLocalizations', [])
    sources = ['en'] + sorted(p.stem for p in
                              (ROOT / 'app/Resources/Localizations').glob('*.json'))
    if languages != sources:
        raise SystemExit(f'Bundle languages {languages} differ from {sources}')
    kind = subprocess.run(['file', '-b', str(output / 'Contents/MacOS/Recall')],
                          capture_output=True, text=True, check=True).stdout.strip()
    if 'arm64' not in kind:
        raise SystemExit(f'App binary is not arm64: {kind}')
    announce('checks', 'Info.plist, helpers, contract (sync options embedded), languages, arch')


def parse_events(text):
    events = []
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def helper_probe(output):
    helper = output / 'Contents/Helpers/ow2-setup'
    with tempfile.TemporaryDirectory(prefix='recall-selftest-root-') as tmp:
        for command in ('preflight', 'status'):
            probe = subprocess.run([str(helper), command, '--root', tmp],
                                   capture_output=True, text=True, timeout=120)
            events = parse_events(probe.stdout)
            codes = {e['code'] for e in events if e.get('stage') == 'error'}
            last = events[-1]['stage'] if events else 'no-output'
            if probe.returncode == 0:
                if command == 'preflight' and 'preflight_ok' not in [e['stage'] for e in events]:
                    raise SystemExit(f'Helper preflight exited 0 without its event')
                announce('helper', f'{command} ok ({last})')
            elif probe.returncode == 1 and codes and codes <= EXPECTED_HELPER_CODES:
                announce('helper', f'{command} documented stop: {sorted(codes)}')
            else:
                raise SystemExit(f'Helper {command} failed: exit {probe.returncode}, '
                                 f'{codes or last}, stderr {probe.stderr[:200]}')


def selftest_processes(output):
    executable = str(Path(output).resolve() / 'Contents/MacOS/Recall')
    rows = subprocess.run(['ps', '-axo', 'pid=,comm='], capture_output=True,
                          text=True, check=True).stdout.splitlines()
    return [(int(pid), row) for row in rows for pid in [row.strip().split(None, 1)[0]]
            if row.strip().split(None, 1)[1:] == [executable]]


def app_probe(output):
    if selftest_processes(output):
        raise SystemExit('A selftest bundle instance is already running; review it first')
    subprocess.run(['/usr/bin/open', str(output)], check=True)
    executable = output.resolve() / 'Contents/MacOS/Recall'
    deadline = time.monotonic() + 15
    pid = None
    while time.monotonic() < deadline:
        matches = selftest_processes(output)
        if matches:
            pid = matches[0][0]
            break
        time.sleep(.2)
    if not pid:
        raise SystemExit('The app did not become a process within 15 s')
    for _ in range(6):
        time.sleep(.5)
        if not selftest_processes(output):
            raise SystemExit('The app quit on its own within the live check')
    announce('run', f'process alive for 3 s after open (pid {pid})')
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and selftest_processes(output):
        time.sleep(.2)
    if selftest_processes(output):
        raise SystemExit('The app ignored a graceful quit within 8 s')
    announce('run', 'quit cleanly after SIGTERM')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT,
                        help='Bundle path (runtime/build/Recall-selftest.app by default; '
                             'my previous builds are recreated)')
    parser.add_argument('--no-gui', action='store_true',
                        help='Build and run the worker checks without opening the window.')
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    build(output)
    bundle_checks(output)
    helper_probe(output)
    if args.no_gui:
        announce('run', 'opened checks skipped (--no-gui)')
    else:
        app_probe(output)
    print('Selftest passed.')
