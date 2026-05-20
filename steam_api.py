import glob
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from typing import Dict, Optional

import requests
import vdf


def get_steam_userdata_path() -> Optional[str]:
    """Locate the Steam userdata directory (contains ``shortcuts.vdf``
    and other per-user data).  Returns ``None`` if Steam is not found."""
    if os.name == 'nt':
        steam_root = os.path.join(os.environ.get('PROGRAMFILES', 'C:/Program Files'), 'Steam')
    else:
        steam_root = os.path.expanduser('~/.local/share/Steam')

    if not os.path.exists(steam_root):
        return None

    if os.name == 'nt':
        userdata_path = os.path.join(steam_root, 'steamapps', 'data')
        if os.path.exists(userdata_path):
            dirs = [d for d in os.listdir(userdata_path) if os.path.isdir(os.path.join(userdata_path, d))]
            if dirs:
                return userdata_path
    else:
        userdata_dirs = glob.glob(os.path.join(steam_root, 'userdata', '*'))
        if userdata_dirs:
            return userdata_dirs[0]

    return None


def load_shortcuts() -> Dict[str, Dict]:
    """Parse Steam's binary ``shortcuts.vdf`` and return a dict of
    non-Steam game entries keyed by their (negative) app ID.

    Only entries with a negative app ID (i.e. user-added shortcuts)
    are included; built-in shortcuts are ignored.
    """
    shortcuts = {}
    userdata_path = get_steam_userdata_path()
    if not userdata_path:
        logging.debug("No Steam userdata directory found")
        return shortcuts

    shortcuts_path = os.path.join(userdata_path, 'config', 'shortcuts.vdf')

    if not os.path.exists(shortcuts_path):
        logging.debug("No shortcuts.vdf found")
        return shortcuts

    try:
        with open(shortcuts_path, 'rb') as f:
            data = vdf.binary_loads(f.read())
        if 'shortcuts' in data:
            for idx, app in data['shortcuts'].items():
                app_id = str(app.get('appid', ''))
                if app_id.startswith('-'):
                    shortcuts[app_id] = {
                        'name': app.get('appname', ''),
                        'exe': app.get('exe', ''),
                        'icon': app.get('icon', '')
                    }
                    logging.debug(f"Found shortcut: {app.get('appname')} (ID: {app_id})")
        logging.info(f"Loaded {len(shortcuts)} non-Steam games from shortcuts")
    except Exception as e:
        logging.warning(f"Error loading shortcuts.vdf: {e}")

    return shortcuts


@lru_cache(maxsize=1000)
def get_game_name(app_id: str) -> Optional[str]:
    """Look up a game's display name from the Steam store API.
    Results are cached (LRU, 1000 entries) and the request is retried
    up to 3 times with exponential back-off on failure."""
    url = f"https://store.steampowered.com/api/appdetails?appids={app_id}"

    for attempt in range(3):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            if str(app_id) in data and data[str(app_id)].get('success'):
                game_data = data[str(app_id)].get('data', {})
                name = game_data.get('name')
                if name:
                    logging.debug(f"Retrieved name for AppID {app_id}: {name}")
                    return name

            logging.warning(f"No valid data found for AppID {app_id}")
            return None

        except requests.exceptions.Timeout:
            logging.warning(f"Timeout fetching name for AppID {app_id} (attempt {attempt + 1}/3)")
        except requests.exceptions.RequestException as e:
            logging.warning(f"Request error for AppID {app_id} (attempt {attempt + 1}/3): {e}")
        except Exception as e:
            logging.error(f"Unexpected error fetching name for AppID {app_id}: {e}")
            return None

        if attempt < 2:
            time.sleep(2 ** attempt)

    logging.error(f"Failed to fetch name for AppID {app_id} after 3 attempts")
    return None


def get_library_folders(library_vdf_path: str) -> list[str]:
    """Parse ``libraryfolders.vdf`` and return the list of Steam library
    root paths (each containing ``steamapps/``)."""
    try:
        with open(library_vdf_path, 'r', encoding='utf-8') as f:
            data = vdf.load(f)
    except Exception:
        return []
    folders = []
    for folder_data in data.get('libraryfolders', {}).values():
        path = folder_data.get('path', '')
        if path:
            folders.append(path)
    return folders


