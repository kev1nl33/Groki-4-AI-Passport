# SPDX-FileCopyrightText: 2026 sefuzhou770801-hub
# SPDX-License-Identifier: MIT
"""Checks on the examples in the public documentation.

The Grok Bot hand-off examples use the neutral agent name "assistant" (the
Chinese documents translate it as 助手) or a placeholder such as
<agent name>, in English for the English documents.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# This repository ships the gateway standalone (with the AI Passport firmware
# in ../firmware) instead of inside the full Groki Bot checkout, so the doc
# lists cover the repository README plus the gateway README rather than the
# upstream repository-root docs.
ENGLISH_DOCS = ["README.md", "gateway/README.md"]
CHINESE_DOCS = ["README.zh-CN.md", "gateway/README.zh-CN.md"]

TOOL_BOT_EXAMPLE = re.compile(r"STACKCHAN_TOOL_BOT=([^\s`]+)")
BOT_CREATE_EXAMPLE = re.compile(r"gbot bots create --name ([^\s`]+)")
TARGET_EXAMPLE = re.compile(r'"target": "([^"]+)"')

CJK = re.compile(r"[㐀-鿿]")


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


EXAMPLE_AGENT_NAMES = {"assistant", "助手"}


def _agent_examples(text: str) -> list[str]:
    return (
        TOOL_BOT_EXAMPLE.findall(text)
        + BOT_CREATE_EXAMPLE.findall(text)
        + TARGET_EXAMPLE.findall(text)
    )


@pytest.mark.parametrize("rel", ENGLISH_DOCS + CHINESE_DOCS)
def test_docs_use_the_agreed_example_agent_name(rel):
    for name in _agent_examples(_read(rel)):
        assert name in EXAMPLE_AGENT_NAMES or name.startswith("<"), f"{rel}: example agent name {name!r}"


@pytest.mark.parametrize("rel", ENGLISH_DOCS)
def test_english_docs_use_english_agent_examples(rel):
    text = _read(rel)
    for name in _agent_examples(text):
        assert not CJK.search(name), f"{rel}: example agent name {name!r}"
    # Example conversations use English. The code's own Chinese templates may
    # still be quoted, but only with the {agent} placeholder, never a name.
    assert not re.search(r"已经发给[^{]", text)
    assert not re.search(r"[^}]回话了", text)


def test_english_readmes_show_an_agent_name_example():
    for rel in ("README.md", "gateway/README.md"):
        assert set(TOOL_BOT_EXAMPLE.findall(_read(rel))) == {"assistant"}, rel
