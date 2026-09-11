# Privacy & recording-consent notes for pi-recap

pi-recap is designed to be private **by default**, mirroring Apple's approach
with Siri Recap:

- **No raw audio is retained.** Speech segments are transcribed in RAM by a
  local faster-whisper model; the wav bytes are released immediately after
  transcription and never written to disk. This is controlled by
  `delete_audio_after_transcription: true` in `config.yaml`. (Setting it to
  `false` keeps debug wavs under `data/debug_audio/` — only do this briefly
  while troubleshooting, then delete them.)
- **No cloud transcription by default.** The default LLM backend is local
  Ollama. If you switch `llm.backend` to `openai`, transcripts are sent to
  whatever OpenAI-compatible endpoint you configure — that's your choice,
  and your responsibility.
- **No speaker attribution.** The summarizer prompt forbids labeling who said
  what; notes describe *what was discussed*, not *who said it*.
- **No network exfiltration by design.** The recorder makes zero outbound
  requests except to your configured LLM endpoint (localhost by default).

## The part the software can't do for you: consent

**Recording laws vary.** In the US, some states require only one party's
consent to record a conversation (one-party consent), while others require
**all parties** to consent (two-party / all-party consent, e.g. California,
Florida, Illinois, and others). Other countries have their own rules, many
stricter. This project can't give legal advice — check the rules where you
live and where the device operates.

Practical guidance:

1. **Your own space, your own voice** (e.g. solo dictation-style notes,
   journaling aloud) is the lowest-risk use.
2. **Around other people: tell them.** A small sign ("this room has an AI
   note-taker running") or a verbal heads-up goes a long way, and is legally
   required in all-party-consent places.
3. **Use pause liberally.** The web UI has pause/resume; the recorder checks
   the flag every second. Pause before sensitive conversations — or better,
   leave the Pi off for them.
4. **Workplaces** may have their own policies on recording; check before
   deploying at work.

## Wiping your data

Everything lives in two places:

```bash
# Episodes, notes, and settings flags:
rm ~/pi-recap/data/recap.db
# Daily recap markdown files:
rm ~/pi-recap/recaps/*.md
# (Only if you disabled the default:) debug audio:
rm -rf ~/pi-recap/data/debug_audio
```

Deleting `data/recap.db` removes all transcripts, summaries, and the
pause/rewind state in one shot. There is no other copy — no cloud backup,
no telemetry.
