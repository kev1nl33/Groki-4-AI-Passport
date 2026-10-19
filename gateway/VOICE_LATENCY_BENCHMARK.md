<!-- SPDX-FileCopyrightText: 2026 Joseph Zhou -->
<!-- SPDX-License-Identifier: MIT -->
# Voice latency benchmark

This benchmark compares Gemini and signed-in Grok Bot voice with the same English
synthetic WAV fixtures, neutral instructions, server VAD, disabled tools and no
local wake gate. It uses real 16 kHz Opus input and a fake device that decodes real
24 kHz Opus output. It does not open the robot's serial port or start the full gateway.

```sh
cd gateway
VOICE_EVIDENCE="$(mktemp -d)"
STACKCHAN_USB_DISABLE=1 STACKCHAN_GBOT_VOICE_BOT_ID="YOUR_AGENT_ID" \
  uv run --extra tts --extra gemini-live python scripts/benchmark_voice_latency.py \
  --backends gbot,gemini --prompt-mode neutral --warm-rounds 2 \
  --input-wav fixtures/count.wav --interrupt-wav fixtures/interrupt.wav \
  --require-interruption --max-initial-ttfa-ms 2500 \
  --max-interrupt-ttfa-ms 2500 --output-dir "$VOICE_EVIDENCE"
```

Start the login helper as described in [setup](GBOT_VOICE_VALIDATION.md).
Supply Gemini credentials through the current environment or a local environment
file. Credentials remain in memory. Each backend runs serially; `--warm-rounds 2`
means one cold round and two further rounds in the same session.

The script writes local WAV output, per-event JSON, and an atomic timing-only
`summary.partial.json` after each round. Test transcripts are included in the
final local summary; full session instructions and credentials are excluded.

## Measurement boundaries

Fixture speech duration is measured separately from response and interruption
latency. The clock records audible PCM sample boundaries on the realtime packet
clock, the last speech packet's completed send, per-frame/upstream send times and
schedule drift. Server events are recorded on handler entry after JSON decoding;
this is explicitly not a raw socket-receive timestamp. Handler and audio callback
execution times are separate measurements.

Grok Bot exposes speech-start/stop events. Gemini's available `interrupted` event
is used as its observable interruption signal, not mislabeled as raw VAD onset.
Detection may happen before an utterance ends, yielding a negative
end-of-utterance-to-detection difference. First reply PCM and the first subsequent
Opus frame are measured separately. Opus frames have no response identifier, so
an uncleared old FIFO can invalidate attribution; clearing is checked separately.

Missing timings, unfinished rounds, failed connection or a missing output phrase
“interruption successful” return nonzero. Fragmented Gemini transcripts are joined
per round, excluding input transcripts. `--require-interruption` also requires
observable interruption, local stop and queue-clearing evidence. Timing guards
are diagnostic thresholds, not proof of parity with another provider.

`--prompt-mode native` keeps each provider's original instructions and records
only character count and SHA256. Native harness instructions may change between
calls, so matching length alone is not a byte-identical prompt control.
`--input-transport pcm` bypasses only input Opus encoding/decoding for a diagnostic
comparison; output still uses the real proxy. Default input transport is Opus.

## Small-sample observations

Earlier real cloud tests used three rounds per provider: one cold round and two
warm samples. The table reports warm medians from the validated default profile.
These measurements came from the earlier implementation baseline; the later port
to current public main was checked offline and was not deployed to a robot.

| Stage | Grok Bot | Gemini |
| --- | ---: | ---: |
| Last speech packet to first reply Opus | 1373 ms | 1298 ms |
| Interruption waveform onset to local stop | 1130 ms | 504 ms |
| Received interruption signal to local stop | 0.159 ms | 0.092 ms |
| Interruption utterance end to new reply Opus | 1371 ms | 1719 ms |

Initial Grok Bot first audio did not improve relative to its earlier 1277 ms warm
median. A frozen neutral baseline exceeded the 2500 ms diagnostic guard for the
new reply after interruption, at roughly 3.78–3.92 seconds to first PCM. Removing
receiver waits on audio callbacks/playback completion fixed a reproducible local
blocking defect. Gemini's earlier path could wait about 10.94 seconds for natural
playback completion because `interrupted` did not reach the proxy abort path.
Deterministic blocked-sink tests verify the corrected receive/control boundary.

An earlier isolated slow observation was not stably reproduced with either native
instructions or direct PCM. It is not attributed to prompt length or input Opus,
and no fixed-factor latency improvement is claimed. The remaining default-profile
interruption gap is mainly before the upstream signal arrives. One cold-connection
sample changed from 3608 to 3005 ms with concurrent credential/session requests;
that is not a statistical startup-performance conclusion.

A separate, matching-echo `reasoning.effort=none` experiment produced warm medians
of 1349 ms initial Opus, 1171 ms interruption stop and 1348 ms new reply Opus.
The roughly 20–40 ms changes are not convincing evidence of a useful gain, and
interruption was slower. Production reasoning defaults remain unchanged.
The benchmark's `--gbot-reasoning-effort default|none|high` changes only a session
experiment and fails immediately without a matching server echo.

A VAD threshold candidate was acknowledged without echo, so effectiveness was not
confirmed. A 250 ms silence setting was echoed at handshake only; cutoff and
multi-turn reliability were not tested. Neither candidate is adopted, and the
250 ms experiment was canceled after the existing profile was accepted.

## Offline validation

The current-main port passed 251 Python voice tests, three Node helper tests and
Ruff for the affected Python files. The earlier implementation's corresponding
suite passed 212 tests; these are different code baselines, not repeated hardware
acceptance. No firmware changes or device reconfiguration are part of this work.
The synthetic-audio measurements are gateway observations, not acoustic or
long-duration robot reliability measurements.
