#!/usr/bin/env python3
"""Scan shipped text for credentials and maintainer-local identifiers."""

from __future__ import annotations

import re
from pathlib import Path

TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".mjs", ".py", ".sh", ".txt", ".toml", ".yaml", ".yml"}
PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"),
    "Home Assistant token": re.compile(r"\b[A-Za-z0-9_-]{80,}\.[A-Za-z0-9_-]{20,}\b"),
    "password assignment": re.compile(
        r"(?i)\b(?:password|passwd|api[_-]?token)\s*[=:]\s*['\"]?(?!<|\{|example|redacted|replace|token\b)[^\s'\"]{8,}"
    ),
    "local home path": re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "private workspace path": re.compile(r"(?:^|[/\\])(?:private-workspace|maintainer-workspace)(?:[/\\])"),
    "private IPv4 address": re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    ),
}
ALLOWED_EXACT_MATCHES = {
    "password assignment": {
        'api_token="first-token',
        'api_token="second-token',
        "password = ftp.get(",
        r"Password=secret\ndifficulty=1\n",
        r"Password=[redacted]\ndifficulty=1\n",
        "password=[redacted]",
        'password="different',
        r"Password=old-secret\ndifficulty=1\n",
        r"Password=old-secret\ndifficulty=2\n",
        r"Password=backup-secret\ndifficulty=2\n",
        r"Password=external\ndifficulty=1\n",
        r"Password=external\ndifficulty=3\n",
        r"Password=external\ndifficulty=9\n",
        r"Password=someone-else\ndifficulty=3\n",
        r"Password=changed\ndifficulty=2\n",
        r"Password=new-secret\ndifficulty=2\n",
        r"Password=second\ndifficulty=3\n",
        "password=do-not-leak",
        'ApiToken="nested-leak',
        'ApiToken="outside-token',
    },
    "local home path": {"/home/account/"},
    "private IPv4 address": {"192.168.1.10"},
}


def scan_tree(root: Path) -> list[str]:
    """Return deterministic findings for every shipped text file."""

    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in PATTERNS.items():
            allowed = ALLOWED_EXACT_MATCHES.get(label, set())
            for match in pattern.finditer(text):
                if match.group(0) not in allowed:
                    findings.append(f"{path.relative_to(root).as_posix()}: likely {label}")
    return findings


__all__ = ("ALLOWED_EXACT_MATCHES", "PATTERNS", "TEXT_SUFFIXES", "scan_tree")
