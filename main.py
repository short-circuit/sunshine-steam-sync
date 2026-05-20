import argparse
import logging
import os
import sys

from config import validate_config, get_sunshine_config, save_sunshine_config
from steam_api import load_installed_games
from grid_downloader import fetch_grid_from_steamgriddb
from app_processor import process_existing_apps, add_new_games, cleanup_steam_apps, is_steam_cmd, extract_app_id
from process import restart_steam, restart_sunshine


class ColoredFormatter(logging.Formatter):
    grey = '\033[90m'
    cyan = '\033[36m'
    green = '\033[32m'
    yellow = '\033[33m'
    red = '\033[31m'
    bold_red = '\033[1;31m'
    reset = '\033[0m'

    LEVEL_COLORS = {
        logging.DEBUG: cyan,
        logging.INFO: green,
        logging.WARNING: yellow,
        logging.ERROR: red,
        logging.CRITICAL: bold_red,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, self.reset)
        record.levelname_colored = f"{color}{record.levelname}{self.reset}"
        return super().format(record)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the application."""
    level = logging.DEBUG if verbose else logging.INFO

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(ColoredFormatter(
        fmt=f"{ColoredFormatter.grey}[%(asctime)s]{{reset}} %(levelname_colored)s %(message)s".format(reset=ColoredFormatter.reset),
        datefmt='%H:%M:%S',
    ))

    file_handler = logging.FileHandler('sunshine-steam-sync.log', mode='w')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        fmt='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))

    logging.basicConfig(level=level, handlers=[console_handler, file_handler])


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='sunshine-steam-sync')
    parser.add_argument('--verbose', '-v', action='store_true', help='Enable verbose logging')
    parser.add_argument('--no-restart', action='store_true', help='Skip restarting Steam and Sunshine')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without making changes')
    parser.add_argument('--cleanup', action='store_true', help='Remove all Steam-based entries from Sunshine config')
    parser.add_argument('--wait', action='store_true', help='Wait for game to exit before ending stream (uses process watcher)')
    return parser.parse_args()


def main() -> None:
    """Main application function."""
    args = parse_args()
    setup_logging(args.verbose)
    logging.info("Starting sunshine-steam-sync")

    try:
        config = validate_config()

        if not args.no_restart:
            restart_steam(config['STEAM_EXE'])

        sunshine_config = get_sunshine_config(config['SUNSHINE_APPS_JSON'])

        if args.cleanup:
            remaining = cleanup_steam_apps(
                sunshine_config, config['SUNSHINE_GRIDS'], dry_run=args.dry_run
            )
            if not args.dry_run:
                sunshine_config['apps'] = remaining
                save_sunshine_config(config['SUNSHINE_APPS_JSON'], sunshine_config)
            return

        installed_games = load_installed_games(config['STEAM_LIBRARY_VDF'])

        os.makedirs(config['SUNSHINE_GRIDS'], exist_ok=True)

        updated_apps, removed_games, existing_steam_apps, games_need_grid_redownload = process_existing_apps(
            sunshine_config, installed_games
        )

        new_games = set(installed_games.keys()) - existing_steam_apps

        duplicates_removed = len(updated_apps) < len(sunshine_config.get('apps', []))
        if removed_games:
            logging.info(f"Games to remove: {[name for name, _ in removed_games]}")
        if new_games:
            logging.info(f"New games to add: {[installed_games[app_id] for app_id in new_games]}")
        if games_need_grid_redownload:
            logging.info(f"Re-downloading grids for {len(games_need_grid_redownload)} games with missing images")
        if duplicates_removed:
            logging.info("Removed duplicates from list")

        if not removed_games and not new_games and not games_need_grid_redownload and not duplicates_removed:
            logging.info("No changes needed - all games are up to date")
            return

        if args.dry_run:
            logging.info("Dry run mode - no changes will be made")
            return

        has_api_key = bool(config['STEAMGRIDDB_API_KEY'])
        if has_api_key:
            new_apps = add_new_games(
                new_games, installed_games, config['STEAMGRIDDB_API_KEY'], config['SUNSHINE_GRIDS'],
                use_watcher=args.wait, library_vdf_path=config['STEAM_LIBRARY_VDF'],
            )
            updated_apps.extend(new_apps)

            refreshed_names = []
            if games_need_grid_redownload:
                for app_id in games_need_grid_redownload:
                    game_name = installed_games.get(app_id)
                    grid_path = fetch_grid_from_steamgriddb(
                        app_id, config['STEAMGRIDDB_API_KEY'], config['SUNSHINE_GRIDS'], game_name
                    )
                    if grid_path:
                        for app in updated_apps:
                            cmd = app.get('cmd', '')
                            if is_steam_cmd(cmd) and extract_app_id(cmd) == app_id:
                                app['image-path'] = grid_path
                                refreshed_names.append(app.get('name', app_id))
                                break
        else:
            new_apps = add_new_games(
                new_games, installed_games, '', config['SUNSHINE_GRIDS'],
                use_watcher=args.wait, library_vdf_path=config['STEAM_LIBRARY_VDF'],
            )
            updated_apps.extend(new_apps)
            refreshed_names = []

        sunshine_config['apps'] = updated_apps
        save_sunshine_config(config['SUNSHINE_APPS_JSON'], sunshine_config)

        if not args.no_restart:
            restart_sunshine(config['SUNSHINE_EXE'])

        added_names = [a['name'] for a in new_apps]
        removed_names = [n for n, _ in removed_games]

        logging.info("")
        logging.info("╔══════════════════════════════════════╗")
        logging.info("║            Sync Complete             ║")
        logging.info("╠══════════════════════════════════════╣")
        if added_names:
            for name in added_names:
                logging.info(f"║  + {name:<34}║")
        if removed_names:
            for name in removed_names:
                logging.info(f"║  - {name:<34}║")
        if refreshed_names:
            for name in refreshed_names:
                logging.info(f"║  ~ {name:<34}║")
        if not added_names and not removed_names and not refreshed_names:
            logging.info(f"║{'No changes':^38}║")
        logging.info("╚══════════════════════════════════════╝")

    except KeyboardInterrupt:
        logging.info("Process interrupted by user")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()