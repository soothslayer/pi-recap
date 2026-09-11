"""Configuration loading: config.yaml + environment variable overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # optional; plain env vars still work
    def load_dotenv(*args, **kwargs):
        return False

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.yaml"

load_dotenv(BASE_DIR / ".env")  # optional; missing file is fine


@dataclass
class AudioConfig:
    device: Any = None  # None = default input device; int index or name substring
    sample_rate: int = 16000
    channels: int = 1
    vad_aggressiveness: int = 2  # 0-3
    pre_speech_padding_ms: int = 300
    post_speech_padding_ms: int = 500
    max_segment_seconds: int = 120


@dataclass
class WhisperConfig:
    model: str = "base.en"
    compute_type: str = "int8"
    device: str = "cpu"
    whisper_remote_url: str = ""       # "" = disabled; else whisper.cpp server URL
    whisper_remote_timeout_s: int = 30
    whisper_remote_check_interval_s: int = 60


@dataclass
class LLMConfig:
    backend: str = "ollama"  # "ollama" | "openai"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    timeout_seconds: int = 120
    private_mode: bool = False  # startup default; live state lives in kv table


@dataclass
class RecapConfig:
    episode_gap_minutes: int = 10
    rewind_buffer_seconds: int = 15
    delete_audio_after_transcription: bool = True
    data_dir: str = "data"
    sync_dir: str = "recaps"          # Markdown exports live here (Syncthing folder)
    rewind_chime: bool = True         # audible chime when a rewind is served
    chime_device: Any = None          # None = default output device
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    flask_secret: str = "change-me"


@dataclass
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    recap: RecapConfig = field(default_factory=RecapConfig)

    @property
    def data_dir_path(self) -> Path:
        p = Path(self.recap.data_dir)
        return p if p.is_absolute() else BASE_DIR / p


def _merge(dataclass_type: type, values: dict) -> Any:
    """Build a dataclass from a dict, ignoring unknown keys."""
    import dataclasses

    known = {f.name for f in dataclasses.fields(dataclass_type)}
    return dataclass_type(**{k: v for k, v in values.items() if k in known})


def load_config(path: str | Path | None = None) -> Config:
    """Load config.yaml and apply environment overrides."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict = {}
    if cfg_path.exists():
        with open(cfg_path) as f:
            raw = yaml.safe_load(f) or {}

    cfg = Config(
        audio=_merge(AudioConfig, raw.get("audio", {})),
        whisper=_merge(WhisperConfig, raw.get("whisper", {})),
        llm=_merge(LLMConfig, raw.get("llm", {})),
        recap=_merge(RecapConfig, raw.get("recap", {})),
    )

    # Environment overrides (also picked up from .env via load_dotenv).
    if os.getenv("OLLAMA_HOST"):
        cfg.llm.ollama_host = os.environ["OLLAMA_HOST"]
    if os.getenv("OPENAI_API_KEY"):
        cfg.llm.openai_api_key = os.environ["OPENAI_API_KEY"]
    if os.getenv("OPENAI_BASE_URL"):
        cfg.llm.openai_base_url = os.environ["OPENAI_BASE_URL"]
    if os.getenv("FLASK_SECRET"):
        cfg.recap.flask_secret = os.environ["FLASK_SECRET"]
    if os.getenv("DATA_DIR"):
        cfg.recap.data_dir = os.environ["DATA_DIR"]

    # Resolve relative data_dir and sync_dir against the project root.
    if not Path(cfg.recap.data_dir).is_absolute():
        cfg.recap.data_dir = str(BASE_DIR / cfg.recap.data_dir)
    if not Path(cfg.recap.sync_dir).is_absolute():
        cfg.recap.sync_dir = str(BASE_DIR / cfg.recap.sync_dir)

    return cfg
