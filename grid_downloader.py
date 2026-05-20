import os
import logging
import time
from typing import Optional

import requests
from PIL import Image
import io


def fetch_grid_from_steamgriddb(
    app_id: str,
    api_key: str,
    grids_folder: str,
    game_name: Optional[str] = None
) -> Optional[str]:
    """Download a grid image from SteamGridDB for the given *app_id*
    and save it as ``{grids_folder}/{app_id}.png``.

    For non-Steam games (negative app_id) the function first searches
    by *game_name* to obtain a SteamGridDB game ID, then fetches the
    grid.  Retries API calls up to 3 times with exponential back-off.

    Returns the local path to the saved image, or ``None`` on failure.
    """
    headers = {"Authorization": f"Bearer {api_key}"}

    grid_url = None
    is_non_steam = app_id.startswith('-')

    if is_non_steam and game_name:
        for attempt in range(3):
            try:
                search_url = f"https://www.steamgriddb.com/api/v2/search/autocomplete/{requests.utils.quote(game_name)}"
                response = requests.get(search_url, headers=headers, timeout=15)
                response.raise_for_status()
                data = response.json()

                if "data" in data and len(data["data"]) > 0:
                    sgdb_game_id = data["data"][0].get("id")
                    if sgdb_game_id:
                        grid_url = f"https://www.steamgriddb.com/api/v2/grids/game/{sgdb_game_id}"
                        break
                    logging.warning(f"No game ID found for: {game_name}")
                else:
                    logging.warning(f"No search results for: {game_name}")
                    return None

            except requests.exceptions.Timeout:
                logging.warning(f"Timeout searching for {game_name} (attempt {attempt + 1}/3)")
            except requests.exceptions.RequestException as e:
                logging.warning(f"Request error searching for {game_name} (attempt {attempt + 1}/3): {e}")
            except Exception as e:
                logging.error(f"Unexpected error searching for {game_name}: {e}")
                return None

            if attempt < 2:
                time.sleep(2 ** attempt)
    else:
        grid_url = f"https://www.steamgriddb.com/api/v2/grids/steam/{app_id}"

    if not grid_url:
        logging.warning(f"No grid URL for AppID {app_id}")
        return None

    for attempt in range(3):
        try:
            response = requests.get(grid_url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()

            if "data" in data and len(data["data"]) > 0:
                img_url = data["data"][0]["url"]
                grid_response = requests.get(img_url, timeout=30)
                grid_response.raise_for_status()

                try:
                    image = Image.open(io.BytesIO(grid_response.content))
                    image.verify()

                    image = Image.open(io.BytesIO(grid_response.content))
                    grid_path = os.path.join(grids_folder, f"{app_id}.png")

                    os.makedirs(grids_folder, exist_ok=True)

                    image.save(grid_path, "PNG")
                    logging.debug(f"Downloaded grid for AppID {app_id}: {grid_path}")
                    return grid_path

                except Exception as img_error:
                    logging.warning(f"Invalid image data for AppID {app_id}: {img_error}")
                    return None
            else:
                logging.warning(f"No grid data found for AppID {app_id}")
                return None

        except requests.exceptions.Timeout:
            logging.warning(f"Timeout fetching grid for AppID {app_id} (attempt {attempt + 1}/3)")
        except requests.exceptions.RequestException as e:
            logging.warning(f"Request error for AppID {app_id} (attempt {attempt + 1}/3): {e}")
        except Exception as e:
            logging.error(f"Unexpected error fetching grid for AppID {app_id}: {e}")
            return None

        if attempt < 2:
            time.sleep(2 ** attempt)

    logging.error(f"Failed to fetch grid for AppID {app_id} after 3 attempts")
    return None