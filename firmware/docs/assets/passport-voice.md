[简体中文](passport-voice.zh_CN.md) · **English**

# Passport Voice — implemented Stages A–F

Status (2026-10-19): Stages A–F are implemented and device-verified. Stage F
(state-driven avatar, firmware `29567dc1…`) passed device acceptance: the face
blinks at Ready, opens wide while listening, sweeps its pupils while thinking,
mouths along with speech without audio stutter, and the setup screen shows the
hotspot password in full. The approved direction was LAN WebSocket, a gateway
PCM extension, and temporary SoftAP first-time provisioning; the scope grew
stage by stage as recorded below. Per-stage acceptance checklists are kept as
written before execution — see the repository-level `DELIVERY.md` for the
observed results.

## Implementation plan and boundaries

Stage A proceeds in five steps. (1) File responsibilities: `main/passport_config.[ch]` hold pure, host-testable form decoding, validation, and the connection model; `main/passport_app.[ch]` hold the lifetime owner task, temporary AP, HTTP form, NVS blob, and the independent status page; `main/main.c` is only startup wiring. (2) Red/green tests: `tests/test_passport_config.c` first fails against stubs, then passes after implementation; `tests/test_stage_a_wiring.py` keeps the callback, teardown, and credential-hygiene contracts. (3) Integration: callbacks only enqueue; `httpd_stop` runs solely in the owner; a `WIFI_EVENT_STA_STOP` barrier plus a live `esp_wifi_sta_get_ap_info`/`esp_netif_get_ip_info` recheck prevents a previous generation's late GOT_IP from approving new credentials. (4) Gate: `./tools/validate.sh --static`, then `--firmware` with the archived merged image. (5) Acceptance: the device checklist below, not run in this change.

1. Add host-tested bounded form decoding, SSID/password/URL validation, and a connection state machine under `main/`; tests live in `tests/` and run in the existing validation gate.
2. One application owner task serializes network and storage operations. Wi-Fi and button callbacks only enqueue events. HTTP receives a bounded complete form, validates a per-session token and same-origin request, returns its response before handing the candidate to the owner.
3. With no credentials, open a 2.4 GHz WPA2 SoftAP for one client. Display its name, a fresh random password, and `http://192.168.4.1`. Never log passwords. Keep the old versioned NVS blob until a candidate obtains a STA IP; save all fields in one blob and commit. Never erase NVS on initialization errors.
4. A new independent LVGL status page uses BSP display, buttons, shared I2C, and battery. Hold UP while idle to configure; OK cancels and restores the old configuration, or retries errors when no configuration session is open. A failed save remains recoverable. No combination keys, demo shell, or fake volume control. BLE remains disabled; audio initialization is deferred to the first hold-to-talk in Stage C.
5. Run focused red/green host tests, the complete ESP-IDF 5.5.3 gate, and archive verification. Do not flash, commit, or push.

## UI and resource policy

Stage A uses English status text: the built-in Source Han Sans SC 16 CJK subset does not cover the required fixed Chinese inventory. Dynamic SSIDs and gateway strings are not rendered on the device, preventing unsupported glyphs or exposed credentials. The page displays a status, IP, setup instructions, and top-right battery percentage. HTTP and AP stop only after candidate IP and successful persistence; failed connection/save keeps setup available. Audio/I2S and BLE consume no application allocation in Stage A. Measure free heap, largest block, and stack high-water marks on hardware before Stage B. TODO for Stage B: an idle timeout that closes the temporary setup AP when no client configures the device; in Stage A the AP stays up while the setup screen is open.

## Stage B (approved, minimal WebSocket handshake)

After Wi-Fi obtains an IP and a saved gateway exists, the owner opens a WebSocket client to the saved `ws://host:port[/path]` (no other gateway traffic) and sends:

```json
{"type":"hello","version":1,"transport":"websocket","features":{"mcp":false},"audio_params":{"format":"opus","sample_rate":16000,"channels":1,"frame_duration":60}}
```

