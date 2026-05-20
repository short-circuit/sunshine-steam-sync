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

import logging
import os
import subprocess
import sys
import time

import psutil


log = logging.getLogger(__name__)


def find_process_by_name(name: str):
    """Return first psutil.Process whose name matches *name* (case-insensitive, exact
    match preferred, substring fallback for Linux/Proton compatibility)."""
    name_lower = name.lower()
    candidates = []
    for proc in psutil.process_iter(['name']):
        try:
            pname = proc.info['name']
            if not pname:
                continue
            pname_lower = pname.lower()
            if pname_lower == name_lower:
                return proc
            if name_lower in pname_lower:
                candidates.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return candidates[0] if candidates else None


def wait_for_process_by_name(name: str, timeout: int = 120):
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


def _steam_cmd(args: list[str]) -> list[str]:
    """Return the appropriate command vector for running a Steam CLI, detecting
    Flatpak vs. native Steam on Linux."""
    if os.name == 'nt':
        return ["steam"] + args
    flatpak_check = subprocess.run(
        ['flatpak', 'list', '--app', '--columns=application'],
        capture_output=True, text=True
    ).stdout
    if 'com.valvesoftware.Steam' in flatpak_check:
        return ["flatpak", "run", "com.valvesoftware.Steam"] + args
    return ["steam"] + args


def ensure_steam_bpm():
    """Make sure Steam Big Picture Mode is active and visible.

    If Steam is not running at all, launches it in BPM.  If it *is*
    running (e.g. background systray), tells it to switch to BPM.

    Returns True if Steam was already running, False if it was started
    by this call (or we gave up waiting).
    """
    running = find_steam_process() is not None
    if running:
        log.info("Steam already running — switching to Big Picture Mode")
    else:
        log.info("Steam not running — launching Big Picture Mode")

    subprocess.Popen(
        _steam_cmd(["steam://open/bigpicture"]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **({} if os.name == 'nt' else {'start_new_session': True}),
    )

    for _ in range(30):
        if find_steam_process() is not None:
            log.info("Steam BPM is active")
            return running
        time.sleep(1)
    log.warning("Steam did not start within 30 seconds")
    return False


def get_steam_children(steam_proc):
    """Return set of child PIDs under the Steam process tree."""
    try:
        return {p.pid for p in steam_proc.children(recursive=True)}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return set()


def launch_game(app_id):
    """Launch the Steam game."""
    subprocess.Popen(
        _steam_cmd([f"steam://rungameid/{app_id}"]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


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
    log.info("track_by_executable(app_id=%s, executable=%s)", app_id, executable_name)

    existing = find_process_by_name(executable_name)
    if existing:
        log.info("Game already running (PID %s) — waiting for exit", existing.pid)
        wait_for_process_exit(existing)
        log.info("Game process exited")
        return

    log.info("Launching game %s", app_id)
    ensure_steam_bpm()
    log.info("Waiting 8s for Steam BPM to settle before launching game")
    time.sleep(8)
    launch_game(app_id)
    game = wait_for_process_by_name(executable_name)
    if not game:
        log.warning("Could not detect process '%s' after launch", executable_name)
        return

    log.info("Game detected (PID %s) — waiting for exit", game.pid)
    wait_for_process_exit(game)
    log.info("Game process exited")


def track_via_steam_children(app_id: str):
    """Fallback: ensure Steam BPM is running, launch *app_id*, then watch
    Steam's child process tree for new PIDs to appear."""
    log.info("track_via_steam_children(app_id=%s)", app_id)
    ensure_steam_bpm()
    log.info("Waiting 8s for Steam BPM to settle before launching game")
    time.sleep(8)
    launch_game(app_id)

    steam = find_steam_process()
    if steam is None:
        log.warning("Steam not running after launch — waiting 2 minutes then exiting")
        time.sleep(120)
        return

    children_before = get_steam_children(steam)
    log.debug("Steam children before launch: %s", sorted(children_before))
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
        log.warning("Could not detect any new Steam child process — sleeping 5 min then exiting")
        time.sleep(300)
        return

    log.info("Game detected via Steam (PIDs: %s) — waiting for exit", sorted(game_pids))
    wait_for_pids(game_pids)
    log.info("Game process exited")


def setup_logging():
    log.setLevel(logging.DEBUG)
    handler = logging.FileHandler('steam-game-watcher.log', mode='a')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    log.addHandler(handler)


def main():
    setup_logging()
    log.info("=== steam-game-watcher started ===")
    log.info("Args: %s", sys.argv[1:])

    if len(sys.argv) < 2:
        log.error("Usage: %s <app_id> [executable_name]", sys.argv[0])
        sys.exit(1)

    app_id = sys.argv[1]
    executable_name = sys.argv[2] if len(sys.argv) > 2 else None
    log.info("app_id=%s executable_name=%s", app_id, executable_name)

    try:
        if executable_name:
            track_by_executable(app_id, executable_name)
        else:
            track_via_steam_children(app_id)
    except Exception as e:
        log.exception("Unhandled exception")
        raise
    finally:
        log.info("=== steam-game-watcher exiting ===")


if __name__ == "__main__":
    main()
