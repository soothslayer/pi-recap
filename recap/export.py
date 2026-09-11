"""Export summarized episodes as individual Markdown files.

Every saved episode gets its own note under <sync_dir>/episodes/, alongside
the daily recaps. Point Syncthing at sync_dir (default: recaps/) and the
notes land on your phone/laptop with no cloud account involved. The files
also drop straight into an Obsidian or Logseq vault.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger("pi-recap.export")


def sanitize_filename(text: str, max_len: int = 60) -> str:
    """Lowercase, spaces -> dashes, strip anything not a-z/0-9/dash/underscore."""
    slug = re.sub(r"\s+", "-", (text or "").strip().lower())
    slug = re.sub(r"[^a-z0-9_-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-_")
    return slug[:max_len] or "untitled"


def export_episode(ep: dict, sync_dir: str | Path) -> Path:
    """Write one episode as Markdown. Returns the file path.

    ep should have: title, started_at, ended_at, summary, key_points,
    action_items, transcript, pending_summary (optional).
    """
    sync_dir = Path(sync_dir)
    episodes_dir = sync_dir / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)

    # Timestamp prefix keeps files sortable even with duplicate titles.
    try:
        ts = datetime.fromisoformat(ep.get("started_at", "")).strftime("%Y-%m-%d-%H%M%S")
    except (ValueError, TypeError):
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")

    path = episodes_dir / f"{ts}-{sanitize_filename(ep.get('title', ''))}.md"
    # Avoid clobbering on re-export (e.g. re-run of a backfill).
    counter = 1
    while path.exists():
        path = episodes_dir / f"{ts}-{sanitize_filename(ep.get('title', ''))}-{counter}.md"
        counter += 1

    lines = [
        f"# {ep.get('title', 'Untitled conversation')}",
        "",
        f"- **Started:** {ep.get('started_at', '?')}",
        f"- **Ended:** {ep.get('ended_at', '?')}",
    ]
    if ep.get("pending_summary"):
        lines.append("- **Status:** summary pending (private mode, local LLM unavailable)")
    lines += ["", "## Summary", "", ep.get("summary", "") or "_(none)_", ""]

    key_points = ep.get("key_points") or []
    lines += ["## Key points", ""]
    lines += [f"- {kp}" for kp in key_points] or ["_(none)_"]
    lines.append("")

    actions = ep.get("action_items") or []
    lines += ["## Action items", ""]
    lines += [f"- [ ] {ai}" for ai in actions] or ["_(none)_"]
    lines.append("")

    lines += ["## Transcript", "", ep.get("transcript", "") or "_(none)_", ""]

    path.write_text("\n".join(lines))
    log.info("Episode exported to %s", path)
    return path