It accepts a bounded (`<= 4096` byte) text reply whose top-level `type` is `hello` or `server_hello`; a reply is accepted as a successful handshake only when `version` is `1` and `session_id` is non-empty (otherwise the client retries). The status page distinguishes `Connecting...`, `Ready` (with a truncated session id), and `Retrying...`. Connect and handshake deadlines are 10 s and 5 s; failures retry with deterministic exponential backoff of 1, 2, 4, 8, 16, then 30 s (capped), reset after a successful hello or a Wi-Fi reconnect. Events from Wi-Fi, buttons, HTTP, and the WebSocket client only enqueue; the owner task performs network calls and UI updates, so disconnect and retry never block the UI. Audio send/receive is explicitly out of scope for Stage B, and the gateway is not modified. The client dependency `espressif/esp_websocket_client` (1.8.0) is pinned through `main/idf_component.yml` and `dependencies.lock`.

### Stage B device acceptance (not yet run)

1. Start the gateway on the computer and confirm its status endpoint (`/debug/status`) is reachable from the LAN.
2. Configure the device with that gateway URL (Stage A flow or existing NVS) and let it boot with the saved network.
3. Confirm the screen goes `Connecting...` then `Ready`, and the gateway's `/debug/status` shows the connected session. The gateway log should show `transport=websocket`, `version=1`, `mcp=false`, and opus/16000/mono/60 ms parameters.
4. Stop the gateway: the screen must show `Retrying...` within the handshake timeout and the UI must stay responsive (UP long press opens setup; OK is not blocked). Restart it: backoff must reconnect to `Ready`.
5. Use an unreachable port: retries must stay capped at 30 s without reboot, watchdog, or UI freeze; inspect heap/largest block stability.
6. Cycle Wi-Fi (AP off/on): the device returns to `Retrying...`/`Connecting...` and then `Ready` without a manual restart.
7. Confirm no audio is transmitted or expected: the gateway must not require microphone frames during this stage.

## Stage C (approved, PCM uplink)

Holding OK while the handshake is `Ready` starts one utterance. The owner sends `{"type":"listen","state":"start","mode":"manual","session_id":"..."}` and enables capture; release stops capture, the send task drains the bounded queue, and only then the owner sends `listen.stop`. The device captures 16 kHz, 16-bit, mono PCM and sends one 640-byte binary WebSocket frame per 20 ms. The hello now declares:

```json
{"type":"hello","version":1,"transport":"websocket","features":{"mcp":false},"audio_params":{"format":"pcm","sample_rate":16000,"channels":1,"frame_duration":20}}
```

The gateway enables direct PCM passthrough only for that exact tuple; it forwards valid 640-byte frames to the existing wake gate and bridge without Opus decoding, drops and counts other sizes, and answers with `audio_params` `pcm/24000/1/20`. Opus clients keep the previous path and parameters. Server `audio_params` are ignored by the device.

Capture and send run in two 3 KB tasks behind an 8-frame queue (160 ms). A full queue evicts the oldest frame and increments a counter; network sends never block capture. Audio/I2S is initialized lazily on the first hold (I2S DMA 11,520 B) so Wi-Fi, HTTP, and WebSocket startup do not compete for memory. UI states are `Listening...` while held and `Thinking...` after release; a binary send failure stops capture and shows `Send failed`, and any WebSocket or Wi-Fi loss aborts the utterance and resets the queue immediately. Binary downlink frames are discarded without JSON parsing; playback belongs to Stage D, so the server hello's `pcm/24000/1/20` downlink parameters are advertised but not produced until then. The PCM server hello is sent immediately after registration, before the Gemini cold start, so the firmware's 5 s handshake window is not raced by a roughly 10 s upstream connect; legacy Opus clients keep the previous ordering. Release, stop-completion, and disconnect events are also latched as coalesced owner-polled flags, so a full event queue cannot leave the microphone open. All network writes happen in the owner/send task with a mutex protecting client teardown; button and WebSocket callbacks still only enqueue.

### Stage C device acceptance (not yet run)

