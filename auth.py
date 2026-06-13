"""
Session management + per-user Spotify client.

Replaces the old "send the OAuth code on every request" pattern:
- Exchange the OAuth code ONCE at login, store tokens in the DB.
- Issue an opaque session token (stored hashed) to the client.
- On each request, resolve session -> user -> a Spotify client, refreshing the
  access token from the stored refresh token when it has expired.

This is multi-instance safe (no filesystem token cache) and revocable.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import MemoryCacheHandler

from models import db, User, UserIdentity, Session
from crypto import encrypt, decrypt

SESSION_TTL_DAYS = 30
ACCESS_TOKEN_REFRESH_MARGIN_S = 60  # refresh if within this many seconds of expiry


# ==================== SESSION TOKENS ====================

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(user_id: str) -> str:
    """Create a session for a user and return the RAW token (shown to client once)."""
    raw = secrets.token_urlsafe(32)
    sess = Session(
        token_hash=_hash_token(raw),
        user_id=user_id,
        expires_at=datetime.utcnow() + timedelta(days=SESSION_TTL_DAYS),
    )
    db.session.add(sess)
    db.session.commit()
    return raw


def resolve_session(raw_token: str | None) -> User | None:
    """Return the User for a raw session token, or None if invalid/expired."""
    if not raw_token:
        return None
    sess = Session.query.get(_hash_token(raw_token))
    if not sess:
        return None
    if sess.expires_at and sess.expires_at < datetime.utcnow():
        # Expired — clean it up.
        db.session.delete(sess)
        db.session.commit()
        return None
    sess.last_used_at = datetime.utcnow()
    db.session.commit()
    return User.query.get(sess.user_id)


def revoke_session(raw_token: str | None) -> bool:
    if not raw_token:
        return False
    sess = Session.query.get(_hash_token(raw_token))
    if not sess:
        return False
    db.session.delete(sess)
    db.session.commit()
    return True


# ==================== SPOTIFY CLIENT (per user) ====================

def _oauth(client_id, client_secret, redirect_uri, scope) -> SpotifyOAuth:
    # MemoryCacheHandler => no filesystem token cache (the old cross-user leak vector).
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
        cache_handler=MemoryCacheHandler(),
    )


def exchange_code_and_store(code, *, client_id, client_secret, redirect_uri, scope):
    """Exchange an OAuth code once, fetch the profile, upsert the identity with tokens.

    Returns (user, spotify_client, token_info).
    """
    from models import get_or_create_user

    oauth = _oauth(client_id, client_secret, redirect_uri, scope)
    token_info = oauth.get_access_token(code, as_dict=True, check_cache=False)
    sp = spotipy.Spotify(auth=token_info["access_token"])
    me = sp.current_user()

    user, _is_new = get_or_create_user(
        spotify_user_id=me["id"],
        email=me.get("email"),
        display_name=me.get("display_name", me["id"]),
        avatar_url=me["images"][0]["url"] if me.get("images") else None,
    )

    identity = UserIdentity.query.filter_by(provider="spotify", provider_user_id=me["id"]).first()
    if identity:
        identity.access_token = token_info["access_token"]
        if token_info.get("refresh_token"):
            identity.refresh_token = encrypt(token_info["refresh_token"])
        identity.token_expires_at = datetime.utcfromtimestamp(token_info["expires_at"])
        identity.token_scope = token_info.get("scope")
        db.session.commit()

    return user, sp, token_info


def get_spotify_client_for_user(user, *, client_id, client_secret, redirect_uri, scope):
    """Return an authenticated spotipy client for a user, refreshing the token if needed."""
    identity = UserIdentity.query.filter_by(user_id=user.id, provider="spotify").first()
    if not identity or not identity.refresh_token:
        raise RuntimeError("No Spotify identity on file — user must reconnect Spotify.")

    now = datetime.utcnow()
    fresh = (
        identity.access_token
        and identity.token_expires_at
        and now < identity.token_expires_at - timedelta(seconds=ACCESS_TOKEN_REFRESH_MARGIN_S)
    )
    if fresh:
        return spotipy.Spotify(auth=identity.access_token)

    # Refresh using the stored (decrypted) refresh token.
    oauth = _oauth(client_id, client_secret, redirect_uri, scope)
    refresh = decrypt(identity.refresh_token)
    if not refresh:
        raise RuntimeError("Stored Spotify token could not be decrypted — user must reconnect.")
    token_info = oauth.refresh_access_token(refresh)

    identity.access_token = token_info["access_token"]
    identity.token_expires_at = datetime.utcfromtimestamp(token_info["expires_at"])
    if token_info.get("refresh_token"):
        # Spotify sometimes rotates the refresh token.
        identity.refresh_token = encrypt(token_info["refresh_token"])
    if token_info.get("scope"):
        identity.token_scope = token_info.get("scope")
    db.session.commit()
    return spotipy.Spotify(auth=identity.access_token)
