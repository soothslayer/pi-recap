#!/usr/bin/env bash
# pi-recap installer for Raspberry Pi OS (Bookworm, 64-bit).
# Idempotent-ish: safe to re-run; skips steps that are already done.
set -euo pipefail

cd "$(dirname "$0")"
echo "==> pi-recap installer"

echo "==> [1/5] Installing system packages (portaudio, ffmpeg, venv)…"
sudo apt update
sudo apt install -y git portaudio19-dev ffmpeg python3-venv python3-pip curl

echo "==> [2/5] Creating Python virtualenv…"
if [ ! -d venv ]; then
  python3 -m venv venv
  echo "    created venv/"
else
  echo "    venv/ already exists, reusing"
fi

echo "==> [3/5] Installing Python dependencies (this takes a while on a Pi)…"
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

echo "==> [4/5] Preparing directories…"
mkdir -p data recaps
if [ ! -f .env ]; then
  cp .env.example .env
  echo "    created .env from .env.example — edit it if you use the OpenAI backend"
fi

echo "==> [5/5] Checking audio group membership…"
if ! groups "$USER" | grep -q '\baudio\b'; then
  echo "    adding $USER to the 'audio' group (mic access)…"
  sudo usermod -aG audio "$USER"
  echo "    NOTE: log out and back in for the audio group to take effect"
else
  echo "    $USER is already in the audio group"
fi

echo ""
echo "==================== NEXT STEPS ===================="
echo "1. Install Ollama (local LLM) and pull a model:"
echo "     curl -fsSL https://ollama.com/install.sh | sh"
echo "     ollama pull llama3.1:8b     # ~4.7 GB; on a 4 GB Pi use llama3.2:3b"
echo ""
echo "2. Review config.yaml (mic device, whisper model, episode gap…)"
echo ""
echo "3. Verify your microphone:"
echo "     venv/bin/python -c \"import sounddevice as sd; print(sd.query_devices())\""
echo ""
echo "4. Try it in the foreground first:"
echo "     venv/bin/python -m recap.main"
echo "   Web UI: http://\$(hostname -I | awk '{print \$1}'):8080"
echo ""
echo "5. Install as services (after the foreground test works):"
echo "     sudo cp systemd/recap.service /etc/systemd/system/"
echo "     sudo cp systemd/recap-daily.service systemd/recap-daily.timer /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now recap.service"
echo "     sudo systemctl enable --now recap-daily.timer"
echo ""
echo "6. Read PRIVACY.md before leaving it always-on around other people."
echo "===================================================="
