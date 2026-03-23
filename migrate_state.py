"""One-time migration: copy all JSON state files into the SQLite database.

Run once:
    python migrate_state.py

This does NOT delete the original JSON files — they remain as backups.
After confirming the bot runs correctly with SQLite, you can remove them.
"""

import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent))

from utils.state_db import StateDB, migrate_json_to_db

DATA = Path(__file__).parent / "data"

# Map logical key → JSON file path
JSON_FILES = {
    "fast_reply_state":    str(DATA / "fast_reply_state.json"),
    "reply_performance":   str(DATA / "reply_performance.json"),
    "account_memory":      str(DATA / "account_memory.json"),
    "hourly_performance":  str(DATA / "hourly_performance.json"),
    "seen_mentions":       str(DATA / "seen_mentions.json"),
    "trend_poster_state":  str(DATA / "trend_poster_state.json"),
    "posting_lock":        str(DATA / "posting_lock.json"),
    "daily_log":           str(DATA / "daily_log.json"),
    "style_stats":         str(DATA / "style_stats.json"),
    "style_performance":   str(DATA / "style_performance.json"),
    "united_context":      str(DATA / "united_context.json"),
    "rival_context":       str(DATA / "rival_context.json"),
    "recent_angles":       str(DATA / "recent_angles.json"),
}

if __name__ == "__main__":
    db = StateDB()
    print(f"SQLite DB: {db._path}")
    count = migrate_json_to_db(JSON_FILES, db)
    print(f"Migrated {count}/{len(JSON_FILES)} JSON files into SQLite.")
    print(f"Keys in DB: {db.keys()}")
