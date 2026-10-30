import glob
import json
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
                        'icon': app.get('icon', ''),
                        'startdir': app.get('StartDir', ''),
                        'launch_options': app.get('LaunchOptions', ''),
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


def is_game_installed(app_id: str, library_vdf_path: str) -> bool:
    """Return True only if *app_id* is genuinely installed on disk.

    Steam's ``libraryfolders.vdf`` can still list an app that isn't actually
    installed (e.g. a stale record left behind after its files were removed,
    which would otherwise create a phantom entry).  This checks that an app
    manifest exists for the app AND that its install directory is physically
    present, so uninstalled/stale records are excluded.
    """
    if app_id.startswith('-'):
        # Non-Steam shortcuts are valid regardless of Steam install state.
        return True
    for lib in get_library_folders(library_vdf_path):
        manifest_path = os.path.join(lib, 'steamapps', f'appmanifest_{app_id}.acf')
        if not os.path.exists(manifest_path):
            continue
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = vdf.load(f)
        except Exception:
            continue
        installdir = manifest.get('AppState', {}).get('installdir')
        if not installdir:
            continue
        common_dir = os.path.join(lib, 'steamapps', 'common', installdir)
        if os.path.isdir(common_dir):
            return True
    return False


_NON_EXEC_EXTS = {
    'txt', 'json', 'md', 'log', 'ini', 'cfg', 'toml', 'yaml', 'yml',
    'xml', 'csv', 'html', 'htm', 'vdf', 'acf', 'pyc', 'db', 'ico',
    # media / data / shaders / models that may still carry the exec bit
    'mp4', 'avi', 'mkv', 'wmv', 'webm', 'mpg', 'm4v', 'bik', 'bnk',
    'wav', 'ogg', 'mp3', 'flac', 'pak', 'uexp', 'uasset', 'dds',
    'tga', 'bmp', 'fbx', 'obj', 'mtl', 'spv', 'fxo', 'glb', 'gltf',
    'ttf', 'otf', 'rtf', 'as', 'cache', 'index', 'js', 'css', 'hic',
    'jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'fnt', 'mesh', 'anim',
    'gfx', 'cur', 'bin', 'pdb', 'dat', 'sav',
}
# Directory-name fragments that strongly suggest the game binary lives here.
_BINARY_DIR_HINTS = ('bin', 'binaries', 'win64', 'x64', 'linux64', 'linux')
# Well-known helper/launcher processes that are usually NOT the game itself.
_HELPER_FRAGMENTS = (
    'launcher', 'unityplayer', 'unitycrashhandler', 'crashreport',
    'crashpad', 'setup', 'unins', 'vc_redist', 'vcredist', 'redist',
    'steamclient', 'steam_api', 'bootstrap', 'dedicated',
)
# Library files are rarely the entry-point process; .exe is the typical
# Windows/Proton main binary and must strongly outweigh them so a real game
# binary is never beaten by a bundled .dll or asset file.
_LIBRARY_EXTS = {'dll', 'so', 'lib', 'a', 'dylib'}


def scan_game_exe(search_dir: str, target: str) -> Optional[str]:
    """Recursively scan *search_dir* (following directory symlinks, loop-safe)
    for the most plausible game executable and return its base filename.

    *target* is a suggestion string (e.g. the install dir or game name) that
    steers filename-similarity scoring; pass '' to skip that weighting.
    Returns None if nothing usable is found.
    """
    if not search_dir or not os.path.isdir(search_dir):
        return None
    norm_target = (target or '').lower().replace(' ', '').replace('-', '').replace('_', '')
    scored = []

    seen_dirs = {os.path.realpath(search_dir)}
    stack = [(search_dir, 0)]
    while stack:
        root, depth = stack.pop()
        try:
            entries = os.scandir(root)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=True):
                    real_dir = os.path.realpath(entry.path)
                    if real_dir in seen_dirs:
                        continue
                    seen_dirs.add(real_dir)
                    stack.append((entry.path, depth + 1))
                elif entry.is_file():
                    depth_ = depth if root == search_dir else depth + 1
                    fname = entry.name
                    path = entry.path
                    name, ext = os.path.splitext(fname)
                    ext = ext.lstrip('.').lower()
                    if ext in _NON_EXEC_EXTS:
                        continue
                    # Require an executable bit, or an .exe (Proton/Wine on Linux).
                    if ext != 'exe' and not os.access(path, os.X_OK):
                        continue
                    stem = name.lower().replace(' ', '').replace('-', '').replace('_', '')
                    score = 4 * len(os.path.commonprefix([norm_target, stem]))
                    if ext == 'exe':
                        score += 6
                    elif ext in _LIBRARY_EXTS:
                        score -= 4
                    if any(hint in root.lower() for hint in _BINARY_DIR_HINTS):
                        score += 2
                    if any(frag in stem for frag in _HELPER_FRAGMENTS):
                        score -= 8
                    scored.append((score, depth_, len(fname), fname))
            except OSError:
                continue

    if not scored:
        return None
    scored.sort(key=lambda x: (-x[0], x[1], x[2]))
    return scored[0][3]