1. Boot with a reachable gateway and confirm `Ready`; hold OK and confirm `Listening...` and `listen.start` in the gateway log.
2. Speak, release, and confirm `Thinking...`, a drained binary uplink ending at 640-byte frames, then `listen.stop`. Confirm the gateway labels the connection as PCM and does not run Opus decode.
3. Stop the gateway while holding: the utterance must abort at once, the queue reset, and the UI move to `Retrying...` without a freeze.
4. Disconnect Wi-Fi while holding: same immediate stop; after reconnect, the next hold starts cleanly.
5. Hold for an extended period while the network is slow; confirm bounded memory and a drop counter (no unbounded growth, no UI stall).
6. Confirm no audio is played from the device in this stage and no binary downlink is parsed as JSON.

## Stage D (approved, PCM playback and half-duplex)

PCM downlink is enabled only for a connection that negotiated the exact PCM tuple. The device parses the server hello `audio_params` and requires `format=pcm`, `sample_rate=24000`, `channels=1`, `frame_duration=20`; any mismatch shows `Audio error` and closes the connection without playing or falling back to Opus. Both gateway TTS sources are already 24 kHz PCM16 mono (Gemini Live `on_audio`, and the Grok/Gbot bridge's `response.audio.delta` path, whose configured output rate is 24000), so the gateway forwards them without Opus encoding.

The gateway frames 24 kHz PCM into exactly 960-byte (20 ms) chunks, zero-pads a non-multiple tail, and keeps a bounded 16-frame (~320 ms) queue; a full queue backpressures the output worker without dropping normal reply audio or holding the send lock. Grok and Gemini receive control/VAD independently. For PCM connections, queued and executing output callbacks share gateway-only limits of 5,760,000 PCM bytes (120 seconds) and 4096 callbacks; overflow explicitly aborts the current reply, releases its budget, sends the device a stop, and keeps the connection available for the next turn. Cancellation also wakes blocked frame producers. A drain task paces 20 ms per frame (with at most a 160 ms start burst) instead of dumping a whole reply. `tts.start`/`tts.stop` semantics are unchanged: `start` stops uplink audio, `stop` marks the end of the segment. Opus connections keep the 60 ms codec path and unbounded queue unchanged. The server hello's `pcm/24000/1/20` declaration now matches the bytes actually sent.

On the device, binary WebSocket frames are reassembled across fragments and multi-frame messages into 960-byte frames in a separate 8-frame (~160 ms) queue; a full queue drops the oldest. A dedicated playback task calls `bsp_audio_write` with a shared audio-I/O mutex. Initialization explicitly sets a moderate 50% playback volume; codec reopens retain that value across 16↔24 kHz changes. On `tts.start` it stops capture and discards unsent uplink audio, then switches the codec to 24 kHz/16-bit/mono before the first frame. On `tts.stop` it keeps playing the queued frames; after a 100 ms drain grace (or a 5 s no-audio timeout) it switches back to 16 kHz and returns to `Ready`. The status page shows `Speaking...`; OK presses are ignored while speaking or draining. Disconnect, playback failure, or a format-switch failure clears both queues, restores 16 kHz, and leaves `Retrying...`/`Audio error` rather than a stuck Speaking state. Binary frames never enter the JSON parsers. Text is parsed in the WebSocket callback and the owner receives a self-contained hello/TTS result, so back-to-back `tts.start` and `sentence_start` cannot overwrite each other; malformed non-hello traffic is ignored, and only hello types are validated. An abort or disconnect may still play the roughly 160 ms already accepted into the device queue before the stop/drain completes.

Memory: downlink queue 8 × (960 + 2) B ≈ 7.7 KB, playback task 3 KB, plus two small mutexes; 16↔24 kHz switching reopens the codec and reuses the existing I2S DMA. Allocation remains lazy (first hold or first TTS), and the boot log keeps free heap and largest free block.

### Stage D device acceptance (not yet run)

1. Connect with a gateway returning `pcm/24000/1/20`; confirm `Ready`, then trigger a reply from the gateway.
2. Confirm `Speaking...`, audible speech at the correct pitch/speed, and that no uplink frames are sent while speaking.
3. Hold OK during speech: the press must be ignored and the UI stay `Speaking...`.
4. Trigger a long reply: playback should be continuous and paced, with bounded memory; a network stall must not dump the whole reply at once.
5. After speech, confirm the screen returns to `Ready` and the next hold records at 16 kHz normally.
6. Disconnect Wi-Fi/gateway mid-speech: playback must stop, queues clear, and the UI leave `Speaking...` without a reboot.
7. Point the gateway at a wrong downlink rate/channels/frame duration: the device must show `Audio error` and not play.
8. Confirm Opus clients still receive the existing 60 ms Opus behavior (regression).

## Device acceptance (not yet run)

Test first boot and WPA2 AP access; correct, wrong, and unavailable STA credentials; DHCP timeout; bounded/malformed/interrupted and cross-site forms; new configuration save and reboot; failed save with retry/cancel; old configuration recovery; UP hold and OK cancellation during each setup state; repeat entry/exit and reconnect; HTTP response arrives before AP disappears; missing battery; screen clipping; stable heap/tasks/sockets. Gateway must receive no traffic. No device was flashed for this implementation.

## Stage E (implemented, device-verified 2026-10-19)

- PCM hold-to-talk uses explicit manual turns: release submits buffered speech without trailing silence; empty/too-short input is cleared, duplicate stops are idempotent. Automatic VAD and legacy Opus keep their existing behavior. Grok sessions select `turn_detection: null` before accepting audio; a release sends `input_audio_buffer.commit` then `response.create` over the same ordered WebSocket. Committed acknowledgements never create another response. The 200 ms minimum is a local product threshold, not a documented provider requirement. xAI's public voice guide supports manual commit, but the internal Grok Bot route still requires end-to-end device acceptance.
- UP/DOWN clicks adjust volume by 10 within 0–100; UP long-press retains provisioning without a volume click. The screen shows the value. A separate optional NVS `volume` key defaults to 50, preserving `config_v1`; save after 2 seconds idle, never during capture/playback. No erase or migration is required.
- Wi-Fi and WebSocket recover with bounded retry; OK can retry immediately while offline and never captures before voice readiness. Provisioning remains available.
- Transport connectivity and voice-backend readiness are separate. Backend startup/failure and a bounded response timeout leave an actionable recoverable state instead of endless Thinking. Keep half-duplex playback/capture and stream framing unchanged.
- Retain only bounded counters, heap/largest-block and task stack diagnostics; no credentials or private transcripts in added logs. No UI beautification, dependency installation, flashing, commits, pushes, or restarting the active gateway during implementation.
- Validate manual/automatic turn boundaries, empty/short/duplicate stops, volume compatibility/persistence, retry and backend recovery, and a new utterance after playback. Run static/firmware/archive gates and gateway tests, then independent review before device testing.

## Stage G diagnostic repair (implemented, device validation pending)

The owner reads the previous abnormal restart record before writing this boot's
abnormal reason to the separate NVS `diag` key. Reading or hiding never consumes
that record: each boot continues to display the last stored abnormal record
until a key hides it for the current boot (setup keeps its provisioning details).
A diagnostic write failure leaves saved network configuration and volume usable.
The voltage is sampled during the boot that writes the record, not at the instant
of the crash.

Diagnostics occupy exactly two short lines in the 204 × 34 detail area, for
example `Restart: interrupt WDT` and `Boot battery: 3310 mV`, or
`Boot battery: unknown` when unavailable. The first key refreshes only details
under the LVGL lock before any key-action early return; existing status, hints,
and key actions retain their behavior. Host doubles exercise the actual key and
boot code, read-before-write ordering, subsequent reads, and open/set/commit
failures. A layout test uses the installed Montserrat 14 advances and kerning for
all uint16_t reason values and voltage boundaries; without existing Managed
Components this font check is explicitly skipped.

Device validation remains pending: confirm both diagnostic lines are fully
visible, the first key restores details even on ignored/release paths without
changing status or hints, setup remains readable, and history survives ordinary
restarts while configuration still loads. No device was flashed for this repair.

## Stage F (implemented, device-verified 2026-10-19)

A state-driven avatar drawn from five plain LVGL objects (two eyes, two
pupils, one mouth) inside a fixed box between the status line and the detail
lines. No images, no canvas, no extra frame buffer: only those objects are
invalidated, so rendering stays inside the existing 40-line partial buffer.
The English status line remains, so the avatar never hides a state.

Animation decisions live in `main/passport_face.c`, a pure module with no LVGL
or audio includes, covered by `tests/test_passport_face.c`. The app maps its
0–100 outputs onto object geometry. A 100 ms LVGL timer (priority 4, below
every audio task at 6) drives it; the play task publishes a per-frame PCM peak
to one atomic that the timer reads, so the audio path takes no extra lock.

Mapping: Ready/Starting/Connecting blink; Listening opens the eyes wide;
Thinking sweeps the pupils; Speaking opens the mouth with the playback level
(immediate attack, ~500 ms decay); Retrying sweeps slowly; Audio error, Send
failed, Voice unavailable and the setup/storage errors droop the eyes. The
avatar hides while the setup screen is open.

The gateway's Grok PCM path sends no expression messages, so the face follows
device state only. The gateway can emit `{"type":"llm","emotion":...}` for MCP
clients, but the firmware ignores that type today.

### Stage F device acceptance (not yet run)

1. Boot and confirm the face appears under the status line, blinks at Ready,
   and the status text stays readable.
2. Hold OK: eyes widen at once; release: pupils sweep during Thinking.
3. On a reply, the mouth opens and closes with the speech and the audio stays
   continuous; hold a reply longer than 30 s and confirm no stutter.
4. Stop the gateway: the face droops into the error look at Retrying/Audio
   error and returns to blinking once Ready again.
5. Hold UP for setup: the avatar hides and the password stays readable; cancel
   and confirm the avatar returns.
6. Compare the boot `heap=`/`largest=` log with Stage E (121348/110592) and
   confirm the drop stays small.

## Stage H (implemented 2026-10-20, device tests pending)

Proactive announcements: a loopback-only HTTP endpoint on the gateway lets
the computer make the device speak a short notice while it is idle. This is
the base layer for the later reminder and quick-note stages.

Gateway queues at most 3 notices of up to 100 characters, expires waiting jobs
at 60 s (also during submission), and spaces deliveries by 2 s after device
completion. It requires a connected cloud proxy. Scheduling no longer reads
DebugStatus timestamps or assumes that a reply finishes after 10 s.

Each `notify.start` carries a positive `id` and `flash` flag. The device owner
returns `notify_ack` with the same ID and `busy` or `granted`. It grants only
outside setup, recording, pending replies and playback, after 1.5 s idle.
No announcement text is sent to firmware. TTS start/stop carry the lease ID;
stale segments are rejected. `tts.stop` only starts draining. Device PLAY_DONE
returns `done` and clears the amber `Notice...` marker; cloud completion alone
does neither. OK cancels both preparation and playback. The optional status
blink lasts about 2.5 s and always restores visibility, including setup.

The entire granted job is limited to 30 s on both sides. Cancellation stops the
real cloud response and paced frames. A response created after cancellation is
suppressed by ID; an unacknowledged response creation retires the cloud session
and uses the existing session-error recovery path rather than occupying the
next conversation. Voice errors and disconnects clear the device lease.

`POST /notify` remains bearer-authenticated and bound only to `127.0.0.1`
(default port 8767). HTTP 202 / `queued` means acceptance only. Authenticated
`GET /notify/{id}` returns `queued`, `playing`, `done`, `cancelled`, `expired`
or `failed`; the latest 128 metadata records are retained in memory, without
text. `gateway/tools/notify "Task finished" --flash --wait` waits for the
outcome and exits nonzero on failure. Local stdio MCP `notify` uses the same
queue. LAN WS `gemini_say` is rejected; Opus `say`/`speak` refuse PCM devices.
Errors log fixed codes or exception types, never announcement/error contents.

### Stage H device acceptance (not yet run)

1. With the device idle, run `gateway/tools/notify "Task finished" --flash`:
   the notice plays, the screen shows `Notice...` (amber, blinking), then
   returns to Ready.
2. Notify while holding OK or during a reply: it waits and plays after the
   conversation; an expired item (60 s) is dropped.
3. Notify with the device off or the voice session down: the CLI reports a
   clear HTTP 503 error, not silent success.
4. OK during a notice: stops it immediately with no leftover `Notice...`.
5. 120-character text is rejected with 413; five rapid notifies fill the
   3-slot queue and the rest get 429.
