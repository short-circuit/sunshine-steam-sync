import os
import subprocess
import time
import logging
import psutil


def restart_steam(steam_exe_path: str) -> None:
    """Restart Steam application safely."""
    if os.name != 'nt':
        logging.warning("Steam restarting is only supported on Windows. Please restart Steam manually if any game is missing.")
        return

    if not steam_exe_path or not os.path.exists(steam_exe_path):
        logging.warning("Steam executable path not configured or doesn't exist. Skipping Steam restart.")
        return

    logging.info("Restarting Steam...")
    try:
        terminated = False
        for proc in psutil.process_iter(['name', 'pid']):
            if proc.info['name'] and proc.info['name'].lower() == 'steam.exe':
                logging.debug(f"Terminating Steam process (PID: {proc.info['pid']})")
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                    terminated = True
                except psutil.TimeoutExpired:
                    logging.warning(f"Steam process (PID: {proc.info['pid']}) didn't terminate gracefully")
                    proc.kill()

        if terminated:
            time.sleep(3)

        logging.info(f"Starting Steam from: {steam_exe_path}")
        subprocess.Popen([steam_exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(10)
        logging.info("Steam restart completed")

    except Exception as e:
        logging.error(f"Error restarting Steam: {e}")


def _find_and_kill(name_match: str) -> bool:
    """Find and terminate all processes matching name. Returns True if any were killed."""
    terminated = False
    for proc in psutil.process_iter(['name', 'pid']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == name_match.lower():
                logging.debug(f"Terminating {name_match} process (PID: {proc.info['pid']})")
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                    terminated = True
                except psutil.TimeoutExpired:
                    logging.warning(f"{name_match} (PID: {proc.info['pid']}) didn't terminate gracefully")
                    proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return terminated


def restart_sunshine(sunshine_exe_path: str) -> None:
    """Restart Sunshine safely. Tries binary, then user systemd on Linux."""
    logging.info("Restarting Sunshine...")

    try:
        if sunshine_exe_path and os.path.exists(sunshine_exe_path):
            proc_name = os.path.basename(sunshine_exe_path)
            terminated = _find_and_kill(proc_name)
            if terminated:
                time.sleep(3)
            logging.info(f"Starting Sunshine from: {sunshine_exe_path}")
            subprocess.Popen([sunshine_exe_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logging.info("Sunshine restart completed")
            return

        if os.name != 'nt':
            for unit in ('sunshine.service', 'sunshine'):
                try:
                    result = subprocess.run(
                        ['systemctl', '--user', 'restart', unit],
                        capture_output=True, text=True, timeout=30
                    )
                    if result.returncode == 0:
                        logging.info(f"Sunshine restart completed via systemctl --user {unit}")
                        return
                except FileNotFoundError:
                    break
                except subprocess.TimeoutExpired:
                    logging.warning(f"systemctl restart of {unit} timed out")
                    continue

            _find_and_kill('sunshine')
            logging.info("Sunshine processes terminated (restart manually or via systemd)")

        logging.warning("Could not restart Sunshine. Please restart it manually.")

    except Exception as e:
        logging.error(f"Error restarting Sunshine: {e}")