def _heroic_install_path(app_name: str, runner: str) -> Optional[str]:
    """Return the on-disk install directory Heroic uses for *app_name*.

    Reads Heroic's per-runner ``installed`` info (GOG store, Legendary, or
    Nile) and returns the ``install_path`` for the matching app, or None.
    """
    heroic_config = os.path.expanduser('~/.config/heroic')
    if not os.path.isdir(heroic_config):
        return None

    if runner == 'gog':
        candidates = [os.path.join(heroic_config, 'gog_store', 'installed.json')]
    elif runner == 'legendary':
        candidates = [os.path.join(heroic_config, 'store_cache', 'legendary_install_info.json')]
    elif runner == 'nile':
        candidates = [os.path.join(heroic_config, 'store_cache', 'nile_install_info.json')]
    else:
        candidates = []

    # Also fall back to a broad grep across all Heroic config for safety.
    candidates += [
        os.path.join(heroic_config, 'gog_store', 'installed.json'),
        os.path.join(heroic_config, 'store_cache', 'legendary_install_info.json'),
        os.path.join(heroic_config, 'store_cache', 'nile_install_info.json'),
    ]

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            continue
        installed = data.get('installed', []) if isinstance(data, dict) else []
        for item in installed:
            if not isinstance(item, dict):
                continue
            if str(item.get('appName', '')) == str(app_name):
                install_path = item.get('install_path')
                if install_path:
                    return install_path
    return None


def find_shortcut_executable(app_id: str) -> Optional[str]:
    """Return the most likely game executable for a non-Steam shortcut.

    Shortcut app IDs are negative.  For Heroic-managed shortcuts the exact
    game binary is not the shortcut's ``exe`` (which points at the Heroic
    launcher), so we resolve the app's install directory from Heroic's config
    and scan it for the real binary.  For other shortcuts we fall back to
    scanning the shortcut's working directory, then its ``exe`` basename.
    """
    shortcuts = load_shortcuts()
    info = shortcuts.get(app_id)
    if not info:
        return None

    # Heroic shortcuts encode the game identity in LaunchOptions.
    hero = info.get('launch_options', '')
    app_name = None
    runner = None
    if 'heroic://launch' in hero:
        try:
            query = hero.split('heroic://launch', 1)[1].split('?', 1)[1]
            params = dict(p.split('=', 1) for p in query.split('&') if '=' in p)
            app_name = params.get('appName')
            runner = params.get('runner')
        except Exception:
            pass

    # 1) Prefer scanning the actual game install directory (Heroic).
    if app_name:
        install_path = _heroic_install_path(app_name, runner)
        if install_path:
            exe = scan_game_exe(install_path, info.get('name', ''))
            if exe:
                logging.debug(f"Found executable for shortcut {app_id} "
                              f"(Heroic install): {exe}")
                return exe

    # 2) Fall back to scanning the shortcut's working/start directory.
    start_dir = info.get('startdir', '').strip('"')
    if start_dir:
        exe = scan_game_exe(start_dir, info.get('name', ''))
        if exe:
            logging.debug(f"Found executable for shortcut {app_id}: {exe}")
            return exe

    # 3) Last resort: use the shortcut's configured executable basename.
    configured_exe = info.get('exe', '').strip('"')
    if configured_exe:
        base = os.path.basename(configured_exe)
        # Heroic's launcher is never the game process itself, so skip it.
        lowered = base.lower()
        if lowered not in ('heroic', 'heroic.exe'):
            logging.debug(f"Using configured exe for shortcut {app_id}: {base}")
            return base
    return None


@lru_cache(maxsize=256)
def find_game_executable(app_id: str, library_vdf_path: str) -> Optional[str]:
    """Return the executable filename for a Steam game, or None if not found.

    For non-Steam shortcuts (negative app IDs) delegates to
    :func:`find_shortcut_executable`.  For regular Steam games it reads the
    app manifest from each library folder to determine the install directory,
    then walks it looking for the most plausible game executable (games often
    keep their main binary in a subdirectory).  Returns only the base
    filename, which the process watcher uses for process-name matching.
    """
    if app_id.startswith('-'):
        return find_shortcut_executable(app_id)

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
        best = scan_game_exe(common_dir, installdir)
        if best:
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
                    # Skip apps Steam records but that aren't actually on disk
                    # (e.g. stale leftovers) — they'd otherwise show up as
                    # phantom entries and cause duplicates.
                    if not is_game_installed(app_id, library_vdf_path):
                        logging.debug(f"Skipping {app_id}: not actually installed")
                        continue
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