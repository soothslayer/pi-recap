"""Generate a Markdown daily recap from a day's episodes.

Usage:
    python -m recap.daily            # yesterday (local time)
    python -m recap.daily 2026-09-10

Saves to the daily_recaps table and writes <sync_dir>/YYYY-MM-DD.md
(where sync_dir defaults to recaps/ and is the Syncthing-shared folder).
Designed to run from systemd's daily timer at 06:00.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
log = logging.getLogger("pi-recap.daily")


def parse_date_arg(argv: list[str]) -> str:
    if len(argv) > 1:
        try:
            datetime.strptime(argv[1], "%Y-%m-%d")
        except ValueError:
            print(f"Invalid date {argv[1]!r}; expected YYYY-MM-DD", file=sys.stderr)
            sys.exit(2)
        return argv[1]
    return (date.today() - timedelta(days=1)).isoformat()


def main(argv: list[str] | None = None) -> int:
    from .config import load_config
    from . import store
    from .summarize import Summarizer

    date_str = parse_date_arg(argv or sys.argv)
    cfg = load_config()
    store.init_db(cfg.recap.data_dir)

    episodes = store.list_episodes_for_date(date_str)
    log.info("Found %d episodes for %s", len(episodes), date_str)

    summarizer = Summarizer(
        cfg.llm,
        private_mode_check=lambda: store.kv_get("private_mode", "0") == "1",
    )
    markdown = summarizer.summarize_daily(date_str, episodes)

    store.save_daily(date_str, markdown)

    recaps_dir = Path(cfg.recap.sync_dir)  # already absolute via load_config
    recaps_dir.mkdir(parents=True, exist_ok=True)
    out_path = recaps_dir / f"{date_str}.md"
    out_path.write_text(markdown + "\n")
    log.info("Daily recap written to %s", out_path)
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
