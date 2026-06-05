"""
Archaeologist — Collection Snapshot Builder

Fetches a user's Spotify collection (playlists, saved tracks, top items, recent plays)
and stores it as a compressed JSON blob on users.archaeologist_snapshot.

Heavy operation: 30-80 Spotify API calls depending on library size.
Caps are applied to keep snapshot under rate limits and DB storage manageable.
"""
from __future__ import annotations

import base64
import gzip
import json
from datetime import datetime
from typing import Any

# Caps — tuned to fit comfortably under Spotify's ~180 req/min rate limit,
# and to keep snapshot blob under ~500KB compressed even for power users.
MAX_PLAYLISTS = 100
MAX_TRACKS_PER_PLAYLIST = 500
MAX_TOTAL_PLAYLIST_TRACKS = 5_000
MAX_SAVED_TRACKS = 2_000
MAX_SAVED_ALBUMS = 500
MAX_RECENT = 50
MAX_TOP_ITEMS = 50  # per time range

SCHEMA_VERSION = 1


def _track_summary(track: dict) -> dict:
    """Compact track shape — we drop fields we don't use to keep the blob small."""
    if not track:
        return {}
    return {
        "id": track.get("id"),
        "uri": track.get("uri"),
        "name": track.get("name"),
        "artist_ids": [a.get("id") for a in track.get("artists", []) if a.get("id")],
        "artist_names": [a.get("name") for a in track.get("artists", []) if a.get("name")],
        "album_id": (track.get("album") or {}).get("id"),
        "album_name": (track.get("album") or {}).get("name"),
        "duration_ms": track.get("duration_ms", 0),
        "popularity": track.get("popularity"),
        "is_local": bool(track.get("is_local")),
    }


def _artist_summary(artist: dict) -> dict:
    return {
        "id": artist.get("id"),
        "name": artist.get("name"),
        "genres": artist.get("genres", []),
        "popularity": artist.get("popularity"),
        "followers": (artist.get("followers") or {}).get("total"),
    }


def _paginate(fetch, key: str = "items", limit: int = 50, hard_cap: int | None = None):
    """Generic paginator over spotipy responses with a 'next' field."""
    offset = 0
    while True:
        page = fetch(offset=offset, limit=limit)
        items = page.get(key, []) or []
        for item in items:
            yield item
            if hard_cap is not None:
                hard_cap -= 1
                if hard_cap <= 0:
                    return
        if not page.get("next"):
            return
        offset += limit


