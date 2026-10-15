#!/usr/bin/env python3
"""Sample the game's host stacks whenever its frames stall.

Watches the newest parity session's frame CSVs (DXMT_FRAME_LOG); when the
game stops presenting for longer than --gap seconds, finds the Overwatch
process and wineserver, and captures host stack samples with /usr/bin/sample
into a temp folder, one per freeze. Prints one line per freeze with the
output paths. Run from the repository root while an instrumented session is
open, in its own terminal:

    python3 scripts/watch_game_freezes.py        (then reproduce the freeze)
"""
from pathlib import Path
import argparse
import csv
import subprocess
import time

HOME = Path.home()
LOGS = HOME / 'Library/Application Support/Overwatch2Mac/logs'
MAX_GAP_US = 4_000_000  # watch only freezes at least this long


def newest_session():
    folders = sorted((LOGS).glob('parity-*'), key=lambda p: p.stat().st_mtime)
    return folders[-1] if folders else None


def latest_frame_row(session_folders):
    """The tail row/interface of each frame csv in the newest folders; CSV rows
    only ever append, so the last line carries the freshest boundary."""
    rows = []
    for folder in session_folders:
        for csv_path in folder.glob('frame-*.csv'):
            try:
                with open(csv_path, errors='replace') as stream:
                    lines = stream.readlines()[-3:]
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    parts = line.strip().split(',')
                    if len(parts) < 6 or not parts[4].isdigit():
                        continue
                    unix_us = int(parts[4])
                    rows.append((unix_us, csv_path.name, int(parts[1]), folder.name))
                    break
                except (ValueError, IndexError):
                    continue
    return max(rows, default=None)


def process_pids():
    out = subprocess.run(['ps', '-axo', 'pid=,comm='], capture_output=True,
                         text=True, check=True).stdout.splitlines()
    game = None
    server = None
    for line in out:
        pid_text, _, rest = line.strip().partition(' ')
        path = rest.strip()
        if not pid_text.isdigit():
            continue
        if game is None and path.endswith(('Overwatch.exe', 'Overwatch.app/Contents/MacOS/Overwatch')):
            game = int(pid_text)
        if server is None and path.endswith('/bin/wineserver'):
            server = int(pid_text)
    return game, server


def sample(pid, seconds, folder, tag):
    """sample <pid> with a hard deadline, writing to disk as it runs.

    sample can hang against a frozen Rosetta process; running it with stdout
    on the file and killing the own subprocess after the deadline keeps
    whatever partial stacks it printed instead of losing the capture."""
    if pid is None:
        return None
    out = folder / f'sample-{tag}-{pid}-{int(time.time())}.txt'
    folder.mkdir(parents=True, exist_ok=True)
    with open(out, 'w') as stream:
        proc = subprocess.Popen(['/usr/bin/sample', str(pid), str(seconds), '-mayDie'],
                                stdout=stream, stderr=stream,
                                stdin=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            proc.wait(timeout=seconds + 8)
            stream.write(f'\n[sample exit {proc.returncode}]\n')
        except subprocess.TimeoutExpired:
            stream.write('\n[sample timeout — partial coverage retained]\n')
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=10)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gap', type=float, default=1.5,
                        help='seconds without a frame before sampling (default 1.5)')
    parser.add_argument('--sessions', type=int, default=2,
                        help='how many newest parity folders to watch (default 2)')
    args = parser.parse_args()
    watched = set()
    print(f'watching [{LOGS}] for freezes > {args.gap}s; Ctrl-C stops', flush=True)
    try:
        while True:
            session_folders = sorted((LOGS).glob('parity-*'),
                                     key=lambda p: p.stat().st_mtime)[-args.sessions:]
            latest = latest_frame_row(session_folders)
            if latest:
                unix_us, stream, boundary, folder = latest
                now_us = int(time.time() * 1_000_000)
                gap = now_us - unix_us
                if gap > args.gap * 1_000_000:
                    key = (folder, stream, boundary)
                    if key not in watched and gap < MAX_GAP_US:
                        watched.add(key)
                        game, server = process_pids()
                        tag = f'{folder[:8]}-{game or "nogame"}'
                        print(f'FREEZE: gap {gap/1e6:.2f}s after boundary {boundary} '
                              f'({stream} in {folder[:8]}); game pid {game}, wineserver {server}; '
                              'sampling…', flush=True)
                        try:
                            out = sample(server, 2, LOGS.parent / 'freezes-samples', 'wineserver')
                            if out:
                                print(f'  wineserver sample: {out.name}', flush=True)
                        except Exception as exc:
                            print(f'  wineserver sample failed: {type(exc).__name__}', flush=True)
                        try:
                            out = sample(game, 3, LOGS.parent / 'freezes-samples', tag)
                            if out:
                                print(f'  freeze sample: {out.name}', flush=True)
                        except Exception as exc:
                            print(f'  game sample failed: {type(exc).__name__}', flush=True)
            time.sleep(0.25)
    except KeyboardInterrupt:
        print('stopped.', flush=True)


if __name__ == '__main__':
    main()
