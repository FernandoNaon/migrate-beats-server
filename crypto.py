"""
Symmetric encryption for provider refresh tokens at rest.

Uses Fernet (AES-128-CBC + HMAC) from the `cryptography` package.
The key is read from the FERNET_KEY env var.

Generate a key once with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Set it as FERNET_KEY locally (.env) and on Railway. If the key changes, previously
encrypted tokens become undecryptable — users would just need to reconnect Spotify.
"""
from __future__ import annotations

import os
from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    key = os.environ.get("FERNET_KEY")
    if not key:
        # Fail loudly — running without a stable key means tokens encrypted now
        # can't be decrypted after a restart, which silently breaks auth.
        raise RuntimeError(
            "FERNET_KEY env var is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and set it in your environment."
        )
    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string, returning a urlsafe token string."""
    if plaintext is None:
        return None
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str | None:
    """Decrypt a token string back to plaintext. Returns None if invalid/undecryptable."""
    if not token:
        return None
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
