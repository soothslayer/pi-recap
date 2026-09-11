"""Flask web UI: browse episodes, daily recaps, Live Rewind, pause/resume.

Run:  python -m recap.web   (binds web_host:web_port from config.yaml)
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify, render_template, request

log = logging.getLogger("pi-recap.web")
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def create_app(config=None) -> Flask:
    from .config import load_config
    from . import store

    cfg = config or load_config()
    store.init_db(cfg.recap.data_dir)

    app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
    app.secret_key = cfg.recap.flask_secret
    app.config["CFG"] = cfg

    # ------------------------------------------------------------------ pages

    def _template_ctx(**extra):
        extra["private"] = store.kv_get("private_mode", "0") == "1"
        return extra

    @app.get("/")
    def index():
        q = request.args.get("q", "").strip() or None
        episodes = store.list_episodes(limit=100, search=q)
        paused = store.kv_get("capture_paused", "0") == "1"
        return render_template("index.html", **_template_ctx(episodes=episodes, q=q or "", paused=paused))

    @app.get("/episode/<int:episode_id>")
    def episode_detail(episode_id: int):
        ep = store.get_episode(episode_id)
        if not ep:
            return render_template("404.html", **_template_ctx(message="Episode not found")), 404
        return render_template("episode.html", **_template_ctx(ep=ep))

    @app.get("/daily")
    def daily_list():
        dailies = store.list_dailies()
        return render_template("daily.html", **_template_ctx(dailies=dailies, daily=None))

    @app.get("/daily/<date_str>")
    def daily_detail(date_str: str):
        daily = store.get_daily(date_str)
        if not daily:
            return render_template("404.html", **_template_ctx(message="No recap for that date")), 404
        return render_template("daily.html", **_template_ctx(dailies=store.list_dailies(), daily=daily))

    @app.get("/rewind")
    def rewind_page():
        text = store.kv_get("last_rewind_text", "(No rewind captured yet.)")
        at = store.kv_get("last_rewind_at", "")
        return render_template("rewind.html", **_template_ctx(text=text, at=at))

    # -------------------------------------------------------------------- api

    @app.post("/api/pause")
    def api_pause():
        store.kv_set("capture_paused", "1")
        log.info("Capture paused via web UI")
        return jsonify({"paused": True})

    @app.post("/api/resume")
    def api_resume():
        store.kv_set("capture_paused", "0")
        log.info("Capture resumed via web UI")
        return jsonify({"paused": False})

    @app.get("/api/status")
    def api_status():
        return jsonify(
            {
                "paused": store.kv_get("capture_paused", "0") == "1",
                "private": store.kv_get("private_mode", "0") == "1",
                "episodes": store.count_episodes(),
                "last_rewind_text": store.kv_get("last_rewind_text", ""),
                "last_rewind_at": store.kv_get("last_rewind_at", ""),
            }
        )

    @app.post("/api/private/on")
    def api_private_on():
        store.kv_set("private_mode", "1")
        log.info("Private mode enabled via web UI — cloud backends blocked")
        return jsonify({"private": True})

    @app.post("/api/private/off")
    def api_private_off():
        store.kv_set("private_mode", "0")
        log.info("Private mode disabled via web UI")
        return jsonify({"private": False})

    @app.post("/api/rewind")
    def api_rewind():
        """Ask the recorder process to transcribe its ring buffer.

        The web UI and the recorder are separate processes; they coordinate
        through the kv store. main.py watches for this flag.
        """
        import time

        store.kv_set("rewind_requested", str(time.time()))
        return jsonify({"requested": True})

    @app.post("/api/delete/<int:episode_id>")
    def api_delete(episode_id: int):
        ok = store.delete_episode(episode_id)
        if not ok:
            return jsonify({"deleted": False}), 404
        return jsonify({"deleted": True})

    return app


def main() -> None:
    from .config import load_config

    cfg = load_config()
    logging.basicConfig(level=logging.INFO)
    app = create_app(cfg)
    log.info("Serving on %s:%d", cfg.recap.web_host, cfg.recap.web_port)
    app.run(host=cfg.recap.web_host, port=cfg.recap.web_port, threaded=True)


if __name__ == "__main__":
    main()
