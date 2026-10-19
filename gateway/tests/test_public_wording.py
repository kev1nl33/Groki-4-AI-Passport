# SPDX-FileCopyrightText: 2026 sefuzhou770801-hub
# SPDX-License-Identifier: MIT
"""Keep private names and development-time wording out of tracked files.

The list of private words (real names, internal nicknames and project names)
is not part of the repository. When it exists locally, the scan below checks
every tracked text file against it:

- the file named by the environment variable GROKI_PRIVATE_WORDLIST, or
- gateway/tests/private_wordlist.txt (ignored by git).

Format: one word or phrase per line, UTF-8. Blank lines and lines starting
with "#" are ignored. Matching is case-insensitive; entries made only of
ASCII letters, digits and underscores match whole words, everything else
matches anywhere in a line.

Without the file only the generic words in PUBLIC_WORDS are checked, and the
matching itself is tested with made-up words.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIVATE_WORDLIST = Path(__file__).with_name("private_wordlist.txt")

# Generic development-time phrasing that has no place in public prompts,
# spoken lines, docs or comments.
PUBLIC_WORDS = ("boss", "tonight", "in-room demo")

# Files that must name the words in order to check for them, vendored code,
# and minified bundles (random base64 can contain any letters).
SKIP_PREFIXES = ("third_party/",)
SKIP_FILES = {
    "gateway/tests/test_public_wording.py",
    "docs/esptool.js",
    "tools/remote-flasher/web/esptool-bundle.js",
}

_WORD_ONLY = re.compile(r"\w+", re.ASCII)


def load_wordlist(path: Path) -> list[str]:
    words = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            words.append(entry)
    return words


def compile_words(words: Iterable[str]) -> re.Pattern[str] | None:
    parts = []
    for word in words:
        escaped = re.escape(word)
        parts.append(rf"\b{escaped}\b" if _WORD_ONLY.fullmatch(word) else escaped)
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


def find_hits(pattern: re.Pattern[str], files: Iterable[Path], root: Path) -> list[str]:
    hits = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            match = pattern.search(line)
            if match:
                hits.append(f"{path.relative_to(root)}:{lineno}: {match.group(0)}")
    return hits


def _private_wordlist_path() -> Path | None:
    configured = os.getenv("GROKI_PRIVATE_WORDLIST", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            pytest.fail(f"GROKI_PRIVATE_WORDLIST points to a missing file: {path}")
        return path
    return DEFAULT_PRIVATE_WORDLIST if DEFAULT_PRIVATE_WORDLIST.is_file() else None


def _tracked_text_files() -> list[Path]:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("needs a git checkout")
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.decode()
    files = []
    for rel in filter(None, out.split("\0")):
        if rel in SKIP_FILES or rel.startswith(SKIP_PREFIXES):
            continue
        path = REPO_ROOT / rel
        if path.is_file():
            files.append(path)
    return files


def test_matching_finds_whole_words_and_phrases(tmp_path):
    sample = tmp_path / "sample.md"
    sample.write_text(
        "Zorblax wrote this line.\n"
        "zorblaxian is a longer word.\n"
        "Built by 测试名 for the demo.\n"
        "Nothing to see here.\n",
        encoding="utf-8",
    )
    wordlist = tmp_path / "words.txt"
    wordlist.write_text("# made-up test words\n\nzorblax\n测试名\n", encoding="utf-8")

    pattern = compile_words(load_wordlist(wordlist))
    assert pattern is not None
    assert find_hits(pattern, [sample], tmp_path) == [
        "sample.md:1: Zorblax",
        "sample.md:3: 测试名",
    ]


def test_empty_wordlist_checks_nothing(tmp_path):
    wordlist = tmp_path / "words.txt"
    wordlist.write_text("# only a comment\n", encoding="utf-8")
    assert compile_words(load_wordlist(wordlist)) is None


def test_no_generic_dev_era_wording_in_tracked_files():
    pattern = compile_words(PUBLIC_WORDS)
    assert pattern is not None
    assert find_hits(pattern, _tracked_text_files(), REPO_ROOT) == []


def test_no_private_words_in_tracked_files():
    path = _private_wordlist_path()
    if path is None:
        pytest.skip("no private word list (set GROKI_PRIVATE_WORDLIST or add tests/private_wordlist.txt)")
    pattern = compile_words(load_wordlist(path))
    if pattern is None:
        pytest.skip(f"private word list {path} is empty")
    assert find_hits(pattern, _tracked_text_files(), REPO_ROOT) == []
