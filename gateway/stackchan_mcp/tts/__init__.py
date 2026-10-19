"""TTS framework for Phase 4 (Issue #70).

This package provides the engine-agnostic skeleton for the gateway-side
``say(text)`` MCP tool plus concrete Qwen3-TTS and VOICEVOX engines.

The package exports :class:`TTSEngine`, an :class:`EngineRegistry`, the
:func:`synthesize_and_send` orchestrator, and registers the default
Qwen3-TTS engine plus the VOICEVOX fallback at import time. Engines
whose modules require optional extras to import are registered behind
``try / except ImportError`` so the framework still works when the
corresponding extra is missing.
"""

from __future__ import annotations

import logging
from typing import Callable

from .base import EngineRegistry, TTSEngine, get_registry
from .orchestrator import DEFAULT_VOICE, synthesize_and_send

_logger = logging.getLogger(__name__)


def _try_register(register_fn: Callable[[], None], engine_label: str) -> None:
    """Run ``register_fn`` and swallow ImportErrors.

    Used so an engine whose top-level module needs an optional extra
    (e.g. PR3's Irodori importing torch / transformers) can fail to
    register cleanly without breaking the rest of the framework. The
    VOICEVOX engine module itself imports fine without any extras —
    httpx is only imported inside :meth:`VoicevoxEngine.synthesize`.
    """
    try:
        register_fn()
    except ImportError as exc:
        _logger.debug("Skipping %s engine registration: %s", engine_label, exc)


def _register_qwen3() -> None:
    from .qwen3 import Qwen3Engine

    get_registry().register(Qwen3Engine())


def _register_voicevox() -> None:
    from .voicevox import VoicevoxEngine

    get_registry().register(VoicevoxEngine())


_try_register(_register_qwen3, "qwen3")
_try_register(_register_voicevox, "voicevox")


__all__ = [
    "DEFAULT_VOICE",
    "EngineRegistry",
    "TTSEngine",
    "get_registry",
    "synthesize_and_send",
]
