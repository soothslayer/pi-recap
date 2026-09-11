# pi-recap

A self-hosted, privacy-first clone of Apple Watch's **Siri Recap** — running on a Raspberry Pi.

It listens ambiently, detects conversations, and turns them into notes with a **title, summary, key points, and action items** — no raw audio ever stored. It also includes a **Live Rewind**: ask "what was just said?" and get the last ~15 seconds transcribed on demand.

Inspired by Apple's Audio Intelligence features (Siri Recap + Live Rewind), rebuilt so that *you* own the whole pipeline: local mic → local transcription → local LLM → local database.

## How it maps to Apple's features

| Apple Watch (Siri Recap / Live Rewind) | pi-recap |
|---|---|
| Ambient listening takes high-level notes of conversations | `recap/capture.py` + `recap/episodes.py` detect speech and group it into conversation episodes |
| Apple Intelligence generates title, summary, key points | `recap/summarize.py` (local Ollama, or optional OpenAI-compatible API) |
| Review notes in the Siri app | `recap/web.py` — local web UI at `http://<pi-ip>:8080` |
| Live Rewind: last 15s of conversation on demand | `recap/rewind.py` — rolling in-memory ring buffer, transcribed only when you ask |
| No audio recordings stored | `delete_audio_after_transcription: true` — wav bytes are transcribed in RAM and released immediately, never written to disk |
| No speaker attribution | Summaries never label who said what (enforced in the system prompt) |
| On/off + scheduling | `/api/pause` + `/api/resume` in the web UI |
| Rewind chime (people nearby hear a tone when you rewind) | `rewind_chime: true` — `recap/rewind.py` plays a short two-tone chime on the Pi's speaker |
| Private mode (nothing leaves your network) | 🔒 toggle in the web UI (`/api/private/on|off`) — forces local Ollama, never the cloud; episodes queue unsummarized if Ollama is down |
| Daily notes synced to your devices | Each episode + daily recap is exported as Markdown under `recaps/` — share it with [Syncthing](https://syncthing.net) (see below) |

## Architecture

```
 ┌─────────────┐   30ms frames    ┌──────────┐   speech segments   ┌──────────────┐
 │ USB mic      │ ─────────────▶  │ webrtcvad │ ───────────────▶  │ faster-whisper│
 │ (16kHz mono) │   ring buffer   │  VAD      │   wav bytes (RAM) │  (local, int8) │
 └─────────────┘   (last 15 s)    └──────────┘        │           └──────┬───────┘
        │                                            │  delete wav     │ transcript
        │                                            ▼  immediately    ▼
        │                                     ┌──────────────┐   ┌──────────────┐
        │                                     │ EpisodeBuilder│──▶│  Summarizer  │
        │                                     │ (10-min gaps  │   │ (Ollama /    │
        │                                     │  split convos)│   │  OpenAI API) │
        │                                     └──────────────┘   └──────┬───────┘
        │                                                              │ JSON note
        │                                                              ▼
        │                                                     ┌──────────────────┐
        └────────────────────────────────────────────────────▶│ SQLite (recap.db)│
              Live Rewind: ring buffer → transcribe on demand │ + web UI :8080   │
                                                              └──────────────────┘
```

**Data flow, one segment at a time:**

1. `capture.py` — `AudioCapture` reads 16 kHz mono audio via `sounddevice`, runs `webrtcvad` on 30 ms frames, emits speech segments (with pre/post padding) as in-memory wav bytes, and keeps the last 15 s of audio in a RAM ring buffer.
2. `transcribe.py` — `Transcriber` (faster-whisper, local, int8) turns each segment into text. The wav bytes are released right after — never saved.
3. `episodes.py` — `EpisodeBuilder` groups segments into episodes; a silence gap longer than `episode_gap_minutes` (default 10) starts a new episode.
4. `summarize.py` — `Summarizer` asks the LLM for strict JSON: `{title, summary, key_points, action_items}`.
5. `store.py` — episodes land in SQLite. `daily.py` merges a day's episodes into a Markdown daily recap.
6. `web.py` — Flask UI to browse episodes, dailies, and Live Rewind; pause/resume capture.

## Parts list

- **Raspberry Pi 5, 8 GB** (recommended). A Pi 4 (4 GB+) works but needs a smaller whisper model and is slower at transcription. A Pi Zero 2 W is too slow for local whisper — not recommended for the full pipeline.
- **Microphone**: any USB mic, or a ReSpeaker 2-Mic HAT for a tidy build.
- **32 GB+ microSD** (A2 rated) with Raspberry Pi OS Bookworm 64-bit.
- *(Optional, wearable use)*: a battery HAT or USB-C power bank + a small case. Note: a Pi is not watch-sized — start with a desk/room unit.

## Setup

```bash
sudo apt update && sudo apt install -y git   # skip if git is already installed
git clone https://github.com/soothslayer/pi-recap.git ~/pi-recap
cd ~/pi-recap
chmod +x install.sh
./install.sh
```

`install.sh` installs system deps (`portaudio19-dev`, `ffmpeg`), creates a venv, and installs Python deps. Then:

```bash
# 1. Install Ollama (local LLM) and pull a model
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1:8b        # ~4.7 GB; needs ~6-8 GB RAM free on the Pi

# 2. Review config
cp config.yaml config.yaml     # already present; edit to taste
nano config.yaml

# 3. Check your mic is visible
venv/bin/python -c "import sounddevice as sd; print(sd.query_devices())"

# 4. Run it
venv/bin/python -m recap.main
```

Browse the UI at `http://<your-pi-ip>:8080`.

### Run as a service (recommended)

```bash
sudo cp systemd/recap.service /etc/systemd/system/
sudo cp systemd/recap-daily.service systemd/recap-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now recap.service
sudo systemctl enable --now recap-daily.timer
```

The daily timer runs `python -m recap.daily` at 06:00 for the previous day.

### Model sizing

| Pi | Whisper model | Notes |
|---|---|---|
| Pi 5 (8 GB) | `base.en` (default) or `turbo` | `turbo` is faster and more accurate; needs ~1-2 GB RAM |
| Pi 4 (4 GB+) | `tiny.en` or `base.en` | `base.en` works but expect real-time lag on long segments |

Set it in `config.yaml` under `whisper.model`. Ollama's `llama3.1:8b` wants ~6 GB free RAM — on a 4 GB Pi, use a smaller model like `llama3.2:3b` or `qwen2.5:3b`, or switch the backend to an OpenAI-compatible API.

## Usage

- **Episodes**: open the web UI — newest conversations first, with search.
- **Live Rewind**: open `/rewind` and hit the button (or `POST /api/rewind`). The recorder transcribes the last 15 s from its RAM buffer and shows the text. Nothing is saved to disk as audio.
- **Daily recap**: generated automatically at 06:00, or manually: `venv/bin/python -m recap.daily` (yesterday) or `venv/bin/python -m recap.daily 2026-09-10`.
- **Pause/resume**: buttons in the web UI, or `curl -X POST http://<pi>:8080/api/pause`.
- **Private mode**: 🔒 toggle in the web UI header (or `curl -X POST http://<pi>:8080/api/private/on`). While on, the summarizer uses only local Ollama — a cloud backend is refused even if configured, and episodes that can't be summarized locally are queued as "pending" instead of being sent to the cloud. Startup default is `llm.private_mode` in `config.yaml`.
- **Rewind chime**: when a rewind is served, the Pi plays a short two-tone chime on its speaker so people nearby know the last 15 s were pulled up. Disable with `rewind_chime: false` in `config.yaml`; pick an output device with `chime_device`.
- **Wipe everything**: `rm data/recap.db recaps/*.md recaps/episodes/*.md` (see PRIVACY.md).

## Syncing recaps to your devices (Syncthing)

Every summarized episode is exported as its own Markdown file under `recaps/episodes/` (plus the daily `recaps/YYYY-MM-DD.md`). To get those notes on your phone or laptop with no cloud account involved:

1. **Install Syncthing on the Pi:**
   ```bash
   sudo apt install -y syncthing
   sudo systemctl enable --now syncthing@$USER
   ```
2. **Install Syncthing** on your phone/laptop from [syncthing.net](https://syncthing.net).
3. Open the Pi's Syncthing web UI at `http://<pi-ip>:8384`, add a folder pointing at `~/pi-recap/recaps` (or whatever `recap.sync_dir` is set to in `config.yaml`), and pair your devices by exchanging device IDs.
4. Everything syncs directly between your devices over the local network — no account, no cloud server, no data leaving your LAN.

Bonus: the Markdown files drop straight into an [Obsidian](https://obsidian.md) or [Logseq](https://logseq.com) vault — point the vault at the synced `recaps/` folder and your conversation notes become searchable second-brain entries.

## Troubleshooting

- **"No audio input device found"** — plug in the USB mic, then check `venv/bin/python -c "import sounddevice as sd; print(sd.query_devices())"`. Make sure your user is in the `audio` group: `sudo usermod -aG audio $USER` (log out/in after).
- **Transcription is slow / lagging** — drop to a smaller whisper model (`tiny.en`) or shorten `max_segment_seconds` in `config.yaml`. Alternative: offload transcription to a faster machine on your LAN — run a [whisper.cpp](https://github.com/ggerganov/whisper.cpp) server there and set `whisper.whisper_remote_url` (e.g. `http://192.168.1.50:8080`). The Pi POSTs each segment to `/inference` and silently falls back to its local model on any failure, so the pipeline keeps working if the server goes down. Audio never leaves your LAN in this mode.
- **Ollama out of memory** — use a smaller model (`ollama pull llama3.2:3b` and set `llm.ollama_model`), or point `llm.backend: openai` at an OpenAI-compatible endpoint.
- **Summaries come back empty / malformed** — the summarizer retries once, then falls back to an extractive stub so the pipeline never crashes. Check Ollama is running: `curl localhost:11434/api/tags`.
- **Web UI unreachable** — the service binds `0.0.0.0:8080` by default; check `sudo systemctl status recap` and your Pi's firewall.
- **Nothing is being captured** — check the pause flag in the UI; `capture_paused` in the `kv` table must be `0`.

## Privacy

See [PRIVACY.md](PRIVACY.md). Short version: raw audio is never written to disk (transcribed in RAM, then released); only transcripts and AI notes are stored. Recording laws vary — inform people around you and check your local one-party vs. all-party consent rules.

## License

MIT — do what you want, just don't use it to be creepy.
