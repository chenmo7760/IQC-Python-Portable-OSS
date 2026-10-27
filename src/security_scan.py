"""Read-only scan for accidentally embedded connection credentials."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_TEXT_SUFFIXES = {".py", ".md", ".json", ".sql", ".bat", ".cmd", ".txt", ".tsv", ".log"}
_PLACEHOLDER = r"(?:\*{3}REDACTED\*{3}|%[^%\r\n]+%|\$\{[^}\r\n]+\})"
_PLACEHOLDER_TOKEN = rf'''(?:"{_PLACEHOLDER}"|'{_PLACEHOLDER}'|{_PLACEHOLDER})'''
_SECRET_TOKEN = rf'''(?!(?:{_PLACEHOLDER_TOKEN}))(?:"[^"]*"|'[^']*'|[^\s"']+)'''
_SECRET_PATTERNS = (
    (
        "MYSQL_PASSWORD_ASSIGNMENT",
        re.compile(rf"(?i)\bMYSQL_(?:PASSWORD|PWD)\s*=\s*{_SECRET_TOKEN}"),
    ),
    (
        "MYSQL_CLI_PASSWORD_ARGUMENT",
        re.compile(rf"(?i)--password(?:=|\s+){_SECRET_TOKEN}"),
    ),
)


@dataclass(frozen=True)
class CredentialFinding:
    path: Path
    kind: str
    line_number: int


def scan_paths(paths: Iterable[Path], *, excluded: Iterable[Path] = ()) -> tuple[CredentialFinding, ...]:
    """Scan text artifacts without returning matched secret values."""

    excluded_paths = {Path(path).resolve() for path in excluded}
    candidates: set[Path] = set()
    for value in paths:
        path = Path(value)
        if path.is_dir():
            candidates.update(item for item in path.rglob("*") if item.is_file())
        elif path.is_file():
            candidates.add(path)

    findings: list[CredentialFinding] = []
    for path in sorted(candidates):
        resolved = path.resolve()
        if resolved in excluded_paths or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            for kind, pattern in _SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(CredentialFinding(resolved, kind, line_number))
    return tuple(findings)


def assert_no_credentials(paths: Iterable[Path], *, excluded: Iterable[Path] = ()) -> None:
    findings = scan_paths(paths, excluded=excluded)
    if findings:
        summary = ", ".join(f"{item.path}:{item.line_number}:{item.kind}" for item in findings)
        raise ValueError(f"发现未脱敏连接凭据：{summary}")
