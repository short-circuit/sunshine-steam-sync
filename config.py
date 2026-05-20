import glob
import json
import logging
import os
import shutil
import sys
from typing import Dict, Optional

from dotenv import load_dotenv


def normalize_path(path: str) -> str:
    """Normalize path and handle escape sequences properly."""
    if not path:
        return path

    path = path.replace('\\\\', '\\')
    path = os.path.normpath(path)
    path = os.path.expandvars(path)
    path = os.path.expanduser(path)

    return path


def get_steam_userdata_path() -> Optional[str]:
    """Find the Steam userdata directory."""
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


def _get_default_paths() -> Dict[str, str]:
    """Return OS-appropriate default paths when no .env is provided."""
    if os.name == 'nt':
        prog86 = os.environ.get('PROGRAMFILES(X86)', 'C:\\Program Files (x86)')
        steam_vdf = os.path.join(prog86, 'Steam', 'steamapps', 'libraryfolders.vdf')
        appdata = os.environ.get('APPDATA', os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming'))
        sunshine_dir = os.path.join(appdata, 'sunshine', 'config')
        apps_json = os.path.join(sunshine_dir, 'apps.json')
        grids = os.path.join(sunshine_dir, 'grids')
        steam_exe = os.path.join(prog86, 'Steam', 'steam.exe')
        sunshine_exe = os.path.join(
            os.environ.get('PROGRAMFILES', 'C:\\Program Files'), 'Sunshine', 'sunshine.exe'
        )
    else:
        steam_vdf = os.path.expanduser('~/.local/share/Steam/steamapps/libraryfolders.vdf')
        sunshine_dir = os.path.expanduser('~/.config/sunshine')
        apps_json = os.path.join(sunshine_dir, 'apps.json')
        grids = os.path.join(sunshine_dir, 'grids')
        steam_exe = ''
        sunshine_exe = ''

    return {
        'STEAM_LIBRARY_VDF': steam_vdf,
        'SUNSHINE_APPS_JSON': apps_json,
        'SUNSHINE_GRIDS': grids,
        'STEAM_EXE': steam_exe,
        'SUNSHINE_EXE': sunshine_exe,
    }


def validate_config() -> Dict[str, str]:
    """Load and validate configuration from environment variables."""
    load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

    defaults = _get_default_paths()

    path_vars = [
        'STEAM_LIBRARY_VDF',
        'SUNSHINE_APPS_JSON',
        'SUNSHINE_GRIDS',
    ]

    config = {}
    for var in path_vars:
        value = os.getenv(var) or defaults[var]
        config[var] = normalize_path(value)
        logging.debug(f"{var}: {config[var]}")

    config['STEAMGRIDDB_API_KEY'] = os.getenv('STEAMGRIDDB_API_KEY', '')
    if not config['STEAMGRIDDB_API_KEY']:
        logging.warning("No SteamGridDB API key configured — grid downloads will be skipped")

    steam_exe = os.getenv('STEAM_EXE') or defaults.get('STEAM_EXE', '')
    sunshine_exe = os.getenv('SUNSHINE_EXE') or defaults.get('SUNSHINE_EXE', '')

    config['STEAM_EXE'] = normalize_path(steam_exe) if steam_exe else ''
    config['SUNSHINE_EXE'] = normalize_path(sunshine_exe) if sunshine_exe else ''

    if not os.path.exists(config['STEAM_LIBRARY_VDF']):
        logging.error(f"Steam library VDF file not found: {config['STEAM_LIBRARY_VDF']}")
        sys.exit(1)

    apps_dir = os.path.dirname(config['SUNSHINE_APPS_JSON'])
    if apps_dir and not os.path.exists(apps_dir):
        logging.warning(f"Sunshine config directory not found: {apps_dir}")
        logging.info("Sunshine config will be created when first saving")

    return config


def get_sunshine_config(path: str) -> Dict:
    """Load Sunshine configuration with error handling."""
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as file:
                config = json.load(file)

            if not isinstance(config, dict):
                raise ValueError("Config must be a dictionary")

            if 'apps' not in config:
                config['apps'] = []

            if 'env' not in config:
                config['env'] = ""

            logging.info(f"Loaded Sunshine config with {len(config['apps'])} apps")
            return config
        else:
            config = {"env": "", "apps": []}
            logging.info("Sunshine config not found, initializing empty config")
            return config
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON in Sunshine config file: {e}")
        raise
    except Exception as e:
        logging.error(f"Error loading Sunshine config: {e}")
        raise


def save_sunshine_config(path: str, config: Dict) -> None:
    """Save Sunshine configuration with backup and error handling."""
    try:
        if os.path.exists(path):
            backup_path = f"{path}.backup"
            shutil.copy2(path, backup_path)
            logging.debug(f"Created backup: {backup_path}")

        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, 'w', encoding='utf-8') as file:
            json.dump(config, file, indent=4, ensure_ascii=False)

        logging.info(f"Saved Sunshine config with {len(config.get('apps', []))} apps")

    except Exception as e:
        logging.error(f"Error saving Sunshine config: {e}")
        raise