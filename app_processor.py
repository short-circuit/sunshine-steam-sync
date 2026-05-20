import itertools
import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Set, Tuple

from grid_downloader import fetch_grid_from_steamgriddb
from steam_api import find_game_executable


class Spinner:
    def __init__(self):
        self._chars = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
        self._message = ""
        self._running = False
        self._thread = None

    def start(self, message=""):
        self._message = message
        self._running = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def update(self, message=""):
        self._message = message

    def _spin(self):
        while self._running:
            ts = datetime.now().strftime('%H:%M:%S')
            icon = next(self._chars)
            sys.stdout.write(f"\r\033[90m[{ts}]\033[0m {icon} {self._message}\033[K")
            sys.stdout.flush()
            time.sleep(0.1)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join()
        sys.stdout.write('\r\033[K')
        sys.stdout.flush()


def is_steam_cmd(cmd: str) -> bool:
    """Return True if cmd is a Steam game entry (direct URL or watcher)."""
    return 'steam://rungameid/' in cmd or 'steam_game_watcher.py' in cmd


def extract_app_id(cmd: str) -> str | None:
    """Extract the Steam app_id from a cmd string.

    Handles both formats:
      steam steam://rungameid/230410
      python3 steam_game_watcher.py 230410 [executable]
    """
    if 'steam_game_watcher.py' in cmd:
        parts = cmd.split('steam_game_watcher.py')
        if len(parts) > 1:
            tail = parts[1].strip().split()
            return tail[0] if tail else None
        return None
    if 'steam://rungameid/' in cmd:
        return cmd.split('rungameid/')[-1].strip()
    return None


def cleanup_steam_apps(sunshine_config: dict, grids_folder: str, dry_run: bool = False) -> Tuple[List[dict], List[Tuple[str, str]]]:
    """Remove all Steam-based entries from Sunshine config.

    Returns (kept_apps, removed_names_and_ids).
    """
    kept = []
    removed = []

    for app in sunshine_config.get('apps', []):
        cmd = app.get('cmd', '')
        if cmd and is_steam_cmd(cmd):
            name = app.get('name', 'Unknown')
            app_id = extract_app_id(cmd) or '?'
            removed.append((name, app_id))
            if not dry_run:
                grid_path = app.get('image-path')
                if grid_path and os.path.exists(grid_path):
                    try:
                        os.remove(grid_path)
                        logging.debug(f"Removed grid image: {grid_path}")
                    except Exception as e:
                        logging.warning(f"Failed to remove grid image {grid_path}: {e}")
        else:
            kept.append(app)

    return kept, removed


def process_existing_apps(
    sunshine_config: dict,
    installed_games: dict
) -> Tuple[List[dict], List[Tuple[str, str]], Set[str], Set[str]]:
    """Process existing Sunshine apps and identify changes."""
    updated_apps = []
    removed_games = []
    existing_steam_apps: Set[str] = set()
    games_need_grid_redownload: Set[str] = set()

    for app in sunshine_config.get('apps', []):
        cmd = app.get('cmd', '')
        if cmd and is_steam_cmd(cmd):
            app_id = extract_app_id(cmd) or ''
            if app_id in installed_games:
                if app_id in existing_steam_apps:
                    logging.info(f"Removing duplicate: {app.get('name', app_id)}")
                    grid_path = app.get('image-path')
                    if grid_path and os.path.exists(grid_path):
                        try:
                            os.remove(grid_path)
                        except Exception:
                            pass
                    continue
                grid_path = app.get('image-path')
                if not grid_path or not os.path.exists(grid_path):
                    games_need_grid_redownload.add(app_id)
                updated_apps.append(app)
                existing_steam_apps.add(app_id)
            else:
                removed_games.append((app.get('name', 'Unknown'), app_id))
                grid_path = app.get('image-path')
                if grid_path and os.path.exists(grid_path):
                    try:
                        os.remove(grid_path)
                        logging.debug(f"Removed grid image: {grid_path}")
                    except Exception as e:
                        logging.warning(f"Failed to remove grid image {grid_path}: {e}")
        else:
            updated_apps.append(app)

    return updated_apps, removed_games, existing_steam_apps, games_need_grid_redownload


def _build_steam_cmd(app_id: str, use_watcher: bool, library_vdf_path: str = "") -> str:
    """Build the cmd for a Steam game entry (always uses Big Picture mode)."""
    if use_watcher:
        watcher = os.path.join(os.path.dirname(__file__), "steam_game_watcher.py")
        exe = find_game_executable(app_id, library_vdf_path) if library_vdf_path else None
        if exe:
            return f"python3 {watcher} {app_id} {exe}"
        return f"python3 {watcher} {app_id}"

    if os.name == 'nt':
        return f"steam://rungameid/{app_id}"

    flatpak_steam = subprocess.run(
        ['flatpak', 'list', '--app', '--columns=application'],
        capture_output=True, text=True
    ).stdout

    if 'com.valvesoftware.Steam' in flatpak_steam:
        return f"flatpak run com.valvesoftware.Steam -bigpicture steam://rungameid/{app_id}"

    return f"steam -bigpicture steam://rungameid/{app_id}"


def add_new_games(
    new_games: Set[str],
    installed_games: dict,
    api_key: str,
    grids_folder: str,
    use_watcher: bool = False,
    library_vdf_path: str = "",
) -> List[dict]:
    """Add new games with grid images using concurrent downloads."""
    new_apps = []
    if not new_games:
        return new_apps

    total = len(new_games)
    spinner = Spinner()
    spinner.start(f"Adding games... (0/{total})")

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_app_id = {}

        for app_id in new_games:
            game_name = installed_games.get(app_id)
            future = executor.submit(fetch_grid_from_steamgriddb, app_id, api_key, grids_folder, game_name)
            future_to_app_id[future] = app_id

        processed = 0
        for future in as_completed(future_to_app_id):
            app_id = future_to_app_id[future]
            processed += 1

            try:
                grid_path = future.result()
                game_name = installed_games[app_id]
                cmd = _build_steam_cmd(app_id, use_watcher, library_vdf_path)

                new_app = {
                    "name": game_name,
                    "cmd": cmd,
                    "output": "",
                    "detached": "",
                    "elevated": "false",
                    "hidden": "true",
                    "wait-all": "true",
                    "exit-timeout": "5",
                    "image-path": grid_path or ""
                }
                new_apps.append(new_app)

                logging.debug(f"Added: {game_name}")
                spinner.update(f"Adding games... {game_name:<40} ({processed}/{total})")

            except Exception as e:
                name = installed_games.get(app_id, app_id)
                logging.debug(f"Failed to add {name}: {e}")
                spinner.update(f"Adding games... failed: {name:<35} ({processed}/{total})")

    spinner.stop()
    return new_apps