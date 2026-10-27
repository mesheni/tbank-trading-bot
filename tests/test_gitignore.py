"""Гигиена репозитория: секреты и артефакты не попадают в git, шаблон без токена."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gitignore_patterns() -> set[str]:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}


def test_gitignore_covers_secrets_and_artifacts():
    patterns = _gitignore_patterns()
    for expected in (".env", "*.key", "*.p12", "*.pfx", "*.pem", "data/", "reports/"):
        assert expected in patterns, f"в .gitignore нет шаблона {expected}"


def test_env_example_has_no_token_value():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert not [line for line in lines if re.match(r"^T_INVEST_TOKEN=\S", line)]
