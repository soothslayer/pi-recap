"""Orchestrator: mic -> VAD -> whisper -> episodes -> LLM summary -> SQLite.

Pipeline per speech segment:
  1. AudioCapture emits wav bytes (RAM only).
  2. Transcriber turns them into text (local faster-whisper).
  3. The wav bytes are released immediately — never written to disk
     (unless delete_audio_after_transcription is false, for debugging).
  4. EpisodeBuilder groups segments into conversations.
  5. Summarizer writes title/summary/key_points/action_items.
  6. Episode is saved to SQLite.

Also watches kv flags set by the web UI:
  - capture_paused=1 -> pause the mic stream; 0 -> resume.
  - rewind_requested=<timestamp> -> transcribe the 15s ring buffer now.
  - private_mode=1 -> summarizer uses local Ollama only, never the cloud.

Run:  python -m recap.main
"""
from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s"
)
log = logging.getLogger("pi-recap.main")

shutdown = threading.Event()


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        log.info("Received signal %d; shutting down...", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main() -> int:
    from . import store
    from .capture import AudioCapture
    from .config import load_config
    from .episodes import EpisodeBuilder
    from .export import export_episode
    from .rewind import Rewind, play_chime
    from .summarize import Summarizer
    from .transcribe import Transcriber

    _install_signal_handlers()
    cfg = load_config()
    store.init_db(cfg.recap.data_dir)
    log.info("Data dir: %s", cfg.recap.data_dir)

    # Seed the private-mode kv flag from config on first run; the web UI
    # owns it from then on.
    if store.kv_get("private_mode") is None:
        store.kv_set("private_mode", "1" if cfg.llm.private_mode else "0")
    log.info("Private mode: %s", "ON" if store.kv_get("private_mode") == "1" else "off")

    summarizer = Summarizer(
        cfg.llm,
        private_mode_check=lambda: store.kv_get("private_mode", "0") == "1",
    )
    transcriber = Transcriber(
        model=cfg.whisper.model,
        compute_type=cfg.whisper.compute_type,
        device=cfg.whisper.device,
        remote_url=cfg.whisper.whisper_remote_url,
        remote_timeout_s=cfg.whisper.whisper_remote_timeout_s,
        remote_check_interval_s=cfg.whisper.whisper_remote_check_interval_s,
    )

    # ------------------------------------------------------------- episode sink
    def handle_episode(ep) -> None:
        data = ep.to_dict()
        try:
            note = summarizer.summarize(data["transcript"])
        except Exception:
            log.exception("Summarizer failed unexpectedly")
            note = {"title": "Untitled conversation", "summary": "", "key_points": [], "action_items": []}
        pending = bool(note.get("_pending"))
        row_id = store.save_episode(
            {
                "episode_key": data["id"],
                "started_at": data["started_at"],
                "ended_at": data["ended_at"],
                "title": note.get("title", ""),
                "summary": note.get("summary", ""),
                "key_points": note.get("key_points", []),
                "action_items": note.get("action_items", []),
                "transcript": data["transcript"],
                "audio_deleted": cfg.recap.delete_audio_after_transcription,
                "pending_summary": pending,
            }
        )
        if pending:
            log.warning("Episode #%d queued unsummarized (private mode, local LLM down)", row_id)
        else:
            log.info("Saved episode #%d: %s", row_id, note.get("title", ""))
        try:
            export_episode(
                {
                    "title": note.get("title", ""),
                    "started_at": data["started_at"],
                    "ended_at": data["ended_at"],
                    "summary": note.get("summary", ""),
                    "key_points": note.get("key_points", []),
                    "action_items": note.get("action_items", []),
                    "transcript": data["transcript"],
                    "pending_summary": pending,
                },
                cfg.recap.sync_dir,
            )
        except Exception:
            log.exception("Episode Markdown export failed")

    builder = EpisodeBuilder(
        gap_minutes=cfg.recap.episode_gap_minutes, on_episode=handle_episode
    )

    # ---------------------------------------------------------- segment worker
    seg_queue: queue.Queue = queue.Queue()
    debug_dir = Path(cfg.recap.data_dir) / "debug_audio"

    def on_segment(wav_bytes: bytes, start_ts: float, end_ts: float) -> None:
        seg_queue.put((wav_bytes, start_ts, end_ts))

    def worker() -> None:
        while not shutdown.is_set():
            try:
                item = seg_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            wav_bytes, start_ts, end_ts = item
            try:
                result = transcriber.transcribe(wav_bytes)
            except Exception:
                log.exception("Transcription failed")
                result = None
            finally:
                # Privacy: audio never touches disk by default; release RAM now.
                if not cfg.recap.delete_audio_after_transcription:
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    (debug_dir / f"seg_{int(start_ts)}.wav").write_bytes(wav_bytes)
                del wav_bytes
            if result and result.get("text"):
                log.debug("Transcribed %.1fs: %s...", result["duration"], result["text"][:80])
                builder.add_segment(result["text"], start_ts, end_ts)
            seg_queue.task_done()

    worker_thread = threading.Thread(target=worker, name="transcribe-worker", daemon=True)
    worker_thread.start()

    # ---------------------------------------------------------------- capture
    capture = AudioCapture(
        cfg.audio,
        rewind_seconds=cfg.recap.rewind_buffer_seconds,
        on_segment=on_segment,
    )
    try:
        capture.start()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    rewind = Rewind(capture, transcriber, cfg.recap.data_dir, seconds=cfg.recap.rewind_buffer_seconds)
    last_rewind_req = store.kv_get("rewind_requested", "0")

    # -------------------------------------------------------------- main loop
    log.info("pi-recap running. Ctrl+C to stop.")
    try:
        while not shutdown.is_set():
            # Web UI pause/resume flag.
            paused = store.kv_get("capture_paused", "0") == "1"
            if paused and capture.running:
                capture.pause()
            elif not paused and not capture.running:
                capture.resume()

            # Web UI Live Rewind request.
            req = store.kv_get("rewind_requested", "0")
            if req != last_rewind_req:
                last_rewind_req = req
                store.kv_delete("rewind_requested")
                try:
                    text = rewind.what_was_just_said()
                    log.info("Rewind: %s", text[:120])
                    # Audible cue for people nearby, Apple-style.
                    if cfg.recap.rewind_chime:
                        play_chime(device=cfg.recap.chime_device)
                except Exception:
                    log.exception("Rewind failed")

            shutdown.wait(1.0)
    finally:
        log.info("Flushing in-progress episode...")
        builder.flush()
        seg_queue.join()  # drain remaining segments
        capture.close()
        log.info("Shutdown complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