def build_snapshot(sp, *, progress=None, limits: dict | None = None) -> dict:
    """
    Fetch and normalize the user's collection.

    Args:
        sp: authenticated spotipy.Spotify client
        progress: optional callable(stage: str, detail: str) for logging
        limits: optional dict to override caps (used in tests)

    Returns:
        dict snapshot with schema_version, built_at, totals, playlists, saved_*, top_*, recent.
    """
    caps = {
        "max_playlists": MAX_PLAYLISTS,
        "max_tracks_per_playlist": MAX_TRACKS_PER_PLAYLIST,
        "max_total_playlist_tracks": MAX_TOTAL_PLAYLIST_TRACKS,
        "max_saved_tracks": MAX_SAVED_TRACKS,
        "max_saved_albums": MAX_SAVED_ALBUMS,
        "max_recent": MAX_RECENT,
        "max_top_items": MAX_TOP_ITEMS,
    }
    if limits:
        caps.update(limits)

    log = progress or (lambda *_a, **_kw: None)
    truncated: list[str] = []

    # ---- Playlists ----
    log("playlists", "listing")
    playlists: list[dict] = []
    total_playlist_tracks = 0
    for p in _paginate(lambda offset, limit: sp.current_user_playlists(limit=limit, offset=offset),
                       hard_cap=caps["max_playlists"]):
        if not p:
            continue
        playlist_id = p.get("id")
        if not playlist_id:
            continue
        playlists.append({
            "id": playlist_id,
            "name": p.get("name"),
            "owner_id": (p.get("owner") or {}).get("id"),
            "owner_name": (p.get("owner") or {}).get("display_name"),
            "tracks_total": (p.get("tracks") or {}).get("total", 0),
            "collaborative": bool(p.get("collaborative")),
            "public": bool(p.get("public")),
            "tracks": [],  # filled below
        })

    if len(playlists) == caps["max_playlists"]:
        truncated.append(f"playlists capped at {caps['max_playlists']}")

    # ---- Playlist tracks ----
    log("playlists", f"fetching tracks for {len(playlists)} playlists")
    for pl in playlists:
        if total_playlist_tracks >= caps["max_total_playlist_tracks"]:
            truncated.append(f"total playlist tracks capped at {caps['max_total_playlist_tracks']}")
            break
        remaining_global = caps["max_total_playlist_tracks"] - total_playlist_tracks
        remaining_for_pl = min(caps["max_tracks_per_playlist"], remaining_global)
        try:
            for item in _paginate(
                lambda offset, limit, pid=pl["id"]: sp.playlist_tracks(pid, offset=offset, limit=limit),
                hard_cap=remaining_for_pl,
            ):
                track = item.get("track")
                if not track:
                    continue
                pl["tracks"].append({
                    **_track_summary(track),
                    "added_at": item.get("added_at"),
                })
                total_playlist_tracks += 1
        except Exception as e:
            pl["error"] = str(e)

    # ---- Saved tracks (Liked Songs) ----
    log("saved_tracks", "fetching")
    saved_tracks: list[dict] = []
    try:
        for item in _paginate(
            lambda offset, limit: sp.current_user_saved_tracks(limit=limit, offset=offset),
            hard_cap=caps["max_saved_tracks"],
        ):
            track = (item or {}).get("track")
            if track:
                saved_tracks.append({**_track_summary(track), "added_at": item.get("added_at")})
        if len(saved_tracks) == caps["max_saved_tracks"]:
            truncated.append(f"saved tracks capped at {caps['max_saved_tracks']}")
    except Exception as e:
        log("saved_tracks", f"error: {e}")

    # ---- Saved albums ----
    log("saved_albums", "fetching")
    saved_albums: list[dict] = []
    try:
        for item in _paginate(
            lambda offset, limit: sp.current_user_saved_albums(limit=limit, offset=offset),
            hard_cap=caps["max_saved_albums"],
        ):
            album = (item or {}).get("album") or {}
            saved_albums.append({
                "id": album.get("id"),
                "name": album.get("name"),
                "artist_ids": [a.get("id") for a in album.get("artists", []) if a.get("id")],
                "artist_names": [a.get("name") for a in album.get("artists", []) if a.get("name")],
                "release_date": album.get("release_date"),
                "added_at": item.get("added_at"),
                "total_tracks": album.get("total_tracks"),
            })
    except Exception as e:
        log("saved_albums", f"error: {e}")

    # ---- Top artists + tracks (per time range) ----
    log("top", "fetching")
    top_artists: dict[str, list[dict]] = {}
    top_tracks: dict[str, list[dict]] = {}
    for tr in ("short_term", "medium_term", "long_term"):
        try:
            ta = sp.current_user_top_artists(limit=caps["max_top_items"], time_range=tr)
            top_artists[tr] = [_artist_summary(a) for a in ta.get("items", [])]
        except Exception as e:
            top_artists[tr] = []
            log("top_artists", f"{tr} error: {e}")
        try:
            tt = sp.current_user_top_tracks(limit=caps["max_top_items"], time_range=tr)
            top_tracks[tr] = [_track_summary(t) for t in tt.get("items", [])]
        except Exception as e:
            top_tracks[tr] = []
            log("top_tracks", f"{tr} error: {e}")

    # ---- Recently played ----
    log("recent", "fetching")
    recent: list[dict] = []
    try:
        rp = sp.current_user_recently_played(limit=caps["max_recent"])
        for item in rp.get("items", []):
            tr = item.get("track")
            if tr:
                recent.append({**_track_summary(tr), "played_at": item.get("played_at")})
    except Exception as e:
        log("recent", f"error: {e}")

    # ---- Profile ----
    try:
        me = sp.current_user()
        profile = {
            "id": me.get("id"),
            "display_name": me.get("display_name"),
            "country": me.get("country"),
            "product": me.get("product"),
        }
    except Exception:
        profile = {}

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "built_at": datetime.utcnow().isoformat() + "Z",
        "profile": profile,
        "caps": caps,
        "truncated": truncated,
        "totals": {
            "playlists": len(playlists),
            "playlist_tracks": total_playlist_tracks,
            "saved_tracks": len(saved_tracks),
            "saved_albums": len(saved_albums),
            "recent": len(recent),
        },
        "playlists": playlists,
        "saved_tracks": saved_tracks,
        "saved_albums": saved_albums,
        "top_artists": top_artists,
        "top_tracks": top_tracks,
        "recent": recent,
    }
    log("done", f"{total_playlist_tracks} playlist tracks, {len(saved_tracks)} saved, {len(playlists)} playlists")
    return snapshot


# ==================== STORAGE ENCODING ====================

def encode_blob(snapshot: dict) -> str:
    """gzip+base64-encode the snapshot for compact text storage."""
    raw = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw, compresslevel=6)
    return base64.b64encode(gz).decode("ascii")


def decode_blob(blob: str) -> dict[str, Any]:
    """Inverse of encode_blob. Returns {} on missing/corrupt blob."""
    if not blob:
        return {}
    try:
        gz = base64.b64decode(blob)
        raw = gzip.decompress(gz)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
