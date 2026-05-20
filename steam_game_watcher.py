#!/usr/bin/env python3
"""Launch a Steam game and wait for its process to exit.

Usage: steam_game_watcher.py <app_id> [executable_name]

Sunshine runs this as the cmd entry. It blocks until the game
process exits, then exits itself — signalling Sunshine to
terminate the stream.

If executable_name is provided, detection uses process-name
matching (handles already-running games cleanly). Without it,
falls back to watching Steam's child process tree.
"""

import os
import subprocess
import sys
import time

import psutil


def find_process_by_name(name: str):
    """Return first psutil.Process matching the given name."""
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and name.lower() in proc.info['name'].lower():
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def wait_for_process_by_name(name: str, timeout: int = 60):
    """Poll for a process matching name to appear, return it or None."""
    for _ in range(timeout):
        proc = find_process_by_name(name)
        if proc:
            return proc
        time.sleep(1)
    return None


def find_steam_process():
    """Find the Steam root process."""
    for proc in psutil.process_iter(['name', 'pid']):
        name = proc.info['name']
        if name and name.lower() in ('steam.exe', 'steam'):
            return proc
    return None


def ensure_steam_bpm():
    """Make sure Steam Big Picture Mode is running (launch detached if not)."""
    if find_steam_process() is not None:
        return

    print("Steam not running. Launching Big Picture Mode...", file=sys.stderr)
    if os.name == 'nt':
        subprocess.Popen(
            ["steam", "-bigpicture"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS,
        )
    else:
        flatpak_check = subprocess.run(
            ['flatpak', 'list', '--app', '--columns=application'],
            capture_output=True, text=True
        ).stdout
        if 'com.valvesoftware.Steam' in flatpak_check:
            cmd = ["flatpak", "run", "com.valvesoftware.Steam", "-bigpicture"]
        else:
            cmd = ["steam", "-bigpicture"]
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Wait for Steam to appear
    for _ in range(30):
        if find_steam_process() is not None:
            print("Steam BPM started.", file=sys.stderr)
            return
        time.sleep(1)
    print("Warning: Steam did not start within 30 seconds.", file=sys.stderr)


def get_steam_children(steam_proc):
    """Return set of child PIDs under the Steam process tree."""
    try:
        return {p.pid for p in steam_proc.children(recursive=True)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return set()


def launch_game(app_id):
    """Launch the Steam game."""
    if os.name == 'nt':
        cmd = ["steam", f"steam://rungameid/{app_id}"]
    else:
        flatpak_check = subprocess.run(
            ['flatpak', 'list', '--app', '--columns=application'],
            capture_output=True, text=True
        ).stdout
        if 'com.valvesoftware.Steam' in flatpak_check:
            cmd = ["flatpak", "run", "com.valvesoftware.Steam", f"steam://rungameid/{app_id}"]
        else:
            cmd = ["steam", f"steam://rungameid/{app_id}"]

    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_pids(pids: set):
    """Block until all PIDs in the set have exited."""
    while True:
        still = {pid for pid in pids if psutil.pid_exists(pid)}
        if not still:
            break
        time.sleep(2)


def wait_for_process_exit(proc):
    """Block until a process exits. Uses polling (works for non-child processes)."""
    while True:
        try:
            if not proc.is_running():
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        time.sleep(2)


def track_by_executable(app_id: str, executable_name: str):
    """Track game via its executable name (handles already-running)."""
    existing = find_process_by_name(executable_name)
    if existing:
        print(f"Game already running (PID {existing.pid}). Waiting for exit...", file=sys.stderr)
        wait_for_process_exit(existing)
        print("Game process exited.", file=sys.stderr)
        return

    print(f"Game not running yet, launching...", file=sys.stderr)
    ensure_steam_bpm()
    launch_game(app_id)
    game = wait_for_process_by_name(executable_name)
    if not game:
        print(f"Could not detect process '{executable_name}' after launch.", file=sys.stderr)
        return

    print(f"Game detected (PID {game.pid}). Waiting for exit...", file=sys.stderr)
    wait_for_process_exit(game)
    print("Game process exited.", file=sys.stderr)


def track_via_steam_children():
    """Fallback: watch Steam's child tree for new processes after launch."""
    steam = find_steam_process()
    if steam is None:
        print("Steam not running. Waiting 2 minutes then exiting.", file=sys.stderr)
        time.sleep(120)
        return

    children_before = get_steam_children(steam)
    game_pids = set()
    for _ in range(30):
        time.sleep(1)
        children_now = get_steam_children(steam)
        new = children_now - children_before
        if new:
            game_pids = new
            break
        children_before = children_now

    if not game_pids:
        print("Could not detect game process via Steam children.", file=sys.stderr)
        time.sleep(300)
        return

    print(f"Game detected via Steam (PIDs: {sorted(game_pids)}). Waiting...", file=sys.stderr)
    wait_for_pids(game_pids)
    print("Game process exited.", file=sys.stderr)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <app_id> [executable_name]", file=sys.stderr)
        sys.exit(1)

    app_id = sys.argv[1]
    executable_name = sys.argv[2] if len(sys.argv) > 2 else None

    if executable_name:
        track_by_executable(app_id, executable_name)
    else:
        track_via_steam_children()


if __name__ == "__main__":
    main()
