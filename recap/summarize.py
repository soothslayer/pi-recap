"""Turn transcripts into structured notes via a local (or API) LLM.

Backends:
  - ollama: local HTTP API (default, private)
  - openai: any OpenAI-compatible chat-completions endpoint (optional)

Output is STRICT JSON: {title, summary, key_points, action_items}.
No speaker attribution — enforced by the system prompt.
Never raises: retries once, then falls back to an extractive stub so the
pipeline keeps running even if the LLM is down.
"""
from __future__ import annotations

import json
import logging
import re

import requests

log = logging.getLogger("pi-recap.summarize")

SUMMARY_SYSTEM_PROMPT = """You are a private note-taking assistant. You receive a transcript \
of a conversation overheard by the user's own device. Write concise notes.

Rules:
- Output STRICT JSON only, no markdown fences, no commentary. The JSON object \
must have exactly these keys: "title" (string), "summary" (string, 2-4 sentences), \
"key_points" (array of strings), "action_items" (array of strings).
- Do NOT attribute statements to speakers. Never write "Speaker 1 said..." or \
use names for who said what. Summarize what was discussed, not who said it.
- Omit sensitive data: passwords, financial account numbers, government IDs.
- If the transcript is trivial or fragmentary, say so briefly in the summary \
and leave key_points/action_items empty.
"""

DAILY_SYSTEM_PROMPT = """You are a private journaling assistant. You receive the day's \
conversation notes (each with a title, summary, key points, action items, and time range). \
Write a single Markdown daily recap.

Format:
- Start with a level-1 header: the date, e.g. "# Daily Recap — 2026-09-10"
- "## Conversations": bullet list of each episode with its time range and one-line summary
- "## Highlights": the 3-7 most important things from the day
- "## Open action items": deduplicated list across all episodes

Rules:
- Do NOT attribute statements to speakers. Summarize what happened, not who said what.
- Omit sensitive data (passwords, account numbers, government IDs).
- If there were no conversations, say the day was quiet.
"""


def _extract_json(raw: str) -> dict:
    """Pull a JSON object out of possibly messy LLM output."""
    text = raw.strip()
    # Strip markdown fences if the model added them anyway.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        # Fall back to the outermost braces.
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def _fallback_summary(transcript: str, pending: bool = False) -> dict:
    """Extractive stub used when the LLM fails: never crash the pipeline.

    pending=True marks the episode as queued for later summarization —
    used in private mode when the local LLM is unreachable, so we never
    fall back to a cloud backend.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", transcript.strip()) if s.strip()]
    words = transcript.split()
    note = {
        "title": " ".join(words[:8]) + ("..." if len(words) > 8 else "") or "Untitled conversation",
        "summary": " ".join(sentences[:2]) or "(No summary available — LLM unreachable.)",
        "key_points": [],
        "action_items": [],
        "_fallback": True,
    }
    if pending:
        note["_pending"] = True
    return note


class Summarizer:
    def __init__(self, llm_cfg, private_mode_check=None):
        """llm_cfg: LLMConfig. private_mode_check: optional callable returning
        bool, polled live (e.g. from the kv store) so the web UI can toggle
        private mode at runtime. Without it, llm_cfg.private_mode is used."""
        self.llm = llm_cfg
        self._private_mode_check = private_mode_check

    def private_mode(self) -> bool:
        """Live private-mode state: nothing may leave the LAN."""
        if self._private_mode_check is not None:
            try:
                return bool(self._private_mode_check())
            except Exception as exc:
                log.warning("private_mode_check failed (%s); assuming private", exc)
                return True  # fail closed: never leak when the check breaks
        return bool(getattr(self.llm, "private_mode", False))

    # ------------------------------------------------------------ primitives

    def chat(self, system: str, user: str) -> str:
        backend = self.llm.backend.lower()
        if self.private_mode() and backend != "ollama":
            log.warning(
                "Private mode ON: refusing cloud backend %r, forcing local ollama",
                self.llm.backend,
            )
            backend = "ollama"
        if backend == "ollama":
            return self._ollama_chat(system, user)
        if backend == "openai":
            return self._openai_chat(system, user)
        raise ValueError(f"Unknown llm.backend: {self.llm.backend!r} (use 'ollama' or 'openai')")

    def _ollama_chat(self, system: str, user: str) -> str:
        url = self.llm.ollama_host.rstrip("/") + "/api/chat"
        resp = requests.post(
            url,
            json={
                "model": self.llm.ollama_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=self.llm.timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def _openai_chat(self, system: str, user: str) -> str:
        url = self.llm.openai_base_url.rstrip("/") + "/chat/completions"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self.llm.openai_api_key}"},
            json={
                "model": self.llm.openai_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            timeout=self.llm.timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    # ------------------------------------------------------------- features

    def summarize(self, transcript: str) -> dict:
        """Summarize one episode's transcript into a structured note dict."""
        transcript = (transcript or "").strip()
        if not transcript:
            return {"title": "Empty conversation", "summary": "", "key_points": [], "action_items": []}

        user = (
            "Conversation transcript (no speaker labels available):\n\n"
            f"{transcript}\n\n"
            "Return the STRICT JSON note described in the system prompt."
        )
        for attempt in range(2):
            try:
                raw = self.chat(SUMMARY_SYSTEM_PROMPT, user)
                data = _extract_json(raw)
                return {
                    "title": str(data.get("title", "Untitled conversation"))[:200],
                    "summary": str(data.get("summary", "")),
                    "key_points": [str(x) for x in data.get("key_points", []) or []],
                    "action_items": [str(x) for x in data.get("action_items", []) or []],
                }
            except Exception as exc:
                log.warning("Summarize attempt %d failed: %s", attempt + 1, exc)
        log.error("LLM summarization failed twice; using extractive fallback.")
        # Private mode: the only permitted backend is local Ollama, and it is
        # down — queue the episode unsummarized instead of touching the cloud.
        pending = self.private_mode()
        return _fallback_summary(transcript, pending=pending)

    def summarize_daily(self, date_str: str, episodes: list[dict]) -> str:
        """Merge a day's episode notes into one Markdown daily recap."""
        if not episodes:
            return f"# Daily Recap — {date_str}\n\nQuiet day — no conversations captured.\n"
        lines = []
        for ep in episodes:
            lines.append(
                f"- [{ep.get('started_at', '?')} → {ep.get('ended_at', '?')}] "
                f"**{ep.get('title', 'Untitled')}**: {ep.get('summary', '')}"
            )
            for kp in ep.get("key_points", []) or []:
                lines.append(f"  - key point: {kp}")
            for ai in ep.get("action_items", []) or []:
                lines.append(f"  - action: {ai}")
        user = f"Date: {date_str}\n\nConversation notes:\n" + "\n".join(lines)
        try:
            return self.chat(DAILY_SYSTEM_PROMPT, user)
        except Exception as exc:
            log.error("Daily recap LLM call failed: %s", exc)
            # Graceful degradation: still produce a readable recap.
            body = "\n".join(f"- **{e.get('title', 'Untitled')}** ({e.get('started_at', '?')}): {e.get('summary', '')}" for e in episodes)
            return f"# Daily Recap — {date_str}\n\n(LLM unavailable; notes listed verbatim.)\n\n{body}\n"
