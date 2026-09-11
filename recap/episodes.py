"""Group transcribed speech segments into conversation episodes.

An episode is one conversation: segments separated by silence shorter than
`gap_minutes` belong together; a longer gap starts a new episode.
Completed episodes are handed to `on_episode` for summarization/storage.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

log = logging.getLogger("pi-recap.episodes")


@dataclass
class Episode:
    id: str
    started_at: float  # epoch seconds
    ended_at: float
    segments: list[dict] = field(default_factory=list)

    @property
    def transcript(self) -> str:
        return "\n".join(s["text"] for s in self.segments)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
            "ended_at": datetime.fromtimestamp(self.ended_at).isoformat(timespec="seconds"),
            "segments": self.segments,
            "transcript": self.transcript,
        }


class EpisodeBuilder:
    def __init__(self, gap_minutes: float = 10, on_episode: Callable[[Episode], None] | None = None):
        self.gap_seconds = gap_minutes * 60
        self.on_episode = on_episode
        self._current: Episode | None = None
        self._last_end: float | None = None

    def add_segment(self, text: str, start_ts: float, end_ts: float) -> None:
        """Add a transcribed segment; may finalize the current episode."""
        text = (text or "").strip()
        if not text:
            return
        if (
            self._current is not None
            and self._last_end is not None
            and (start_ts - self._last_end) > self.gap_seconds
        ):
            self._finalize()

        if self._current is None:
            self._current = Episode(
                id=uuid.uuid4().hex[:12], started_at=start_ts, ended_at=end_ts
            )
            log.info("New episode %s started", self._current.id)
        self._current.segments.append({"text": text, "start": start_ts, "end": end_ts})
        self._current.ended_at = end_ts
        self._last_end = end_ts

    def flush(self) -> None:
        """Finalize any in-progress episode (call on shutdown)."""
        self._finalize()

    def _finalize(self) -> None:
        ep = self._current
        self._current = None
        self._last_end = None
        if ep is None or not ep.segments:
            return
        log.info(
            "Episode %s ended: %d segments, %.1f min",
            ep.id,
            len(ep.segments),
            (ep.ended_at - ep.started_at) / 60,
        )
        if self.on_episode:
            try:
                self.on_episode(ep)
            except Exception:
                log.exception("on_episode callback failed for %s", ep.id)
