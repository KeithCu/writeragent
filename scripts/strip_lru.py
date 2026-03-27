import json
import os
import shutil
from pathlib import Path

# Paths to try (based on common LibreOffice locations and find result)
PATHS = [
    Path("~/.config/libreoffice/4/user/config/writeragent.json").expanduser(),
    Path("~/.config/libreoffice/4/user/writeragent.json").expanduser(),
    Path("~/.config/libreoffice/24/user/config/writeragent.json").expanduser(),
    Path("~/.config/libreoffice/24/user/writeragent.json").expanduser(),
]

def clear_lru():
    config_file = None
    for p in PATHS:
        if p.exists():
            config_file = p
            break
            
    if not config_file:
        print("Could not find writeragent.json in expected locations.")
        return

    print(f"Found config at: {config_file}")
    
    # Backup
    backup_file = config_file.with_suffix(".json.bak")
    print(f"Creating backup at: {backup_file}")
    shutil.copy2(config_file, backup_file)
    
    # Load
    with open(config_file, "r", encoding="utf-8") as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON: {e}")
            return

    if not isinstance(config, dict):
        print("Config is not a JSON object.")
        return

    # Filter out LRU keys
    original_keys = list(config.keys())
    lru_keys = [k for k in original_keys if "_lru" in k]
    
    if not lru_keys:
        print("No LRU keys found in config.")
        return

    print(f"Removing {len(lru_keys)} LRU keys:")
    for k in lru_keys:
        print(f"  - {k}")
        del config[k]

    # Save
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
        
    print("Successfully stripped LRU lists.")

if __name__ == "__main__":
    clear_lru()
