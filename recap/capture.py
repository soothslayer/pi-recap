"""Continuous audio capture with voice activity detection.

AudioCapture reads 16 kHz mono audio via sounddevice, runs webrtcvad on
30 ms frames, and emits speech segments (in-memory wav bytes + wall-clock
timestamps) through a callback. It also keeps a rolling in-memory ring
buffer of the last N seconds of audio for the Live Rewind feature.

Privacy note: emitted wav bytes are meant to be transcribed immediately and
released. Nothing is written to disk by this module.
"""
from __future__ import annotations

import io
import logging
import queue
import threading
import time
import wave
from collections import deque
from typing import Callable

import numpy as np
import sounddevice as sd
import webrtcvad

log = logging.getLogger("pi-recap.capture")

FRAME_MS = 30  # webrtcvad requires 10/20/30 ms frames


def _to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Pack int16 mono samples into wav bytes (in memory only)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


class AudioCapture:
    """Capture audio, detect speech, emit segments, keep a rewind ring buffer."""

    def __init__(
        self,
        audio_cfg,
        rewind_seconds: int = 15,
        on_segment: Callable[[bytes, float, float], None] | None = None,
    ):
        self.cfg = audio_cfg
        self.rewind_seconds = rewind_seconds
        self.on_segment = on_segment

        self.sample_rate = audio_cfg.sample_rate
        self.frame_samples = self.sample_rate * FRAME_MS // 1000  # 480 @16kHz

        self._vad = webrtcvad.Vad(audio_cfg.vad_aggressiveness)

        # Rolling ring buffer of raw int16 samples for Live Rewind (RAM only).
        self._ring: deque[int] = deque(maxlen=rewind_seconds * self.sample_rate)
        self._ring_lock = threading.Lock()

        # Segment state machine.
        self._state_lock = threading.Lock()
        self._preroll: deque[np.ndarray] = deque(
            maxlen=max(1, audio_cfg.pre_speech_padding_ms // FRAME_MS)
        )
        self._postroll_frames = max(1, audio_cfg.post_speech_padding_ms // FRAME_MS)
        self._max_frames = max(
            1, audio_cfg.max_segment_seconds * self.sample_rate // self.frame_samples
        )
        self._reset_segment_state()

        self._total_samples = 0
        self._stream_start_wall = 0.0

        # Segment emission happens on a worker thread so the audio
        # callback never blocks.
        self._emit_queue: queue.Queue = queue.Queue()
        self._emitter_thread: threading.Thread | None = None
        self._stream: sd.InputStream | None = None
        self._running = False

    # ------------------------------------------------------------------ state

    def _reset_segment_state(self) -> None:
        self._collecting = False
        self._frames: list[np.ndarray] = []
        self._trailing = 0
        self._seg_start_wall = 0.0

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Open the input stream and begin capturing. Raises RuntimeError if no mic."""
        if self._running:
            return
        try:
            self._stream = sd.InputStream(
                device=self.cfg.device,
                samplerate=self.sample_rate,
                channels=self.cfg.channels,
                dtype="int16",
                blocksize=self.frame_samples,
                callback=self._audio_callback,
            )
        except sd.PortAudioError as exc:
            raise RuntimeError(
                "No audio input device found. Plug in a USB mic (or ReSpeaker HAT), "
                "then check available devices with:\n"
                '  python -c "import sounddevice as sd; print(sd.query_devices())"\n'
                f"PortAudio said: {exc}"
            ) from exc
        self._total_samples = 0
        self._stream_start_wall = time.time()
        self._running = True
        self._emitter_thread = threading.Thread(
            target=self._emitter_loop, name="segment-emitter", daemon=True
        )
        self._emitter_thread.start()
        self._stream.start()
        log.info("AudioCapture started (device=%s)", self.cfg.device or "default")

    def pause(self) -> None:
        """Temporarily stop the stream without tearing it down."""
        if self._stream and self._running:
            self._stream.stop()
            log.info("AudioCapture paused")

    def resume(self) -> None:
        """Resume a paused stream."""
        if self._stream and self._running:
            self._stream.start()
            log.info("AudioCapture resumed")

    def close(self) -> None:
        """Stop everything and release the stream."""
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._emit_queue.put(None)  # sentinel for emitter thread
        if self._emitter_thread:
            self._emitter_thread.join(timeout=5)
        log.info("AudioCapture closed")

    @property
    def running(self) -> bool:
        return self._running and self._stream is not None and self._stream.active

    # --------------------------------------------------------------- capturing

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.warning("Audio status: %s", status)
        samples = np.ascontiguousarray(indata[:, 0], dtype=np.int16).copy()

        with self._ring_lock:
            self._ring.extend(samples.tolist())

        try:
            is_speech = self._vad.is_speech(samples.tobytes(), self.sample_rate)
        except Exception as exc:  # never let VAD kill the stream
            log.warning("VAD error: %s", exc)
            return

        with self._state_lock:
            self._total_samples += len(samples)
            if not self._collecting:
                self._preroll.append(samples)
                if is_speech:
                    self._collecting = True
                    self._frames = list(self._preroll)
                    self._preroll.clear()
                    self._trailing = 0
                    start_sample = self._total_samples - len(self._frames) * len(samples)
                    self._seg_start_wall = (
                        self._stream_start_wall + start_sample / self.sample_rate
                    )
            else:
                self._frames.append(samples)
                if is_speech:
                    self._trailing = 0
                else:
                    self._trailing += 1
                if (
                    self._trailing >= self._postroll_frames
                    or len(self._frames) >= self._max_frames
                ):
                    self._emit_locked()

    def _emit_locked(self) -> None:
        """Package the current segment and hand it to the emitter thread."""
        if not self._frames:
            self._reset_segment_state()
            return
        audio = np.concatenate(self._frames)
        end_wall = self._stream_start_wall + self._total_samples / self.sample_rate
        start_wall = self._seg_start_wall
        self._emit_queue.put((audio, start_wall, end_wall))
        self._reset_segment_state()

    def _emitter_loop(self) -> None:
        while True:
            item = self._emit_queue.get()
            if item is None:
                break
            audio, start_wall, end_wall = item
            try:
                wav_bytes = _to_wav_bytes(audio, self.sample_rate)
            finally:
                del audio  # release raw samples ASAP
            duration = end_wall - start_wall
            log.debug("Segment: %.1fs", duration)
            if self.on_segment:
                try:
                    self.on_segment(wav_bytes, start_wall, end_wall)
                except Exception:
                    log.exception("on_segment callback failed")
            del wav_bytes  # caller should have consumed/transcribed it

    # ------------------------------------------------------------------ rewind

    def get_recent_audio(self, seconds: float | None = None) -> bytes:
        """Return the last `seconds` of audio as raw int16 bytes (RAM only)."""
        n = int((seconds or self.rewind_seconds) * self.sample_rate)
        with self._ring_lock:
            samples = list(self._ring)[-n:]
        if not samples:
            return b""
        return np.asarray(samples, dtype=np.int16).tobytes()

    def get_recent_wav(self, seconds: float | None = None) -> bytes:
        """Return the last `seconds` of audio as in-memory wav bytes."""
        raw = self.get_recent_audio(seconds)
        if not raw:
            return b""
        return _to_wav_bytes(
            np.frombuffer(raw, dtype=np.int16), self.sample_rate
        )
