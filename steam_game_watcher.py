#!/usr/bin/env python3
"""Launch a Steam game and wait for its process to exit.

Usage:
    steam_game_watcher.py <app_id> [executable_name]
    steam_game_watcher.py --help

Sunshine runs this as the `cmd` entry. It blocks until the game process
exits, then exits itself — signalling Sunshine to terminate the stream.

Detection deliberately avoids fragile process-name heuristics that caused
intermittent behaviour. Instead it:
  * attaches immediately if an EXACT name match is already running (never
    relaunches an in-progress game);
  * otherwise starts Steam BPM, launches the game, and waits for a *new*,
    *stable* process to appear — either one whose name exactly matches the
    supplied executable, or (fallback) any new long-lived process that
    spawned under the Steam process tree;
  * and waits for that process to exit, guarding against PID reuse so it
    never hangs on an unrelated process that inherited the PID.
"""

import argparse
import logging
import os
import subprocess
import sys
import time

import psutil

log = logging.getLogger(__name__)

# How long (seconds) a newly-appeared process must survive before we trust
# it is the real game and not a transient Steam helper (webhelper, overlay).
STABILITY_WINDOW = 5
# How long (seconds) to keep hunting for the game after launching it.
STARTUP_TIMEOUT = 180


def _name_of(proc) -> str:
    """Return the lowercased process name, tolerating races/died processes."""
    try:
        info = proc.info
        if not info:
            return ''
        return (info.get('name') or '').lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ''


