"""Live Rewind: transcribe the last N seconds of audio on demand.

Like the Apple Watch feature, this reads from the in-memory ring buffer kept
by AudioCapture and transcribes it only when asked. Audio is never written
to disk; the resulting text is cached in the kv store for the web UI.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("pi-recap.rewind")


def play_chime(device=None) -> None:
    """Play a short two-tone chime on the Pi's audio output.

    Apple plays an audible tone when Live Rewind surfaces the last 15 s, so
    people nearby know. Same idea here. Never raises: missing audio hardware
    or numpy/sounddevice problems are swallowed with a log line.
    """
    try:
        import numpy as np
        import sounddevice as sd

        sample_rate = 44100
        tone_hz, gap_hz = 880.0, 659.0  # A5 then E5
        tone_len = int(sample_rate * 0.18)
        gap_len = int(sample_rate * 0.06)

        def tone(freq, n):
            t = np.arange(n) / sample_rate
            # Sine with short fade in/out to avoid clicks.
            env = np.ones(n)
            fade = int(sample_rate * 0.02)
            env[:fade] = np.linspace(0, 1, fade)
            env[-fade:] = np.linspace(1, 0, fade)
            return 0.4 * np.sin(2 * np.pi * freq * t) * env

        chime = np.concatenate(
            [tone(tone_hz, tone_len), np.zeros(gap_len), tone(gap_hz, tone_len)]
        ).astype(np.float32)

        kwargs = {"device": device} if device is not None else {}
        sd.play(chime, sample_rate, **kwargs)
        sd.wait()
    except Exception as exc:
        log.warning("Could not play rewind chime: %s", exc)


class Rewind:
    def __init__(self, capture, transcriber, data_dir: str, seconds: float = 15):
        self.capture = capture
        self.transcriber = transcriber
        self.seconds = seconds
        # Imported lazily to avoid a hard import cycle; store must be init'd.
        from . import store as _store

        self._store = _store
        _store.init_db(data_dir)

    def what_was_just_said(self) -> str:
        """Transcribe the ring buffer's recent audio and cache the text."""
        wav = self.capture.get_recent_wav(self.seconds)
        if not wav or len(wav) < 4000:  # wav header alone is 44 bytes; be conservative
            text = "(Not enough recent audio to transcribe.)"
        else:
            try:
                result = self.transcriber.transcribe(wav)
            finally:
                del wav  # release immediately; never persisted
            text = result["text"].strip() if result and result.get("text") else "(Silence — nothing transcribed.)"
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self._store.kv_set("last_rewind_text", text)
        self._store.kv_set("last_rewind_at", now)
        log.info("Rewind captured %d chars at %s", len(text), now)
        return text

    def last_text(self) -> dict:
        return {
            "text": self._store.kv_get("last_rewind_text", "(No rewind captured yet.)"),
            "at": self._store.kv_get("last_rewind_at", ""),
        }
