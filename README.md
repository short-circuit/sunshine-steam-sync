# sunshine-steam-sync

Synchronise your Steam library (including non-Steam shortcuts) with [Sunshine](https://github.com/LizardByte/Sunshine), automatically downloading grid artwork from [SteamGridDB](https://www.steamgriddb.com).

> **Note**: This tool has been developed and tested on Linux only. Windows support is implemented but not yet tested. Pull requests welcome.

## Features

- Reads every installed Steam game from your Steam library folders
- Discovers non-Steam shortcuts added through Steam
- Fetches game names from the Steam store API (with in-memory caching and retry logic)
- Downloads high-quality grid images from SteamGridDB and places them in Sunshine's grid directory
- Adds missing games to Sunshine's `apps.json` and removes entries for uninstalled games
- Deduplicates existing entries to keep the list clean
- **Game process watcher** — optional mode where Sunshine monitors the game process and ends the stream automatically when the game exits
- **Dry-run mode** to preview all changes without touching any files
- **Cleanup mode** to bulk-remove every Steam-based entry from Sunshine at once
- **Auto-restart** on Linux (binary → `systemctl --user` → process kill) and Windows
- **Colored terminal output** with a summary box showing added/removed/refreshed games
- Works entirely via environment configuration; no hard-coded paths

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — the fast Python package and project manager
- **Steam** installed with at least one game in your library
- **[Sunshine](https://github.com/LizardByte/Sunshine)** installed and configured (streaming host, e.g. for Moonlight)
- A **SteamGridDB API key** — get one from your [SteamGridDB preferences](https://www.steamgriddb.com/profile/preferences) (optional — without it games will be added without grid artwork)

## Installation

```bash
git clone https://github.com/yourusername/sunshine-steam-sync.git
cd sunshine-steam-sync
uv sync
```

That's it. `uv` will create a virtual environment and install all dependencies automatically.

## Configuration

Copy the template to get started:

```bash
cp .env.template .env
```

Then edit `.env` with your SteamGridDB API key — everything else has sensible OS-specific defaults.

| Variable | Default (Linux) | Default (Windows) | Description |
|---|---|---|---|
| `STEAM_LIBRARY_VDF` | `~/.local/share/Steam/steamapps/libraryfolders.vdf` | `%PROGRAMFILES(X86)%\Steam\steamapps\libraryfolders.vdf` | Path to Steam's `libraryfolders.vdf` |
| `SUNSHINE_APPS_JSON` | `~/.config/sunshine/apps.json` | `%APPDATA%\sunshine\config\apps.json` | Path to Sunshine's `apps.json` |
| `SUNSHINE_GRIDS` | `~/.config/sunshine/grids` | `%APPDATA%\sunshine\config\grids` | Directory for grid images |
| `STEAMGRIDDB_API_KEY` | — | — | SteamGridDB API key (optional) |
| `STEAM_EXE` | — | `%PROGRAMFILES(X86)%\Steam\steam.exe` | Steam executable for auto-restart |
| `SUNSHINE_EXE` | — | `%PROGRAMFILES%\Sunshine\sunshine.exe` | Sunshine executable for auto-restart |

On Linux, `STEAM_EXE` and `SUNSHINE_EXE` are not needed — the tool falls back to `systemctl --user restart sunshine`, then tries matching the `sunshine` process name directly.

## Usage

```bash
uv run python main.py
```

Or, inside the virtual environment:

```bash
uv shell
python main.py
```

### Flags

| Flag              | Description                                                                                                                                                                                                                                                             |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--verbose`, `-v` | Enable debug-level logging. Useful for diagnosing issues with Steam library parsing, API calls, or grid downloads.                                                                                                                                                      |
| `--no-restart`    | Skip restarting Steam and Sunshine after updating `apps.json`. Use this if you plan to restart the services yourself, or if you want to batch multiple runs.                                                                                                            |
| `--dry-run`       | Preview every change that would be made — shows games to add, remove, and entries with missing grids — without modifying `apps.json` or deleting any files. Safe to run any time.                                                                                       |
| `--cleanup`       | Remove **all** Steam-based entries from Sunshine's `apps.json` and delete their grid images. Non-Steam apps in Sunshine are left untouched. Combine with `--dry-run` to see what would be removed first.                                                                |
| `--wait`          | Enable the game process watcher. Instead of using a plain `steam://rungameid/` URL, Sunshine will run `steam_game_watcher.py` which launches the game, monitors its process, and only exits when the game closes — signalling Sunshine to end the stream automatically. |

### Output

Console output is color-coded with clean `[HH:MM:SS]` timestamps. At the end of each run a summary box is printed:

```
[19:50:22] INFO ╔══════════════════════════════════════╗
[19:50:22] INFO ║            Sync Complete             ║
[19:50:22] INFO ╠══════════════════════════════════════╣
[19:50:22] INFO ║  + Game One                          ║
[19:50:22] INFO ║  + Game Two                          ║
[19:50:22] INFO ║  - Old Game                          ║
[19:50:22] INFO ║  ~ Grid Refresh                      ║
[19:50:22] INFO ╚══════════════════════════════════════╝
```

A plain log file (`sunshine-steam-sync.log`) is written alongside, reset each run, with full timestamps and no color codes.

### Examples

```bash
# Normal sync — add new games, remove uninstalled, download grids, restart services
uv run python main.py

# Preview what would change without touching anything
uv run python main.py --dry-run

# Sync and enable game-exit detection for automatic stream end
uv run python main.py --wait

# Remove every Steam game from Sunshine config
uv run python main.py --cleanup

# See what --cleanup would remove without actually removing it
uv run python main.py --cleanup --dry-run

# Sync without restarting Steam or Sunshine
uv run python main.py --no-restart

# Verbose logging for troubleshooting
uv run python main.py --verbose
```

## How It Works

1. **Load config** — reads paths from the environment (`.env` or OS defaults)
2. **Restart Steam** (optional) — Steam is restarted so its VDF files are refreshed before parsing
3. **Read Steam library** — parses `libraryfolders.vdf` to enumerate every installed game, fetches display names from the Steam store API (with retry and caching), and reads non-Steam shortcuts from `shortcuts.vdf`
4. **Diff against Sunshine** — compares the current `apps.json` against the installed game list, marking entries to add, remove, or re-download grids for
5. **Download grids** — for each new or missing grid, fetches artwork from SteamGridDB using your API key and saves it as a PNG to the configured grids folder
6. **Write apps.json** — produces the updated Sunshine configuration, keeping all non-Steam apps intact
7. **Restart Sunshine** — the service is restarted (binary direct → `systemctl --user` → process kill fallback on Linux; direct binary on Windows)

### Game Process Watcher

When `--wait` is used, the `cmd` field in `apps.json` points to `steam_game_watcher.py` instead of a `steam://rungameid/` URL. The watcher script:

1. Ensures Steam Big Picture Mode is running
2. Launches the game via `steam://rungameid/<app_id>`
3. Detects the game process (by executable name when available, or by monitoring Steam's child process tree)
4. Blocks until the game process exits
5. Exits itself, which tells Sunshine to end the stream

This means your Moonlight (or other Sunshine client) stream will automatically end when you quit the game — no need to manually stop the stream.

### Path Auto-Detection

If no `.env` file exists, the tool selects standard paths based on your operating system:

- **Linux**: Steam is expected at `~/.local/share/Steam`, Sunshine config at `~/.config/sunshine`
- **Windows**: Steam is expected in `%PROGRAMFILES(X86)%\Steam`, Sunshine config in `%APPDATA%\sunshine\config`

You can always override any path by setting the corresponding variable in `.env`.

## Project Structure

```
sunshine-steam-sync/
├── main.py                 # Entry point and CLI argument parsing
├── config.py               # Environment loading, path normalisation, defaults
├── steam_api.py            # Steam library VDF parser, store API client, shortcuts reader
├── app_processor.py        # Diff engine: compares installed games against apps.json
├── grid_downloader.py      # SteamGridDB API client with retry and image validation
├── steam_game_watcher.py   # Game process watcher for auto stream-end
├── process.py              # Steam/Sunshine process management and restart
├── pyproject.toml          # Project metadata and dependencies
├── .env.template           # Configuration template (committed)
└── README.md               # This file
```