def snapshot_pids() -> set:
    """Return the set of PIDs currently running (used as a launch baseline)."""
    pids = set()
    for proc in psutil.process_iter():
        try:
            pids.add(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids


def find_process_exact(name: str):
    """Return the first process whose name matches *name* exactly
    (case-insensitive). No substring matching — that was a source of
    false positives that made the watcher end streams early."""
    target = (name or '').lower()
    if not target:
        return None
    for proc in psutil.process_iter(['name']):
        if _name_of(proc) == target:
            return proc
    return None


def find_steam_process():
    """Find the Steam client root process."""
    for proc in psutil.process_iter(['name']):
        name = _name_of(proc)
        if name in ('steam.exe', 'steam'):
            return proc
    return None


def _is_descendant(proc, root) -> bool:
    """Return True if *proc* sits anywhere under *root*'s process tree."""
    if root is None:
        return False
    try:
        if proc.pid == root.pid:
            return True
        for parent in proc.parents():
            if parent.pid == root.pid:
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return False


def _steam_cmd(args: list[str]) -> list[str]:
    """Return the appropriate command vector for running a Steam CLI,
    detecting Flatpak vs. native Steam on Linux."""
    if os.name == 'nt':
        return ["steam"] + args
    try:
        flatpak_check = subprocess.run(
            ['flatpak', 'list', '--app', '--columns=application'],
            capture_output=True, text=True,
        ).stdout
    except FileNotFoundError:
        flatpak_check = ''
    if 'com.valvesoftware.Steam' in flatpak_check:
        return ["flatpak", "run", "com.valvesoftware.Steam"] + args
    return ["steam"] + args


def steam_client_ready() -> bool:
    """Return True once the Steam client UI is actually up (a proxy for
    BPM having settled) rather than merely a `steam` bootstrap process."""
    for proc in psutil.process_iter(['name']):
        name = _name_of(proc)
        if name and 'steamwebhelper' in name:
            return True
    return False


def ensure_steam_bpm() -> bool:
    """Make sure Steam Big Picture Mode is active and visible.

    Returns True if Steam was already running, False if we had to start it
    (or gave up waiting).
    """
    was_running = find_steam_process() is not None
    if was_running:
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
            return was_running
        time.sleep(1)
    log.warning("Steam did not start within 30 seconds")
    return False


def wait_for_steam_ui(timeout: int = 20) -> None:
    """Best-effort wait until the client UI is up before launching a game.
    Avoids firing the game at Steam before it can handle the request."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if steam_client_ready():
            return
        time.sleep(1)
    log.debug("Steam client UI not detected within %ss — proceeding anyway", timeout)


def launch_game(app_id):
    """Launch the Steam game."""
    subprocess.Popen(
        _steam_cmd([f"steam://rungameid/{app_id}"]),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def current_candidates(executable_name: str, steam_root, baseline: set) -> dict:
    """Return {pid: Process} for processes that look like the just-launched
    game. A candidate must be NEW (not in the baseline), must still be ALIVE
    (zombies don't count — a process that already terminated isn't the game
    we're still waiting for), and must be either an exact executable-name
    match or (fallback) a descendant of Steam."""
    target = (executable_name or '').lower()
    candidates = {}
    for proc in psutil.process_iter(['name', 'status']):
        pid = proc.pid
        if pid in baseline:
            continue
        try:
            if proc.info.get('status') == psutil.STATUS_ZOMBIE:
                continue
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        name = _name_of(proc)
        if not name:
            continue
        if target and name == target:
            candidates[pid] = proc
        elif steam_root is not None and _is_descendant(proc, steam_root):
            candidates[pid] = proc
    return candidates


def wait_for_new_game(executable_name: str, steam_root, baseline: set):
    """Poll for a stable, new game process and return it, or None on timeout.

    A candidate only counts once it has survived for STABILITY_WINDOW
    seconds, which filters out transient Steam helper processes (the source
    of premature stream termination)."""
    deadline = time.time() + STARTUP_TIMEOUT
    first_seen = {}  # pid -> first observation time
    while time.time() < deadline:
        now = time.time()
        current = current_candidates(executable_name, steam_root, baseline)

        # Drop candidates that have already disappeared.
        for pid in list(first_seen):
            if pid not in current:
                del first_seen[pid]

        for pid, proc in current.items():
            if pid not in first_seen:
                first_seen[pid] = now
            if now - first_seen[pid] >= STABILITY_WINDOW:
                return proc
        time.sleep(1)

    log.warning(
        "Could not detect a game process after launch (timeout %ss)",
        STARTUP_TIMEOUT,
    )
    return None


def wait_process_exit(proc) -> None:
    """Block until the process *proc* exits.

    Uses the process's creation time to guard against PID reuse, so we never
    wait on an unrelated process that inherited the PID."""
    try:
        start_time = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return
    while True:
        try:
            alive = psutil.Process(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        try:
            if abs(alive.create_time() - start_time) > 0.001:
                return  # PID was reused -> original game has exited
            if not alive.is_running():
                return
            if alive.status() == psutil.STATUS_ZOMBIE:
                return
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return
        time.sleep(1)


def track_game(app_id: str, executable_name: str) -> None:
    """Ensure Steam BPM is up, launch the game (or attach to it if already
    running), then block until the real game process exits."""
    # 1. If an exact-name match is already running, just attach to it.
    if executable_name:
        existing = find_process_exact(executable_name)
        if existing:
            log.info("Game already running (PID %s) — waiting for exit", existing.pid)
            wait_process_exit(existing)
            log.info("Game process exited")
            return

    # 2. Make sure Steam BPM is up before we rely on its process tree.
    started = ensure_steam_bpm()
    if started is False:
        # We launched Steam ourselves (or it never came up) — give the client
        # a moment to actually initialise before firing the game request.
        wait_for_steam_ui()

    # 3. Snapshot PIDs AFTER Steam is up so Steam's own startup children
    #    aren't mistaken for the game.
    baseline = snapshot_pids()
    steam_root = find_steam_process()

    # 4. Launch and hunt for a stable, new process.
    log.info("Launching game %s", app_id)
    launch_game(app_id)
    game = wait_for_new_game(executable_name, steam_root, baseline)

    if game is None:
        return

    try:
        log.info("Game detected (PID %s) — waiting for exit", game.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        log.info("Game detected — waiting for exit")
    wait_process_exit(game)
    log.info("Game process exited")


def setup_logging():
    """Configure logging to stderr (always visible to Sunshine) plus a best-effort
    file log parked next to this script.

    The file log uses an ABSOLUTE path in the script's own directory rather than
    the working directory, because Sunshine launches this process with cwd=/usr/bin,
    which is not writable by the user.  Logging must never crash the watcher, so a
    file-open failure is swallowed."""
    log.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)
    log.addHandler(stderr_handler)

    try:
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            'steam-game-watcher.log',
        )
        file_handler = logging.FileHandler(log_path, mode='a')
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
    except Exception as e:
        log.warning("Could not open log file %s: %s", log_path, e)


def parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=sys.argv[0],
        description="Launch a Steam game and wait for it to exit.",
    )
    parser.add_argument('app_id', help="Steam app ID of the game to launch")
    parser.add_argument(
        'executable_name', nargs='?', default=None,
        help="Optional exact process name to track (e.g. game.exe).",
    )
    args, unknown = parser.parse_known_args(argv)

    # Sunshine's cmd is not guaranteed to quote the executable name, so a name
    # containing spaces/apostrophes arrives split across multiple argv tokens.
    # Reassemble any trailing positional tokens back into the executable name.
    parts = []
    if args.executable_name:
        parts.append(args.executable_name)
    parts.extend(t for t in unknown if not t.startswith('-'))
    if parts:
        args.executable_name = ' '.join(parts)
    return args


def main(argv=None):
    setup_logging()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    log.info("=== steam-game-watcher started ===")
    log.info("app_id=%s executable_name=%s", args.app_id, args.executable_name)

    try:
        track_game(args.app_id, args.executable_name)
    except Exception:
        log.exception("Unhandled exception")
        raise
    finally:
        log.info("=== steam-game-watcher exiting ===")


if __name__ == "__main__":
    main()
