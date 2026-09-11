"""Local speech-to-text with faster-whisper (no cloud, no audio retention).

Optional LAN offload: if whisper_remote_url is configured, segments are POSTed
to a whisper.cpp server on the local network first (/inference endpoint);
on any failure a circuit breaker falls back to the local model and stops
hammering the dead host for whisper_remote_check_interval_s.
"""
from __future__ import annotations

import io
import logging
import time
import wave

import numpy as np
import requests

log = logging.getLogger("pi-recap.transcribe")


def _wav_bytes_to_float32(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        n_channels = w.getnchannels()
        sample_rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels == 2:
        audio = audio.reshape(-1, 2).mean(axis=1)
    return np.ascontiguousarray(audio), sample_rate


class Transcriber:
    """faster-whisper locally, with optional whisper.cpp LAN offload.

    The local model is loaded lazily so a Pi that only uses the remote
    endpoint never pays the model-load cost.
    """

    def __init__(
        self,
        model: str = "base.en",
        compute_type: str = "int8",
        device: str = "cpu",
        remote_url: str = "",
        remote_timeout_s: int = 30,
        remote_check_interval_s: int = 60,
    ):
        self.model_name = model
        self.compute_type = compute_type
        self.device = device
        self.model = None  # loaded lazily on first local use

        self.remote_url = (remote_url or "").rstrip("/")
        self.remote_timeout_s = remote_timeout_s
        self.remote_check_interval_s = remote_check_interval_s
        self._remote_dead_until = 0.0  # circuit breaker: skip remote until this time
        if self.remote_url:
            log.info("Whisper LAN offload enabled: %s", self.remote_url)

    # ------------------------------------------------------------- backends

    def _ensure_local(self):
        if self.model is None:
            from faster_whisper import WhisperModel

            log.info(
                "Loading whisper model %r (device=%s, %s) ...",
                self.model_name, self.device, self.compute_type,
            )
            self.model = WhisperModel(
                self.model_name, device=self.device, compute_type=self.compute_type
            )
            log.info("Whisper model loaded.")
        return self.model

    def _remote_healthy(self) -> bool:
        """Circuit breaker: don't hammer a host that recently failed."""
        return bool(self.remote_url) and time.time() >= self._remote_dead_until

    def _transcribe_remote(self, wav_bytes: bytes) -> dict | None:
        """POST wav bytes to a whisper.cpp server's /inference endpoint."""
        try:
            resp = requests.post(
                self.remote_url + "/inference",
                files={"file": ("segment.wav", wav_bytes, "audio/wav")},
                timeout=self.remote_timeout_s,
            )
            resp.raise_for_status()
            body = resp.text.strip()
            # whisper.cpp may return plain text or {"text": "..."} JSON.
            try:
                import json as _json

                parsed = _json.loads(body)
                if isinstance(parsed, dict) and parsed.get("text"):
                    body = str(parsed["text"]).strip()
            except ValueError:
                pass
            if not body:
                return None
            return {"text": body, "language": "", "duration": 0.0}
        except Exception as exc:
            log.warning("Remote whisper failed (%s); using local model", exc)
            self._remote_dead_until = time.time() + self.remote_check_interval_s
            return None

    def _transcribe_local(self, wav_bytes: bytes) -> dict | None:
        if not wav_bytes:
            return None
        try:
            audio, _sr = _wav_bytes_to_float32(wav_bytes)
        except Exception as exc:
            log.warning("Could not decode wav bytes: %s", exc)
            return None
        if audio.size < 1600:  # < 0.1s of audio: not worth transcribing
            return None

        model = self._ensure_local()
        segments, info = model.transcribe(
            audio,
            beam_size=5,
            vad_filter=False,  # we already ran webrtcvad upstream
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        # faster-whisper needs the generator consumed before info is reliable;
        # duration is derived from the audio we fed in.
        duration = float(audio.size) / 16000.0
        del audio

        if not text:
            return None
        return {"text": text, "language": info.language, "duration": duration}

    # ------------------------------------------------------------------ api

    def transcribe(self, wav_bytes: bytes) -> dict | None:
        """Transcribe in-memory wav bytes.

        Returns {"text", "language", "duration"} or None when there is no
        usable speech. The caller owns the wav bytes and should release them
        immediately after this returns (see main.py).
        """
        if self._remote_healthy():
            result = self._transcribe_remote(wav_bytes)
            if result:
                return result
            # Remote failed (breaker now tripped) — fall through to local.
        return self._transcribe_local(wav_bytes)
