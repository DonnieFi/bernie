"""Shared paths for one-time Google OAuth on the host (not inside Docker)."""
from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def credentials_dir() -> Path:
    raw = os.environ.get("CREDENTIALS_DIR")
    if raw:
        return Path(raw)
    return repo_root() / "credentials"


def client_secrets_file() -> Path:
    raw = os.environ.get("GOOGLE_CREDENTIALS_FILE")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else credentials_dir() / p.name
    return credentials_dir() / "credentials.json"


def calendar_token_file() -> Path:
    raw = os.environ.get("GOOGLE_TOKEN_FILE")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else credentials_dir() / p.name
    return credentials_dir() / "token.json"


def gmail_token_file() -> Path:
    raw = os.environ.get("GMAIL_TOKEN_FILE")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else credentials_dir() / p.name
    return credentials_dir() / "gmail_token.json"
