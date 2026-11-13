"""Create a consistent local backup. Run while the application is stopped."""
import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from meeting_app.db import AUDIO_DIR, DB_PATH, DATA_DIR
from meeting_app import cloud

def skip_links(folder, names):
    return [name for name in names if (Path(folder) / name).is_symlink() or getattr((Path(folder) / name).lstat(), "st_file_attributes", 0) & 0x400]



def main():
    parser = argparse.ArgumentParser(description="Резервная копия встреч и аудио")
    parser.add_argument("destination", type=Path, help="Каталог, в котором создать копию")
    args = parser.parse_args()
    if cloud.enabled():
        parser.error("Cloud mode: use PostgreSQL and private storage backups from the provider")
    if not DB_PATH.is_file():
        parser.error("База ещё не создана")
    target = args.destination.resolve() / ("meetings-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    target.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(target / DB_PATH.name) as dest:
        source.backup(dest)
    shutil.copytree(AUDIO_DIR, target / "audio", dirs_exist_ok=True, ignore=skip_links)
    for name in ("video", "incoming", "staging"):
        if (DATA_DIR / name).is_dir():
            shutil.copytree(DATA_DIR / name, target / name, ignore=skip_links)
    if (DATA_DIR / "openai.key.dpapi").exists():
        shutil.copy2(DATA_DIR / "openai.key.dpapi", target / "openai.key.dpapi")
    print(target)


if __name__ == "__main__":
    main()
