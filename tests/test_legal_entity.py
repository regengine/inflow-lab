"""The copyright holder is RegEngine LLC. SEL-322.

RegEngine is a California single-member LLC. These checks fail if NOTICE
or any other tracked text file names the company as a corporation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Company-name variants only. Case-sensitive so "including" is not a hit.
# Pieces stay split so this file does not contain the phrase it rejects.
FORBIDDEN = re.compile(
    "RegEngine" + r",?\s+" + "Inc" + r"\.?"
    + "|"
    + "REGENGINE" + r",?\s+" + "INC" + r"\.?"
    + "|"
    + r"(?:RegEngine|REGENGINE),?\s+" + "INC" + r"\.'S"
)

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def test_notice_names_regengine_llc():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Copyright 2024-2026 RegEngine LLC" in notice.splitlines()
    assert "RegEngine" + " Inc" not in notice


def test_possessive_pattern_only_matches_regengine():
    assert FORBIDDEN.search("ACME " + "INC." + "'S") is None
    assert FORBIDDEN.fullmatch("REGENGINE " + "INC." + "'S") is not None
    assert FORBIDDEN.fullmatch("RegEngine " + "INC." + "'S") is not None


def _tracked_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    files: list[Path] = []
    for name in listed.stdout.decode("utf-8").split("\0"):
        if not name:
            continue
        path = ROOT / name
        if path.is_file() and not any(part in SKIP_DIRS for part in Path(name).parts):
            files.append(path)
    return files


def test_repo_does_not_name_the_company_as_inc():
    hits: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert hits == []
