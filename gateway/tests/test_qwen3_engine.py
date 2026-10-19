"""Tests for the Qwen3-TTS engine HTTP client."""

from __future__ import annotations

import array
from collections.abc import AsyncIterator

import pytest

httpx = pytest.importorskip("httpx")

from stackchan_mcp.tts.qwen3 import (  # noqa: E402  (import after importorskip)
    DEFAULT_QWEN3_TTS_URL,
    Qwen3Engine,
)


class _AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


def _pcm24(duration_ms: int = 60) -> bytes:
    samples = array.array("h", [i * 10 for i in range(24000 * duration_ms // 1000)])
    return samples.tobytes()


async def _collect_pcm(chunks: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in chunks])


def test_engine_name_is_qwen3():
    engine = Qwen3Engine()
    assert engine.name == "qwen3"


def test_default_url_matches_resident_service():
    assert DEFAULT_QWEN3_TTS_URL == "http://127.0.0.1:8100"


def test_url_param_strips_trailing_slash():
    engine = Qwen3Engine(url="http://test.local:8100/")
    assert engine.url == "http://test.local:8100"


def test_default_registry_includes_qwen3():
    from stackchan_mcp.tts import get_registry

    assert "qwen3" in get_registry().names()


@pytest.mark.asyncio
async def test_synthesize_posts_text_to_tts_and_resamples_to_device_rate():
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "json": request.read().decode(),
            }
        )
        return httpx.Response(200, content=_pcm24())

    transport = httpx.MockTransport(handler)
    engine = Qwen3Engine(url="http://test.local:8100", transport=transport)

    pcm = await _collect_pcm(engine.synthesize("你好"))

    assert captured == [
        {
            "method": "POST",
            "path": "/tts",
            "json": '{"text":"你好"}',
        }
    ]
    decoded = array.array("h")
    decoded.frombytes(pcm)
    assert 950 <= len(decoded) <= 970


@pytest.mark.asyncio
async def test_qwen3_streaming_yields_pcm_chunks_as_they_arrive():
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            200,
            stream=_AsyncChunks([_pcm24(60), _pcm24(60)]),
        )

    engine = Qwen3Engine(transport=httpx.MockTransport(handler))

    chunks = [chunk async for chunk in engine.synthesize("你好")]

    assert len(chunks) == 2
    decoded = array.array("h")
    decoded.frombytes(b"".join(chunks))
    assert 1900 <= len(decoded) <= 1940


@pytest.mark.asyncio
async def test_synthesize_rejects_empty_text_before_http_call():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, content=_pcm24())

    engine = Qwen3Engine(transport=httpx.MockTransport(handler))

    with pytest.raises(ValueError, match="text"):
        await _collect_pcm(engine.synthesize("   "))

    assert captured == []


@pytest.mark.asyncio
async def test_synthesize_propagates_http_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, text="Qwen3 busy")

    engine = Qwen3Engine(transport=httpx.MockTransport(handler))

    with pytest.raises(httpx.HTTPStatusError):
        await _collect_pcm(engine.synthesize("hello"))


@pytest.mark.asyncio
async def test_synthesize_rejects_empty_pcm_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    engine = Qwen3Engine(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="empty PCM"):
        await _collect_pcm(engine.synthesize("hello"))
