# Notices

This repository combines works under the MIT License. Each component keeps its
own license file and upstream copyright notices.

## `firmware/`

Derived from [FoloToy/ai-passport](https://github.com/FoloToy/ai-passport)
(MIT; license kept at `firmware/LICENSE`), baseline commit `3d685a9`, with the
Passport Voice application added: SoftAP provisioning, NVS configuration, the
status page, WebSocket handshake and reconnection, 16 kHz PCM uplink, 24 kHz
PCM playback, manual turns, and volume persistence (`main/passport_*`,
`tests/test_passport_*`, and related build/doc/test updates).

## `gateway/`

Derived from the gateway of
[JosssphZhou/groki-bot](https://github.com/JosssphZhou/groki-bot) (MIT;
license kept at `gateway/LICENSE`), which itself derives from
[kisaragi-mochi/stackchan-mcp](https://github.com/kisaragi-mochi/stackchan-mcp).
Changes in this copy: an explicitly negotiated PCM passthrough mode
(`pcm/16000/1/20` uplink, `pcm/24000/1/20` downlink), manual turn commit for
Grok sessions, frame pacing and backpressure, per-reply output budgets, and
tests covering them; plus small layout adaptations so the gateway tests run
from this standalone repository (documented in the adapted test files).

New files carry `SPDX-License-Identifier: MIT` headers following the upstream
convention. No Emotion Ball data is included in this repository.

## Repository-level material

The root `README.md`, `README.zh-CN.md`, `DELIVERY.md`, `NOTICE.md`, and
`tools/apple-cc` are original to this repository and are covered by the MIT
License at the root `LICENSE`.
