from pathlib import Path
import os
import sys


def get_settings_file() -> Path:
    if sys.platform == "win32":
        config_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        config_dir = Path.home() / "Library" / "Application Support"
    else:
        config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config").expanduser()
    return config_dir / "OAI-Processor" / "settings.json"