@lru_cache(maxsize=256)
def find_game_executable(app_id: str, library_vdf_path: str) -> Optional[str]:
    """Return the executable filename for a Steam game, or None if not found.

    Reads the app manifest from each library folder to get the install
    directory, then scans for executable files inside it.
    """
    libraries = get_library_folders(library_vdf_path)
    for lib in libraries:
        manifest_path = os.path.join(lib, 'steamapps', f'appmanifest_{app_id}.acf')
        if not os.path.exists(manifest_path):
            continue
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = vdf.load(f)
        except Exception:
            continue
        app_state = manifest.get('AppState', {})
        installdir = app_state.get('installdir')
        if not installdir:
            continue
        common_dir = os.path.join(lib, 'steamapps', 'common', installdir)
        if not os.path.isdir(common_dir):
            continue
        # Scan for executables — prefer .exe (Proton/Wine on Linux, native on Windows)
        candidates = []
        for entry in os.listdir(common_dir):
            entry_path = os.path.join(common_dir, entry)
            if os.path.isfile(entry_path) and os.access(entry_path, os.X_OK):
                candidates.append(entry)
            elif entry.lower().endswith('.exe') and os.path.isfile(entry_path):
                candidates.append(entry)
        if not candidates:
            continue
        # Prefer the executable whose stem (no ext) matches installdir most closely
        installdir_lower = installdir.lower().replace(' ', '').replace('-', '').replace('_', '')
        scored = []
        for c in candidates:
            stem = os.path.splitext(c)[0].lower().replace(' ', '').replace('-', '').replace('_', '')
            score = len(os.path.commonprefix([installdir_lower, stem]))
            scored.append((score, len(c), c))
        scored.sort(key=lambda x: (-x[0], x[1]))
        best = scored[0][2]
        logging.debug(f"Found executable for AppID {app_id}: {best}")
        return best
    return None


def load_installed_games(library_vdf_path: str) -> Dict[str, str]:
    """Build a dict of ``{app_id: display_name}`` for every installed
    Steam game (plus non-Steam shortcuts) by parsing the library VDF
    and fetching names from the store API concurrently (10 workers)."""
    logging.info(f"Loading Steam library from {library_vdf_path}")

    installed_games = {}

    try:
        with open(library_vdf_path, 'r', encoding='utf-8') as file:
            steam_data = vdf.load(file)
    except Exception as e:
        logging.error(f"Error loading Steam library VDF: {e}")
        raise

    logging.debug("Raw Steam library data loaded successfully")

    total_apps = 0

    for folder_data in steam_data.get('libraryfolders', {}).values():
        if "apps" in folder_data:
            total_apps += len(folder_data["apps"])

    logging.info(f"Processing {total_apps} Steam apps...")

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_app_id = {}

        for folder_data in steam_data.get('libraryfolders', {}).values():
            if "apps" in folder_data:
                for app_id in folder_data["apps"].keys():
                    future = executor.submit(get_game_name, app_id)
                    future_to_app_id[future] = app_id

        processed = 0
        for future in as_completed(future_to_app_id):
            app_id = future_to_app_id[future]
            processed += 1

            try:
                game_name = future.result()
                if game_name:
                    installed_games[app_id] = game_name
                    logging.debug(f"Found game: {game_name} (ID: {app_id})")
            except Exception as e:
                logging.warning(f"Error processing AppID {app_id}: {e}")

            if processed % 50 == 0 or processed == total_apps:
                logging.debug(f"Processed {processed}/{total_apps} apps...")

    shortcuts = load_shortcuts()
    for app_id, info in shortcuts.items():
        if info.get('name'):
            installed_games[app_id] = info['name']
            logging.debug(f"Found shortcut: {info['name']} (ID: {app_id})")

    logging.info(f"Found {len(installed_games)} installed games")
    return installed_games