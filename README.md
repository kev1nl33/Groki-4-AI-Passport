[简体中文](README.zh-CN.md) | **English**

# Groki-4-AI-Passport

A voice-conversation application for the
[FoloToy AI Passport](https://github.com/FoloToy/ai-passport) (ESP32-C3,
8 MB flash, 240×320 ST7789 display, ES8311 codec, three buttons) that talks
to Grok through a gateway running on your computer over the local network.

One repository, two parts:

- **`firmware/`** — ESP-IDF 5.5.3 firmware derived from the upstream MIT
  firmware: SoftAP first-time provisioning, a status page, hold-to-talk
  capture, 16 kHz PCM uplink, 24 kHz PCM playback, volume control with NVS
  persistence, and bounded reconnection.
- **`gateway/`** — the computer-side Python gateway derived from the
  [Grok Bot](https://github.com/JosssphZhou/groki-bot) gateway (MIT), with an
  explicitly negotiated PCM passthrough mode: `pcm/16000/1/20` uplink,
  `pcm/24000/1/20` downlink, manual turn commit, frame pacing, backpressure,
  and per-reply output budgets. Opus clients keep the upstream 60 ms path.

## How it fits together

```text
AI Passport ⇄ Wi-Fi ⇄ WebSocket ws://<LAN IP>:8765 ⇄ gateway ⇄ Grok login voice
```

Voice runs on your existing Grok Bot desktop login (a small local Node session
bridge keeps it signed in) — no separate xAI voice API key. Audio is raw
PCM16; there is no Opus on the device, which keeps the no-PSRAM memory budget
small.

## Status

Stages A–E are implemented and device-verified (2026-10-19): provisioning and
NVS persistence, WebSocket handshake with reconnection, PCM uplink with
recognition, 24 kHz playback with half-duplex, and release-to-submit manual
turns with volume persistence. See [DELIVERY.md](DELIVERY.md) for the full
acceptance record, known limitations, and the items not yet re-tested on
hardware.

## Quick start

1. **Build the firmware** — activate ESP-IDF 5.5.3, then from `firmware/`:

   ```sh
   ./tools/validate.sh          # on macOS: CC="$PWD/../tools/apple-cc" ./tools/validate.sh
   ```

   Flash the merged image from `0x0` (a merged flash can reset stored device
   data). Details in [DELIVERY.md](DELIVERY.md).

2. **Install the gateway** (macOS with the Grok Bot app signed in, Node 22+,
   [uv](https://docs.astral.sh/uv/)):

   ```sh
   cd gateway && uv sync --all-extras
   ```

3. **Configure `gateway/.env`** with your own agent ID:

   ```dotenv
   STACKCHAN_VOICE_BACKEND=gbot
   STACKCHAN_GBOT_VOICE_BOT_ID=YOUR_AGENT_ID
   STACKCHAN_TOOL_BOT=assistant
   STACKCHAN_TOOL_BOT_ID=YOUR_AGENT_ID
   ```

4. **Start the login helper and the gateway** — the exact commands are in
   [DELIVERY.md](DELIVERY.md); check `http://127.0.0.1:8766/debug/status`.

5. **Provision the device** — on first boot it opens a SoftAP (name and random
   password on screen); connect a phone, open `http://192.168.4.1`, and fill in
   your Wi-Fi plus `ws://<your computer's LAN IP>:8765`.

6. **Talk** — wait for `Ready`, hold OK to speak, release to send. UP/DOWN
   adjust the volume.

## Documentation

- [DELIVERY.md](DELIVERY.md) — delivery notes: build, flashing, gateway
  startup, provisioning, buttons, states, limitations (Chinese).
- [firmware/docs/assets/passport-voice.md](firmware/docs/assets/passport-voice.md)
  — per-stage design and acceptance plan (English ·
  [中文](firmware/docs/assets/passport-voice.zh_CN.md)).
- [gateway/README.md](gateway/README.md) — gateway reference (upstream
  document, kept for the Grok backend details).

## Licenses and attribution

MIT. `firmware/` derives from
[FoloToy/ai-passport](https://github.com/FoloToy/ai-passport) (MIT);
`gateway/` derives from
[JosssphZhou/groki-bot](https://github.com/JosssphZhou/groki-bot) (MIT, itself
from kisaragi-mochi/stackchan-mcp). See [NOTICE.md](NOTICE.md). Keep API keys
and agent IDs in `gateway/.env` — it is never committed.
