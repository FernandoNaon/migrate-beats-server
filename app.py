from flask import Flask, request, redirect, session, jsonify, g
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
import os
import tidalapi
from datetime import datetime

load_dotenv()

# Import database
from config import get_config
from models import db, User, UserIdentity, UserActivity, Migration, ApiUsage, Session
from models import get_or_create_user, log_activity, check_rate_limit, increment_usage
from archaeologist.snapshot import build_snapshot, encode_blob, decode_blob
from archaeologist import analytics as arch_analytics
from archaeologist import discovery as arch_discovery
import auth as auth_module

app = Flask(__name__)

# Load configuration (includes DATABASE_URL)
app.config.from_object(get_config())

# Initialize database
db.init_app(app)

# CORS Configuration - support both local and production
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
CORS(app,
     supports_credentials=True,
     origins=[FRONTEND_URL, "http://localhost:5173", "http://127.0.0.1:5173"],
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "OPTIONS"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "supersecretkey")

# Spotify Configuration
SPOTIPY_CLIENT_ID = os.environ.get("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.environ.get("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:5000/callback")

# Tidal Configuration
TIDAL_CLIENT_ID = os.environ.get("TIDAL_CLIENT_ID")
TIDAL_CLIENT_SECRET = os.environ.get("TIDAL_CLIENT_SECRET")

FRONTEND_REDIRECT = os.environ.get("FRONTEND_REDIRECT", "http://localhost:5173/callback")

# Store Tidal sessions in memory
tidal_sessions = {}


# ==================== DATABASE ENDPOINTS ====================

@app.route("/db/health", methods=["GET"])
def db_health():
    """Check database health."""
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({"status": "healthy", "database": "connected"})
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


@app.route("/db/stats", methods=["GET"])
def db_stats():
    """Get database statistics."""
    try:
        stats = {
            "users": User.query.count(),
            "migrations": Migration.query.count(),
            "activities": UserActivity.query.count(),
        }
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== USER MANAGEMENT ENDPOINTS ====================

@app.route("/user/me", methods=["POST"])
def user_me():
    """Register or get current user (legacy; prefer /auth/me). Session-authed."""
    data = request.get_json() or {}

    try:
        sp, _ = get_spotify_client()
        spotify_user = sp.current_user()

        # Get or create user in database
        user, is_new = get_or_create_user(
            spotify_user_id=spotify_user["id"],
            email=spotify_user.get("email"),
            display_name=spotify_user.get("display_name", spotify_user["id"]),
            avatar_url=spotify_user["images"][0]["url"] if spotify_user.get("images") else None
        )

        # Log login activity
        log_activity(
            user_id=user.id,
            action="login",
            details={"provider": "spotify", "spotify_id": spotify_user["id"]}
        )

        # Get usage stats for today
        from datetime import date
        today = date.today()
        usage = ApiUsage.query.filter_by(
            user_id=user.id,
            action='migration',
            window_start=today
        ).first()
        migrations_today = usage.count if usage else 0
        rate_limit = app.config.get('RATE_LIMIT_MIGRATIONS', 50)

        return jsonify({
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "tier": user.tier,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "migrations_today": migrations_today,
            "migrations_remaining": max(0, rate_limit - migrations_today),
            "rate_limit": rate_limit
        })
    except Exception as e:
        print(f"[USER] Error in user_me: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/user/history", methods=["POST"])
def user_history():
    """Get user's migration history."""
    data = request.get_json() or {}
    limit = data.get("limit", 20)

    try:
        # Authenticated user comes from the session gate.
        migrations = Migration.query.filter_by(user_id=g.user.id)\
            .order_by(Migration.created_at.desc())\
            .limit(limit)\
            .all()

        return jsonify([{
            "id": m.id,
            "source_provider": m.source_provider,
            "target_provider": m.target_provider,
            "source_playlist_name": m.source_playlist_name,
            "target_playlist_name": m.target_playlist_name,
            "migration_type": m.migration_type,
            "total_tracks": m.total_tracks,
            "migrated_tracks": m.migrated_tracks,
            "skipped_tracks": m.skipped_tracks,
            "status": m.status,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "completed_at": m.completed_at.isoformat() if m.completed_at else None
        } for m in migrations])
    except Exception as e:
        print(f"[USER] Error in user_history: {e}")
        return jsonify({"error": str(e)}), 500


# Extended scopes for dashboard insights + playlist management
SCOPE = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public user-top-read user-read-recently-played user-library-read user-read-private user-follow-read"


def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE,
        cache_handler=spotipy.cache_handler.MemoryCacheHandler(),
    )


_SPOTIFY_OAUTH_KWARGS = dict(
    client_id=SPOTIPY_CLIENT_ID,
    client_secret=SPOTIPY_CLIENT_SECRET,
    redirect_uri=SPOTIPY_REDIRECT_URI,
    scope=SCOPE,
)


def get_spotify_client(code=None):
    """Authenticated Spotify client for the CURRENT SESSION user.

    The `code` parameter is legacy and IGNORED — auth now comes from the session
    token resolved by the before_request gate into g.user. Kept so the many existing
    `sp, _ = get_spotify_client(code)` call sites work unchanged. Returns (client, None).
    """
    user = getattr(g, "user", None)
    if user is None:
        raise RuntimeError("No authenticated session on this request.")
    sp = auth_module.get_spotify_client_for_user(user, **_SPOTIFY_OAUTH_KWARGS)
    return sp, None


# ==================== SESSION AUTH GATE ====================

# Paths that require a valid session (Bearer token). Tidal-only and /auth/* and
# /db/* and /login,/callback stay open.
SESSION_PROTECTED_PATHS = {
    "/user/me", "/user/history", "/user_profile",
    "/fetch_playlists", "/playlist_tracks",
    "/playlist/details", "/playlist/create", "/playlist/add_tracks",
    "/playlist/remove_tracks", "/playlist/reorder", "/playlist/delete",
    "/playlist/genres", "/playlist/move_tracks",
    "/top_tracks", "/top_artists", "/recently_played",
    "/liked_songs", "/liked_songs/move_to_playlist", "/library_stats",
    "/migrate_tidal_to_spotify", "/migrate_tidal_tracks",
    "/migrate_tracks", "/migrate_playlist",
    "/archaeologist/status", "/archaeologist/snapshot/refresh",
    "/archaeologist/overview", "/archaeologist/playlist_overlap",
    "/archaeologist/artist_dominance", "/archaeologist/forgotten",
    "/archaeologist/playlist_health", "/archaeologist/evolution",
    "/archaeologist/genre_clusters", "/archaeologist/genre_outliers",
    "/archaeologist/hidden_gems", "/archaeologist/co_occurrence",
    "/auth/me",
    # NOTE: /auth/logout is intentionally NOT protected — it reads the token itself and
    # should succeed (idempotently) even with an expired/invalid session.
}


@app.before_request
def _session_auth_gate():
    if request.method == "OPTIONS":
        return  # let CORS preflight through
    if request.path not in SESSION_PROTECTED_PATHS:
        return
    authz = request.headers.get("Authorization", "")
    token = authz[7:].strip() if authz.startswith("Bearer ") else None
    user = auth_module.resolve_session(token) if token else None
    if not user:
        return jsonify({"error": "Not authenticated"}), 401
    g.user = user


# ==================== SPOTIFY AUTH ====================

@app.route("/login", methods=["GET"])
def login():
    auth_url = get_spotify_oauth().get_authorize_url()
    return jsonify({"auth_url": auth_url})


@app.route("/callback")
def spotify_callback_redirect():
    code = request.args.get("code")
    return redirect(f"{FRONTEND_REDIRECT}?code={code}")


@app.route("/auth/spotify/exchange", methods=["POST", "OPTIONS"])
def auth_spotify_exchange():
    """Exchange a Spotify OAuth code ONCE for tokens, store them, return a session token."""
    if request.method == "OPTIONS":
        return "", 200
    data = request.get_json() or {}
    code = data.get("code")
    if not code:
        return jsonify({"error": "Authorization code required for exchange"}), 400
    try:
        user, sp, _token_info = auth_module.exchange_code_and_store(code, **_SPOTIFY_OAUTH_KWARGS)
        log_activity(user_id=user.id, action="login", details={"provider": "spotify"})
        session_token = auth_module.create_session(user.id)
        return jsonify({"session_token": session_token, **_auth_me_payload(user, sp)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/auth/logout", methods=["POST", "OPTIONS"])
def auth_logout():
    if request.method == "OPTIONS":
        return "", 200
    authz = request.headers.get("Authorization", "")
    token = authz[7:].strip() if authz.startswith("Bearer ") else None
    auth_module.revoke_session(token)
    return jsonify({"success": True})


def _auth_me_payload(user, sp=None):
    """Profile + tier + today's usage for the authed user."""
    from datetime import date
    profile = {}
    try:
        if sp is None:
            sp = auth_module.get_spotify_client_for_user(user, **_SPOTIFY_OAUTH_KWARGS)
        me = sp.current_user()
        profile = {
            "id": me["id"],
            "display_name": me.get("display_name", me["id"]),
            "email": me.get("email"),
            "image": me["images"][0]["url"] if me.get("images") else None,
            "country": me.get("country"),
            "product": me.get("product"),
            "followers": me.get("followers", {}).get("total", 0),
        }
    except Exception as e:
        print(f"[AUTH] profile fetch failed: {e}", flush=True)

    today = date.today()
    usage = ApiUsage.query.filter_by(user_id=user.id, action="migration", window_start=today).first()
    migrations_today = usage.count if usage else 0
    rate_limit = app.config.get("RATE_LIMIT_MIGRATIONS", 50)
    return {
        "spotify_user": profile,
        "app_user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "avatar_url": user.avatar_url,
            "tier": user.tier,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "usage": {
                "migrations_today": migrations_today,
                "migrations_limit": rate_limit,
            },
        },
    }


@app.route("/auth/me", methods=["POST", "OPTIONS"])
def auth_me():
    if request.method == "OPTIONS":
        return "", 200
    return jsonify(_auth_me_payload(g.user))


# ==================== SPOTIFY PLAYLISTS ====================

@app.route("/fetch_playlists", methods=["POST"])
def fetch_playlists():
    data = request.get_json()
    code = data.get("code")


    try:
        sp, _ = get_spotify_client(code)

        playlists = []
        limit = 50
        offset = 0

        while True:
            response = sp.current_user_playlists(limit=limit, offset=offset)
            items = response.get("items", [])
            playlists.extend([
                {
                    "id": p["id"],
                    "name": p["name"],
                    "tracks_total": p["tracks"]["total"],
                    "image": p["images"][0]["url"] if p.get("images") else None,
                    "owner": p["owner"]["display_name"]
                } for p in items
            ])
            if response.get("next"):
                offset += limit
            else:
                break

        return jsonify(playlists)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/playlist_tracks", methods=["POST"])
def playlist_tracks():
    data = request.get_json()
    playlist_id = data.get("playlist_id")
    code = data.get("code")

    if not playlist_id:
        return jsonify({"error": "playlist_id is required"}), 400

    try:
        sp, _ = get_spotify_client(code)

        tracks = []
        offset = 0
        limit = 100

        while True:
            results = sp.playlist_tracks(playlist_id, offset=offset, limit=limit)
            for item in results["items"]:
                track = item.get("track")
                if track:
                    tracks.append({
                        "id": track.get("id"),
                        "uri": track.get("uri"),
                        "name": track["name"],
                        "artist": ", ".join([artist["name"] for artist in track["artists"]]),
                        "artists": [artist["name"] for artist in track["artists"]],
                        "album": track["album"]["name"],
                        "duration_ms": track["duration_ms"],
                        "image": track["album"]["images"][0]["url"] if track["album"].get("images") else None,
                        "is_local": bool(track.get("is_local"))
                    })

            if results.get("next"):
                offset += limit
            else:
                break

        return jsonify(tracks)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== SPOTIFY PLAYLIST MANAGEMENT ====================

@app.route("/playlist/details", methods=["POST"])
def playlist_details():
    """Get a Spotify playlist with snapshot_id + all tracks. Used by the playlist manager."""
    data = request.get_json()
    code = data.get("code")
    playlist_id = data.get("playlist_id")

    if not playlist_id:
        return jsonify({"error": "playlist_id is required"}), 400

    try:
        sp, _ = get_spotify_client(code)
        meta = sp.playlist(playlist_id, fields="id,name,snapshot_id,owner.id,owner.display_name,images,tracks.total")

        tracks = []
        offset = 0
        limit = 100
        while True:
            results = sp.playlist_tracks(playlist_id, offset=offset, limit=limit)
            for item in results["items"]:
                track = item.get("track")
                if not track:
                    continue
                tracks.append({
                    "id": track.get("id"),
                    "uri": track.get("uri"),
                    "name": track["name"],
                    "artist": ", ".join([a["name"] for a in track["artists"]]),
                    "artists": [a["name"] for a in track["artists"]],
                    "album": track["album"]["name"],
                    "duration_ms": track["duration_ms"],
                    "image": track["album"]["images"][0]["url"] if track["album"].get("images") else None,
                    "is_local": bool(track.get("is_local"))
                })
            if results.get("next"):
                offset += limit
            else:
                break

        # Owner check — only owner can mutate (collaborative is rare; treat as read-only for v1)
        me = sp.current_user()
        is_owner = meta.get("owner", {}).get("id") == me.get("id")

        return jsonify({
            "id": meta["id"],
            "name": meta["name"],
            "snapshot_id": meta["snapshot_id"],
            "image": meta["images"][0]["url"] if meta.get("images") else None,
            "owner": meta.get("owner", {}).get("display_name"),
            "is_owner": is_owner,
            "tracks_total": meta.get("tracks", {}).get("total", len(tracks)),
            "tracks": tracks
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/playlist/create", methods=["POST"])
def playlist_create():
    """Create a new Spotify playlist."""
    data = request.get_json()
    code = data.get("code")
    name = data.get("name")
    description = data.get("description", "")
    public = bool(data.get("public", False))

    if not name or not name.strip():
        return jsonify({"error": "name is required"}), 400

    try:
        sp, _ = get_spotify_client(code)
        me = sp.current_user()
        playlist = sp.user_playlist_create(me["id"], name.strip(), public=public, description=description)
        return jsonify({
            "id": playlist["id"],
            "name": playlist["name"],
            "snapshot_id": playlist.get("snapshot_id"),
            "image": playlist["images"][0]["url"] if playlist.get("images") else None,
            "owner": playlist.get("owner", {}).get("display_name"),
            "tracks_total": 0
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/playlist/add_tracks", methods=["POST"])
def playlist_add_tracks():
    """Add tracks to a Spotify playlist. Batches in chunks of 100."""
    data = request.get_json()
    code = data.get("code")
    playlist_id = data.get("playlist_id")
    track_uris = data.get("track_uris", [])
    position = data.get("position")  # optional insertion index

    if not playlist_id:
        return jsonify({"error": "playlist_id is required"}), 400
    if not track_uris:
        return jsonify({"error": "track_uris is required"}), 400

    try:
        sp, _ = get_spotify_client(code)
        snapshot_id = None
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i + 100]
            kwargs = {}
            if position is not None and i == 0:
                kwargs["position"] = position
            result = sp.playlist_add_items(playlist_id, batch, **kwargs)
            snapshot_id = result.get("snapshot_id", snapshot_id)
        return jsonify({"success": True, "snapshot_id": snapshot_id, "added": len(track_uris)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/playlist/remove_tracks", methods=["POST"])
def playlist_remove_tracks():
    """Remove tracks from a Spotify playlist. Pass snapshot_id for safe concurrent edits."""
    data = request.get_json()
    code = data.get("code")
    playlist_id = data.get("playlist_id")
    track_uris = data.get("track_uris", [])
    snapshot_id = data.get("snapshot_id")

    if not playlist_id:
        return jsonify({"error": "playlist_id is required"}), 400
    if not track_uris:
        return jsonify({"error": "track_uris is required"}), 400

    try:
        sp, _ = get_spotify_client(code)
        new_snapshot = snapshot_id
        # spotipy: playlist_remove_all_occurrences_of_items removes by URI
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i + 100]
            result = sp.playlist_remove_all_occurrences_of_items(playlist_id, batch, snapshot_id=new_snapshot)
            new_snapshot = result.get("snapshot_id", new_snapshot)
        return jsonify({"success": True, "snapshot_id": new_snapshot, "removed": len(track_uris)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/playlist/reorder", methods=["POST"])
def playlist_reorder():
    """Reorder tracks within a single Spotify playlist."""
    data = request.get_json()
    code = data.get("code")
    playlist_id = data.get("playlist_id")
    range_start = data.get("range_start")
    insert_before = data.get("insert_before")
    range_length = data.get("range_length", 1)
    snapshot_id = data.get("snapshot_id")

    if not playlist_id:
        return jsonify({"error": "playlist_id is required"}), 400
    if range_start is None or insert_before is None:
        return jsonify({"error": "range_start and insert_before are required"}), 400

    try:
        sp, _ = get_spotify_client(code)
        result = sp.playlist_reorder_items(
            playlist_id,
            range_start=range_start,
            insert_before=insert_before,
            range_length=range_length,
            snapshot_id=snapshot_id
        )
        return jsonify({"success": True, "snapshot_id": result.get("snapshot_id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/playlist/delete", methods=["POST"])
def playlist_delete():
    """Delete (unfollow) a Spotify playlist. Spotify has no true 'delete' — the owner unfollowing
    is the canonical way to remove it from their account."""
    data = request.get_json()
    code = data.get("code")
    playlist_id = data.get("playlist_id")

    if not playlist_id:
        return jsonify({"error": "playlist_id is required"}), 400

    try:
        sp, _ = get_spotify_client(code)
        # Verify ownership — Spotify lets you unfollow any playlist, but we only want to
        # offer this as "delete" when the user actually owns it.
        meta = sp.playlist(playlist_id, fields="owner.id")
        me = sp.current_user()
        if meta.get("owner", {}).get("id") != me.get("id"):
            return jsonify({"error": "Only the owner can delete this playlist"}), 403
        sp.current_user_unfollow_playlist(playlist_id)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/playlist/genres", methods=["POST"])
def playlist_genres():
    """Aggregate Spotify artist genres for a playlist.

    Spotify only exposes genres on the standalone /artists endpoint, not on track payloads.
    We fetch the playlist's unique artist IDs once and batch them (50/request).
    """
    data = request.get_json() or {}
    code = data.get("code")
    playlist_id = data.get("playlist_id")
    top_n = data.get("top_n", 30)

    if not playlist_id:
        return jsonify({"error": "playlist_id is required"}), 400

    try:
        sp, _ = get_spotify_client(code)

        # Collect unique artist IDs from playlist tracks
        artist_ids: list[str] = []
        seen: set[str] = set()
        offset = 0
        limit = 100
        while True:
            results = sp.playlist_tracks(
                playlist_id, offset=offset, limit=limit,
                fields="items(track(artists(id,name))),next"
            )
            for item in results.get("items", []) or []:
                track = item.get("track") or {}
                for a in track.get("artists", []) or []:
                    aid = a.get("id")
                    if aid and aid not in seen:
                        seen.add(aid)
                        artist_ids.append(aid)
            if results.get("next"):
                offset += limit
            else:
                break

        if not artist_ids:
            return jsonify({"genres": [], "total_artists": 0})

        # Batch /artists (max 50 per call)
        from collections import Counter
        genre_count: Counter = Counter()
        # genre -> list of (artist_name, popularity) for sample picking
        genre_artists: dict = {}
        for i in range(0, len(artist_ids), 50):
            batch = artist_ids[i:i + 50]
            resp = sp.artists(batch) or {}
            for artist in resp.get("artists", []) or []:
                name = artist.get("name") or "?"
                popularity = artist.get("popularity") or 0
                for g in artist.get("genres", []) or []:
                    genre_count[g] += 1
                    genre_artists.setdefault(g, []).append((name, popularity))

        genres = []
        for genre, count in genre_count.most_common(top_n):
            sample = sorted(genre_artists.get(genre, []), key=lambda x: x[1], reverse=True)[:3]
            genres.append({
                "genre": genre,
                "count": count,
                "sample_artists": [name for name, _ in sample],
            })

        return jsonify({"genres": genres, "total_artists": len(artist_ids)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/playlist/move_tracks", methods=["POST"])
def playlist_move_tracks():
    """Move tracks across two Spotify playlists. NOT atomic — returns per-step result."""
    data = request.get_json()
    code = data.get("code")
    source_playlist_id = data.get("source_playlist_id")
    target_playlist_id = data.get("target_playlist_id")
    track_uris = data.get("track_uris", [])
    source_snapshot_id = data.get("source_snapshot_id")
    target_position = data.get("target_position")  # optional insertion index in target
    copy_only = bool(data.get("copy_only", False))  # if true, don't remove from source

    if not source_playlist_id or not target_playlist_id:
        return jsonify({"error": "source and target playlist_id required"}), 400
    if source_playlist_id == target_playlist_id:
        return jsonify({"error": "source and target must differ; use /playlist/reorder"}), 400
    if not track_uris:
        return jsonify({"error": "track_uris is required"}), 400

    try:
        sp, _ = get_spotify_client(code)

        # 1) Add to target first — if this fails, source is untouched.
        target_snapshot = None
        for i in range(0, len(track_uris), 100):
            batch = track_uris[i:i + 100]
            kwargs = {}
            if target_position is not None and i == 0:
                kwargs["position"] = target_position
            result = sp.playlist_add_items(target_playlist_id, batch, **kwargs)
            target_snapshot = result.get("snapshot_id", target_snapshot)

        # 2) Remove from source (unless copy-only).
        source_snapshot_new = source_snapshot_id
        removed = 0
        if not copy_only:
            for i in range(0, len(track_uris), 100):
                batch = track_uris[i:i + 100]
                result = sp.playlist_remove_all_occurrences_of_items(
                    source_playlist_id, batch, snapshot_id=source_snapshot_new
                )
                source_snapshot_new = result.get("snapshot_id", source_snapshot_new)
                removed += len(batch)

        return jsonify({
            "success": True,
            "added": len(track_uris),
            "removed": removed,
            "source_snapshot_id": source_snapshot_new,
            "target_snapshot_id": target_snapshot
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== SPOTIFY INSIGHTS/DASHBOARD ====================

@app.route("/user_profile", methods=["POST"])
def user_profile():
    data = request.get_json()
    code = data.get("code")


    try:
        sp, _ = get_spotify_client(code)
        user = sp.current_user()

        return jsonify({
            "id": user["id"],
            "display_name": user.get("display_name", user["id"]),
            "email": user.get("email"),
            "image": user["images"][0]["url"] if user.get("images") else None,
            "country": user.get("country"),
            "product": user.get("product"),  # premium, free, etc.
            "followers": user.get("followers", {}).get("total", 0)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/top_tracks", methods=["POST"])
def top_tracks():
    data = request.get_json()
    code = data.get("code")
    time_range = data.get("time_range", "medium_term")  # short_term, medium_term, long_term
    limit = data.get("limit", 20)


    try:
        sp, _ = get_spotify_client(code)
        results = sp.current_user_top_tracks(limit=limit, time_range=time_range)

        tracks = []
        for track in results["items"]:
            tracks.append({
                "id": track["id"],
                "name": track["name"],
                "artist": ", ".join([artist["name"] for artist in track["artists"]]),
                "album": track["album"]["name"],
                "image": track["album"]["images"][0]["url"] if track["album"].get("images") else None,
                "popularity": track.get("popularity", 0)
            })

        return jsonify(tracks)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/top_artists", methods=["POST"])
def top_artists():
    data = request.get_json()
    code = data.get("code")
    time_range = data.get("time_range", "medium_term")
    limit = data.get("limit", 20)


    try:
        sp, _ = get_spotify_client(code)
        results = sp.current_user_top_artists(limit=limit, time_range=time_range)

        artists = []
        for artist in results["items"]:
            artists.append({
                "id": artist["id"],
                "name": artist["name"],
                "genres": artist.get("genres", []),
                "image": artist["images"][0]["url"] if artist.get("images") else None,
                "popularity": artist.get("popularity", 0),
                "followers": artist.get("followers", {}).get("total", 0)
            })

        return jsonify(artists)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recently_played", methods=["POST"])
def recently_played():
    data = request.get_json()
    code = data.get("code")
    limit = data.get("limit", 20)


    try:
        sp, _ = get_spotify_client(code)
        results = sp.current_user_recently_played(limit=limit)

        tracks = []
        for item in results["items"]:
            track = item["track"]
            tracks.append({
                "id": track["id"],
                "name": track["name"],
                "artist": ", ".join([artist["name"] for artist in track["artists"]]),
                "album": track["album"]["name"],
                "image": track["album"]["images"][0]["url"] if track["album"].get("images") else None,
                "played_at": item["played_at"]
            })

        return jsonify(tracks)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/liked_songs", methods=["POST", "OPTIONS"])
def liked_songs():
    if request.method == "OPTIONS":
        return "", 200
    """Get user's liked/saved songs from Spotify."""
    data = request.get_json()
    code = data.get("code")
    limit = data.get("limit", 50)
    offset = data.get("offset", 0)


    try:
        sp, _ = get_spotify_client(code)
        results = sp.current_user_saved_tracks(limit=limit, offset=offset)

        tracks = []
        for item in results["items"]:
            track = item["track"]
            tracks.append({
                "id": track["id"],
                "name": track["name"],
                "artist": ", ".join([artist["name"] for artist in track["artists"]]),
                "artists": [artist["name"] for artist in track["artists"]],
                "album": track["album"]["name"],
                "duration_ms": track["duration_ms"],
                "image": track["album"]["images"][0]["url"] if track["album"].get("images") else None,
                "added_at": item["added_at"]
            })

        return jsonify({
            "tracks": tracks,
            "total": results.get("total", 0),
            "limit": limit,
            "offset": offset,
            "has_more": results.get("next") is not None
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/liked_songs/move_to_playlist", methods=["POST", "OPTIONS"])
def liked_songs_move_to_playlist():
    """Move (or copy) liked-songs tracks into a Spotify playlist.
    By default removes the tracks from Liked Songs after adding. Pass copy_only=true to keep them.
    """
    if request.method == "OPTIONS":
        return "", 200
    data = request.get_json()
    code = data.get("code")
    target_playlist_id = data.get("target_playlist_id")
    track_ids = data.get("track_ids", [])  # bare Spotify track IDs
    copy_only = bool(data.get("copy_only", False))

    if not target_playlist_id:
        return jsonify({"error": "target_playlist_id is required"}), 400
    if not track_ids:
        return jsonify({"error": "track_ids is required"}), 400

    try:
        sp, _ = get_spotify_client(code)

        # Verify the target playlist is owned by the user — adding to a non-owned playlist
        # only works for collaborative ones, and "delete from liked" is destructive.
        meta = sp.playlist(target_playlist_id, fields="owner.id,snapshot_id")
        me = sp.current_user()
        if meta.get("owner", {}).get("id") != me.get("id"):
            return jsonify({"error": "Target playlist is not owned by you"}), 403

        # 1) Add to playlist (chunks of 100)
        uris = [f"spotify:track:{tid}" for tid in track_ids if tid]
        snapshot_id = meta.get("snapshot_id")
        for i in range(0, len(uris), 100):
            result = sp.playlist_add_items(target_playlist_id, uris[i:i + 100])
            snapshot_id = result.get("snapshot_id", snapshot_id)

        # 2) Remove from Liked Songs unless copy_only
        removed = 0
        if not copy_only:
            for i in range(0, len(track_ids), 50):
                # current_user_saved_tracks_delete accepts up to 50 IDs
                sp.current_user_saved_tracks_delete(track_ids[i:i + 50])
                removed += len(track_ids[i:i + 50])

        return jsonify({
            "success": True,
            "added": len(uris),
            "removed": removed,
            "snapshot_id": snapshot_id
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/library_stats", methods=["POST"])
def library_stats():
    data = request.get_json()
    code = data.get("code")


    try:
        sp, _ = get_spotify_client(code)

        # Get saved tracks count
        try:
            saved_tracks = sp.current_user_saved_tracks(limit=1)
            total_saved_tracks = saved_tracks.get("total", 0)
        except:
            total_saved_tracks = 0

        # Get playlists count
        try:
            playlists = sp.current_user_playlists(limit=1)
            total_playlists = playlists.get("total", 0)
        except:
            total_playlists = 0

        # Get saved albums count
        try:
            saved_albums = sp.current_user_saved_albums(limit=1)
            total_saved_albums = saved_albums.get("total", 0)
        except:
            total_saved_albums = 0

        # Get followed artists count
        try:
            followed = sp.current_user_followed_artists(limit=1)
            total_followed_artists = followed.get("artists", {}).get("total", 0)
        except:
            total_followed_artists = 0

        return jsonify({
            "saved_tracks": total_saved_tracks,
            "playlists": total_playlists,
            "saved_albums": total_saved_albums,
            "followed_artists": total_followed_artists
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== TIDAL INTEGRATION ====================

@app.route("/tidal/login", methods=["POST"])
def tidal_login():
    """Start Tidal OAuth login flow using device authorization."""
    try:
        # Use default tidalapi session (library's built-in credentials)
        # Custom credentials cause 400 errors with Tidal's device auth flow
        # The tidalapi library has its own registered client ID
        tidal_session = tidalapi.Session()

        print(f"[Tidal] Starting OAuth login...")

        # Use login_oauth - this uses tidalapi's internal credentials
        login, future = tidal_session.login_oauth()

        # Generate a unique session ID
        import uuid
        session_id = str(uuid.uuid4())

        tidal_sessions[session_id] = {
            "session": tidal_session,
            "future": future,
            "login": login
        }

        # Fix URL - tidalapi returns URL without https:// prefix
        verification_url = login.verification_uri_complete
        if not verification_url.startswith("http"):
            verification_url = f"https://{verification_url}"

        print(f"[Tidal] Login URL: {verification_url}")
        print(f"[Tidal] User code: {login.user_code}")

        return jsonify({
            "verification_uri": verification_url,
            "user_code": login.user_code,
            "session_id": session_id,
            "expires_in": login.expires_in
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/tidal/check_auth", methods=["POST"])
def tidal_check_auth():
    """Check if Tidal authorization has been completed."""
    data = request.get_json()
    session_id = data.get("session_id")

    if not session_id or session_id not in tidal_sessions:
        print(f"[Tidal] Invalid session: {session_id}")
        return jsonify({"authenticated": False, "error": "Invalid session"}), 200

    try:
        tidal_data = tidal_sessions[session_id]
        tidal_session = tidal_data["session"]
        future = tidal_data["future"]

        print(f"[Tidal] Checking auth - future.done(): {future.done()}")

        # Check if the future is done (user completed login)
        if future.done():
            try:
                future.result()  # This will raise if there was an error
                # Authorization successful
                user = tidal_session.user
                print(f"[Tidal] Auth successful! User: {user}")
                return jsonify({
                    "authenticated": True,
                    "user": {
                        "id": str(user.id) if user else "unknown",
                        "name": getattr(user, 'name', None) or getattr(user, 'first_name', None) or str(user.id) if user else "Tidal User"
                    }
                })
            except Exception as e:
                print(f"[Tidal] Future result error: {e}")
                return jsonify({"authenticated": False, "error": str(e)})
        else:
            print(f"[Tidal] Still waiting for user to authorize...")
            return jsonify({"authenticated": False})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e), "authenticated": False}), 500


@app.route("/tidal/playlists", methods=["POST"])
def tidal_playlists():
    """Get user's Tidal playlists."""
    data = request.get_json()
    session_id = data.get("session_id")

    if not session_id or session_id not in tidal_sessions:
        return jsonify({"error": "Invalid session"}), 400

    try:
        tidal_session = tidal_sessions[session_id]["session"]
        user_playlists = tidal_session.user.playlists()

        playlists = []
        for p in user_playlists:
            # Get image URL - image() is a method in tidalapi
            image_url = None
            try:
                if hasattr(p, 'image') and callable(p.image):
                    image_url = p.image(320)  # Get 320x320 image
                elif hasattr(p, 'picture') and p.picture:
                    image_url = f"https://resources.tidal.com/images/{p.picture.replace('-', '/')}/320x320.jpg"
                elif hasattr(p, 'square_picture') and p.square_picture:
                    image_url = f"https://resources.tidal.com/images/{p.square_picture.replace('-', '/')}/320x320.jpg"
            except:
                pass

            playlists.append({
                "id": str(p.id),
                "name": p.name,
                "tracks_total": p.num_tracks if hasattr(p, 'num_tracks') else 0,
                "image": image_url,
                "description": p.description if hasattr(p, 'description') else ""
            })

        return jsonify(playlists)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/tidal/playlist_tracks", methods=["POST"])
def tidal_playlist_tracks():
    """Get tracks from a Tidal playlist."""
    data = request.get_json()
    session_id = data.get("session_id")
    playlist_id = data.get("playlist_id")

    if not session_id or session_id not in tidal_sessions:
        return jsonify({"error": "Invalid session"}), 400
    if not playlist_id:
        return jsonify({"error": "Playlist ID required"}), 400

    try:
        tidal_session = tidal_sessions[session_id]["session"]
        playlist = tidal_session.playlist(playlist_id)
        playlist_tracks = playlist.tracks()

        tracks = []
        for track in playlist_tracks:
            # Get album image URL - image() is a method in tidalapi
            image_url = None
            try:
                if track.album:
                    if hasattr(track.album, 'image') and callable(track.album.image):
                        image_url = track.album.image(320)
                    elif hasattr(track.album, 'cover') and track.album.cover:
                        image_url = f"https://resources.tidal.com/images/{track.album.cover.replace('-', '/')}/320x320.jpg"
            except:
                pass

            tracks.append({
                "id": str(track.id),
                "name": track.name,
                "artist": track.artist.name if track.artist else "Unknown",
                "album": track.album.name if track.album else "Unknown",
                "duration_ms": track.duration * 1000 if hasattr(track, 'duration') else 0,
                "image": image_url
            })

        return jsonify(tracks)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/tidal/delete_playlist", methods=["POST"])
def tidal_delete_playlist():
    """Delete a Tidal playlist."""
    data = request.get_json()
    session_id = data.get("session_id")
    playlist_id = data.get("playlist_id")

    if not session_id or session_id not in tidal_sessions:
        return jsonify({"error": "Invalid session"}), 400
    if not playlist_id:
        return jsonify({"error": "Playlist ID required"}), 400

    try:
        tidal_session = tidal_sessions[session_id]["session"]
        playlist = tidal_session.playlist(playlist_id)

        # Delete the playlist
        playlist.delete()

        return jsonify({"success": True, "message": "Playlist deleted successfully"})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/tidal/merge_playlists", methods=["POST"])
def tidal_merge_playlists():
    """Merge two Tidal playlists into one."""
    data = request.get_json()
    session_id = data.get("session_id")
    source_playlist_id = data.get("source_playlist_id")  # Playlist to merge FROM (will be deleted)
    target_playlist_id = data.get("target_playlist_id")  # Playlist to merge INTO (will keep)

    if not session_id or session_id not in tidal_sessions:
        return jsonify({"error": "Invalid session"}), 400
    if not source_playlist_id or not target_playlist_id:
        return jsonify({"error": "Both source and target playlist IDs required"}), 400
    if source_playlist_id == target_playlist_id:
        return jsonify({"error": "Cannot merge a playlist with itself"}), 400

    try:
        tidal_session = tidal_sessions[session_id]["session"]

        # Get both playlists
        source_playlist = tidal_session.playlist(source_playlist_id)
        target_playlist = tidal_session.playlist(target_playlist_id)

        # Get tracks from source playlist
        source_tracks = source_playlist.tracks()

        # Get existing track IDs in target to avoid duplicates
        target_tracks = target_playlist.tracks()
        existing_track_ids = {str(t.id) for t in target_tracks}

        # Filter out tracks that already exist in target
        tracks_to_add = [t for t in source_tracks if str(t.id) not in existing_track_ids]

        # Add tracks to target playlist
        added_count = 0
        for track in tracks_to_add:
            try:
                target_playlist.add([track.id])
                added_count += 1
            except Exception as e:
                print(f"Failed to add track {track.id}: {e}")

        # Delete the source playlist
        source_playlist.delete()

        return jsonify({
            "success": True,
            "message": f"Merged {added_count} tracks into target playlist",
            "tracks_added": added_count,
            "tracks_skipped": len(source_tracks) - added_count,
            "source_deleted": True
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/tidal/search", methods=["POST"])
def tidal_search():
    """Search for tracks on Tidal."""
    data = request.get_json()
    session_id = data.get("session_id")
    query = data.get("query")

    if not session_id or session_id not in tidal_sessions:
        return jsonify({"error": "Invalid session"}), 400
    if not query:
        return jsonify({"error": "Query required"}), 400

    try:
        tidal_session = tidal_sessions[session_id]["session"]
        results = tidal_session.search(query, models=[tidalapi.media.Track], limit=5)

        tracks = []
        for track in results.get("tracks", []):
            tracks.append({
                "id": track.id,
                "name": track.name,
                "artist": track.artist.name if track.artist else "Unknown",
                "album": track.album.name if track.album else "Unknown"
            })

        return jsonify(tracks)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/tidal/create_playlist", methods=["POST"])
def tidal_create_playlist():
    """Create a new playlist on Tidal and add tracks."""
    data = request.get_json()
    session_id = data.get("session_id")
    name = data.get("name")
    description = data.get("description", "")
    track_ids = data.get("track_ids", [])

    if not session_id or session_id not in tidal_sessions:
        return jsonify({"error": "Invalid session"}), 400
    if not name:
        return jsonify({"error": "Playlist name required"}), 400

    try:
        tidal_session = tidal_sessions[session_id]["session"]

        # Create playlist
        playlist = tidal_session.user.create_playlist(name, description)

        # Add tracks if provided
        if track_ids:
            playlist.add(track_ids)

        return jsonify({
            "success": True,
            "playlist_id": playlist.id,
            "name": playlist.name,
            "tracks_added": len(track_ids)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== TIDAL LIKED SONGS ====================

@app.route("/tidal/liked_songs", methods=["POST", "OPTIONS"])
def tidal_liked_songs():
    if request.method == "OPTIONS":
        return "", 200
    """Get user's liked/favorite tracks from Tidal."""
    data = request.get_json()
    session_id = data.get("session_id")
    limit = data.get("limit", 50)
    offset = data.get("offset", 0)

    if not session_id or session_id not in tidal_sessions:
        return jsonify({"error": "Invalid session"}), 400

    try:
        tidal_session = tidal_sessions[session_id]["session"]

        # Get user's favorite tracks
        favorites = tidal_session.user.favorites
        favorite_tracks = favorites.tracks(limit=limit, offset=offset)

        tracks = []
        for track in favorite_tracks:
            tracks.append({
                "id": str(track.id),
                "name": track.name,
                "artist": track.artist.name if track.artist else "Unknown",
                "artists": [track.artist.name] if track.artist else [],
                "album": track.album.name if track.album else "Unknown",
                "duration_ms": (track.duration or 0) * 1000,  # Tidal uses seconds
                "image": track.album.image(320) if track.album else None,
                "added_at": track.user_date_added.isoformat() if hasattr(track, 'user_date_added') and track.user_date_added else None
            })

        # Try to get total count
        total = len(favorite_tracks)  # Fallback
        has_more = len(favorite_tracks) == limit

        return jsonify({
            "tracks": tracks,
            "total": total,
            "has_more": has_more
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ==================== TIDAL TO SPOTIFY MIGRATION ====================

@app.route("/migrate_tidal_to_spotify", methods=["POST", "OPTIONS"])
def migrate_tidal_to_spotify():
    if request.method == "OPTIONS":
        return "", 200
    """Migrate a Tidal playlist to Spotify."""
    data = request.get_json()
    spotify_code = data.get("spotify_code")
    tidal_session_id = data.get("tidal_session_id")
    playlist_id = data.get("playlist_id")
    playlist_name = data.get("playlist_name")

    if not tidal_session_id or tidal_session_id not in tidal_sessions:
        return jsonify({"error": "Tidal authorization required"}), 400
    if not playlist_id:
        return jsonify({"error": "Playlist ID required"}), 400

    # Get user for tracking
    user_id = None

    try:
        # Get clients
        sp, _ = get_spotify_client(spotify_code)
        tidal_session = tidal_sessions[tidal_session_id]["session"]

        # Get user ID for tracking
        try:
            spotify_user = sp.current_user()
            identity = UserIdentity.query.filter_by(
                provider='spotify',
                provider_user_id=spotify_user["id"]
            ).first()
            if identity:
                user_id = identity.user_id
        except Exception as e:
            print(f"[MIGRATE] Could not identify user: {e}")

        # Fetch all tracks from Tidal playlist
        tidal_playlist = tidal_session.playlist(playlist_id)
        tidal_tracks = tidal_playlist.tracks()

        source_tracks = []
        for track in tidal_tracks:
            source_tracks.append({
                "name": track.name,
                "artist": track.artist.name if track.artist else "",
                "album": track.album.name if track.album else ""
            })

        # Search for tracks on Spotify and collect URIs
        spotify_uris = []
        not_found = []

        for track in source_tracks:
            query = f"track:{track['name']} artist:{track['artist']}"
            try:
                results = sp.search(q=query, type='track', limit=1)
                found_tracks = results.get("tracks", {}).get("items", [])
                if found_tracks:
                    spotify_uris.append(found_tracks[0]["uri"])
                else:
                    # Try simpler search
                    simple_query = f"{track['name']} {track['artist']}"
                    results = sp.search(q=simple_query, type='track', limit=1)
                    found_tracks = results.get("tracks", {}).get("items", [])
                    if found_tracks:
                        spotify_uris.append(found_tracks[0]["uri"])
                    else:
                        not_found.append(track)
            except:
                not_found.append(track)

        # Create playlist on Spotify
        spotify_user = sp.current_user()
        new_playlist = sp.user_playlist_create(
            spotify_user["id"],
            playlist_name or tidal_playlist.name or "Migrated from Tidal",
            public=False,
            description="Migrated from Tidal"
        )

        # Add tracks to playlist (Spotify allows max 100 per request)
        if spotify_uris:
            for i in range(0, len(spotify_uris), 100):
                batch = spotify_uris[i:i+100]
                sp.playlist_add_items(new_playlist["id"], batch)

        # Track migration in database
        if user_id:
            try:
                from datetime import datetime
                migration = Migration(
                    user_id=user_id,
                    source_provider='tidal',
                    target_provider='spotify',
                    source_playlist_id=playlist_id,
                    source_playlist_name=tidal_playlist.name,
                    target_playlist_id=new_playlist["id"],
                    target_playlist_name=new_playlist["name"],
                    migration_type='playlist',
                    total_tracks=len(source_tracks),
                    migrated_tracks=len(spotify_uris),
                    skipped_tracks=len(not_found),
                    not_found_tracks=not_found[:10],
                    status='completed',
                    completed_at=datetime.utcnow()
                )
                db.session.add(migration)
                increment_usage(user_id, 'migration', len(spotify_uris))
                db.session.commit()
                print(f"[MIGRATE] Tracked Tidal->Spotify migration for user {user_id}: {len(spotify_uris)} tracks")
            except Exception as e:
                print(f"[MIGRATE] Failed to track migration: {e}")

        return jsonify({
            "success": True,
            "playlist_id": new_playlist["id"],
            "playlist_name": new_playlist["name"],
            "total_tracks": len(source_tracks),
            "migrated": len(spotify_uris),
            "not_found": len(not_found),
            "not_found_tracks": not_found[:10]
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/migrate_tidal_tracks", methods=["POST", "OPTIONS"])
def migrate_tidal_tracks():
    if request.method == "OPTIONS":
        return "", 200
    """Migrate selected tracks from Tidal to Spotify."""
    data = request.get_json()
    spotify_code = data.get("spotify_code")
    tidal_session_id = data.get("tidal_session_id")
    tracks = data.get("tracks", [])  # List of {name, artist, album} objects
    playlist_name = data.get("playlist_name", "Migrated from Tidal")
    target_playlist_id = data.get("target_playlist_id")  # Existing Spotify playlist ID
    add_to_liked = data.get("add_to_liked", False)  # Add to Spotify liked songs

    if not tidal_session_id or tidal_session_id not in tidal_sessions:
        return jsonify({"error": "Tidal authorization required"}), 400
    if not tracks:
        return jsonify({"error": "No tracks provided"}), 400

    # Get user for tracking
    user_id = None
    try:
        sp, _ = get_spotify_client(spotify_code)
        spotify_user = sp.current_user()
        identity = UserIdentity.query.filter_by(
            provider='spotify',
            provider_user_id=spotify_user["id"]
        ).first()
        if identity:
            user_id = identity.user_id
    except Exception as e:
        print(f"[MIGRATE] Could not identify user: {e}")

    try:
        sp, _ = get_spotify_client(spotify_code)

        # Search for tracks on Spotify
        spotify_uris = []
        spotify_ids = []
        not_found = []

        for track in tracks:
            query = f"track:{track['name']} artist:{track['artist']}"
            try:
                results = sp.search(q=query, type='track', limit=1)
                found_tracks = results.get("tracks", {}).get("items", [])
                if found_tracks:
                    spotify_uris.append(found_tracks[0]["uri"])
                    spotify_ids.append(found_tracks[0]["id"])
                else:
                    # Try simpler search
                    simple_query = f"{track['name']} {track['artist']}"
                    results = sp.search(q=simple_query, type='track', limit=1)
                    found_tracks = results.get("tracks", {}).get("items", [])
                    if found_tracks:
                        spotify_uris.append(found_tracks[0]["uri"])
                        spotify_ids.append(found_tracks[0]["id"])
                    else:
                        not_found.append(track)
            except:
                not_found.append(track)

        result_playlist_name = ""
        result_playlist_id = None

        if add_to_liked:
            # Add tracks to Spotify liked songs
            if spotify_ids:
                for i in range(0, len(spotify_ids), 50):
                    batch = spotify_ids[i:i+50]
                    sp.current_user_saved_tracks_add(batch)
            result_playlist_name = "Liked Songs"
        elif target_playlist_id:
            # Add to existing Spotify playlist
            if spotify_uris:
                for i in range(0, len(spotify_uris), 100):
                    batch = spotify_uris[i:i+100]
                    sp.playlist_add_items(target_playlist_id, batch)
            # Get playlist name
            playlist = sp.playlist(target_playlist_id)
            result_playlist_name = playlist["name"]
            result_playlist_id = target_playlist_id
        else:
            # Create new playlist on Spotify
            spotify_user = sp.current_user()
            new_playlist = sp.user_playlist_create(
                spotify_user["id"],
                playlist_name,
                public=False,
                description="Migrated from Tidal"
            )
            if spotify_uris:
                for i in range(0, len(spotify_uris), 100):
                    batch = spotify_uris[i:i+100]
                    sp.playlist_add_items(new_playlist["id"], batch)
            result_playlist_name = new_playlist["name"]
            result_playlist_id = new_playlist["id"]

        # Track migration in database
        if user_id:
            try:
                from datetime import datetime
                migration = Migration(
                    user_id=user_id,
                    source_provider='tidal',
                    target_provider='spotify',
                    source_playlist_name='Selected Tracks',
                    target_playlist_name=result_playlist_name,
                    migration_type='tracks' if not add_to_liked else 'liked',
                    total_tracks=len(tracks),
                    migrated_tracks=len(spotify_uris),
                    skipped_tracks=len(not_found),
                    not_found_tracks=not_found[:10],
                    status='completed',
                    completed_at=datetime.utcnow()
                )
                db.session.add(migration)
                increment_usage(user_id, 'migration', len(spotify_uris))
                db.session.commit()
                print(f"[MIGRATE] Tracked Tidal->Spotify tracks migration for user {user_id}: {len(spotify_uris)} tracks")
            except Exception as e:
                print(f"[MIGRATE] Failed to track migration: {e}")

        return jsonify({
            "success": True,
            "playlist_id": result_playlist_id,
            "playlist_name": result_playlist_name,
            "total_tracks": len(tracks),
            "migrated": len(spotify_uris),
            "not_found": len(not_found),
            "not_found_tracks": not_found[:10]
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/migrate_tracks", methods=["POST", "OPTIONS"])
def migrate_tracks():
    if request.method == "OPTIONS":
        return "", 200
    """Migrate selected tracks from Spotify to Tidal."""
    data = request.get_json()
    spotify_code = data.get("spotify_code")
    tidal_session_id = data.get("tidal_session_id")
    tracks = data.get("tracks", [])  # List of {name, artist, album} objects
    playlist_name = data.get("playlist_name", "Migrated Songs")
    target_playlist_id = data.get("target_playlist_id")  # Existing playlist ID (optional)
    add_to_favorites = data.get("add_to_favorites", False)  # Add to Tidal favorites

    if not tidal_session_id or tidal_session_id not in tidal_sessions:
        return jsonify({"error": "Tidal authorization required"}), 400
    if not tracks:
        return jsonify({"error": "No tracks provided"}), 400

    # Get user for tracking
    user_id = None
    try:
        sp, _ = get_spotify_client(spotify_code)
        spotify_user = sp.current_user()
        identity = UserIdentity.query.filter_by(
            provider='spotify',
            provider_user_id=spotify_user["id"]
        ).first()
        if identity:
            user_id = identity.user_id
    except Exception as e:
        print(f"[MIGRATE] Could not identify user: {e}")

    try:
        tidal_session = tidal_sessions[tidal_session_id]["session"]

        # Search for tracks on Tidal and collect IDs
        tidal_track_ids = []
        not_found = []

        for track in tracks:
            query = f"{track['name']} {track['artist']}"
            try:
                results = tidal_session.search(query, models=[tidalapi.media.Track], limit=1)
                found_tracks = results.get("tracks", [])
                if found_tracks:
                    tidal_track_ids.append(found_tracks[0].id)
                else:
                    not_found.append(track)
            except:
                not_found.append(track)

        result_playlist_name = ""
        result_playlist_id = None

        if add_to_favorites:
            # Add tracks to Tidal favorites/collection
            for track_id in tidal_track_ids:
                try:
                    tidal_session.user.favorites.add_track(track_id)
                except Exception as e:
                    print(f"Failed to add track {track_id} to favorites: {e}")
            result_playlist_name = "Favorites"
        elif target_playlist_id:
            # Add to existing playlist
            playlist = tidal_session.playlist(target_playlist_id)
            if tidal_track_ids:
                playlist.add(tidal_track_ids)
            result_playlist_name = playlist.name
            result_playlist_id = playlist.id
        else:
            # Create new playlist on Tidal
            description = f"Migrated from Spotify"
            playlist = tidal_session.user.create_playlist(playlist_name, description)
            if tidal_track_ids:
                playlist.add(tidal_track_ids)
            result_playlist_name = playlist.name
            result_playlist_id = playlist.id

        # Track migration in database
        if user_id:
            try:
                from datetime import datetime
                migration = Migration(
                    user_id=user_id,
                    source_provider='spotify',
                    target_provider='tidal',
                    source_playlist_name='Selected Tracks',
                    target_playlist_name=result_playlist_name,
                    migration_type='tracks' if not add_to_favorites else 'favorites',
                    total_tracks=len(tracks),
                    migrated_tracks=len(tidal_track_ids),
                    skipped_tracks=len(not_found),
                    not_found_tracks=not_found[:10],
                    status='completed',
                    completed_at=datetime.utcnow()
                )
                db.session.add(migration)

                # Increment usage counter
                increment_usage(user_id, 'migration', len(tidal_track_ids))

                db.session.commit()
                print(f"[MIGRATE] Tracked migration for user {user_id}: {len(tidal_track_ids)} tracks")
            except Exception as e:
                print(f"[MIGRATE] Failed to track migration: {e}")

        return jsonify({
            "success": True,
            "playlist_id": result_playlist_id,
            "playlist_name": result_playlist_name,
            "total_tracks": len(tracks),
            "migrated": len(tidal_track_ids),
            "not_found": len(not_found),
            "not_found_tracks": not_found[:10]  # Return first 10 not found for reference
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/migrate_playlist", methods=["POST"])
def migrate_playlist():
    """Migrate a playlist from Spotify to Tidal."""
    data = request.get_json()
    spotify_code = data.get("spotify_code")
    tidal_session_id = data.get("tidal_session_id")
    playlist_id = data.get("playlist_id")
    playlist_name = data.get("playlist_name")

    if not tidal_session_id or tidal_session_id not in tidal_sessions:
        return jsonify({"error": "Tidal authorization required"}), 400
    if not playlist_id:
        return jsonify({"error": "Playlist ID required"}), 400

    # Get user for tracking
    user_id = None

    try:
        # Get Spotify tracks
        sp, _ = get_spotify_client(spotify_code)
        tidal_session = tidal_sessions[tidal_session_id]["session"]

        # Get user ID for tracking
        try:
            spotify_user = sp.current_user()
            identity = UserIdentity.query.filter_by(
                provider='spotify',
                provider_user_id=spotify_user["id"]
            ).first()
            if identity:
                user_id = identity.user_id
        except Exception as e:
            print(f"[MIGRATE] Could not identify user: {e}")

        # Fetch all tracks from Spotify playlist
        spotify_tracks = []
        offset = 0
        while True:
            results = sp.playlist_tracks(playlist_id, offset=offset, limit=100)
            for item in results["items"]:
                track = item.get("track")
                if track:
                    spotify_tracks.append({
                        "name": track["name"],
                        "artist": track["artists"][0]["name"] if track["artists"] else "",
                        "album": track["album"]["name"]
                    })
            if results.get("next"):
                offset += 100
            else:
                break

        # Search for tracks on Tidal and collect IDs
        tidal_track_ids = []
        not_found = []

        for track in spotify_tracks:
            query = f"{track['name']} {track['artist']}"
            try:
                results = tidal_session.search(query, models=[tidalapi.media.Track], limit=1)
                tracks = results.get("tracks", [])
                if tracks:
                    tidal_track_ids.append(tracks[0].id)
                else:
                    not_found.append(track)
            except:
                not_found.append(track)

        # Create playlist on Tidal
        description = f"Migrated from Spotify"
        playlist = tidal_session.user.create_playlist(playlist_name or "Migrated Playlist", description)

        # Add tracks to playlist
        if tidal_track_ids:
            playlist.add(tidal_track_ids)

        # Track migration in database
        if user_id:
            try:
                from datetime import datetime
                migration = Migration(
                    user_id=user_id,
                    source_provider='spotify',
                    target_provider='tidal',
                    source_playlist_id=playlist_id,
                    source_playlist_name=playlist_name,
                    target_playlist_id=str(playlist.id),
                    target_playlist_name=playlist.name,
                    migration_type='playlist',
                    total_tracks=len(spotify_tracks),
                    migrated_tracks=len(tidal_track_ids),
                    skipped_tracks=len(not_found),
                    not_found_tracks=not_found[:10],
                    status='completed',
                    completed_at=datetime.utcnow()
                )
                db.session.add(migration)

                # Increment usage counter
                increment_usage(user_id, 'migration', len(tidal_track_ids))

                db.session.commit()
                print(f"[MIGRATE] Tracked playlist migration for user {user_id}: {len(tidal_track_ids)} tracks")
            except Exception as e:
                print(f"[MIGRATE] Failed to track migration: {e}")

        return jsonify({
            "success": True,
            "playlist_id": playlist.id,
            "playlist_name": playlist.name,
            "total_tracks": len(spotify_tracks),
            "migrated": len(tidal_track_ids),
            "not_found": len(not_found),
            "not_found_tracks": not_found[:10]  # Return first 10 not found for reference
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== ARCHAEOLOGIST ====================

# Per-tier daily limits for snapshot rebuilds (heavy: 30-80 Spotify API calls each)
ARCH_REFRESH_LIMITS = {"free": 1, "plus": 4, "pro": 1000}


def _load_snapshot_for_user(user: User) -> dict:
    return decode_blob(user.archaeologist_snapshot) if user.archaeologist_snapshot else {}


def _snapshot_status_dict(user: User) -> dict:
    built_at = user.archaeologist_snapshot_built_at
    has_snapshot = bool(user.archaeologist_snapshot)
    return {
        "has_snapshot": has_snapshot,
        "built_at": built_at.isoformat() if built_at else None,
        "tier": user.tier or "free",
        "daily_limit": ARCH_REFRESH_LIMITS.get(user.tier or "free", ARCH_REFRESH_LIMITS["free"]),
    }


@app.route("/archaeologist/status", methods=["POST"])
def archaeologist_status():
    """Return whether the user has a snapshot, when it was built, and quota remaining."""
    user = g.user
    limit = ARCH_REFRESH_LIMITS.get(user.tier or "free", ARCH_REFRESH_LIMITS["free"])
    _allowed, remaining = check_rate_limit(user.id, "archaeologist_refresh", daily_limit=limit)
    return jsonify({**_snapshot_status_dict(user), "refresh_remaining": remaining})


@app.route("/archaeologist/snapshot/refresh", methods=["POST"])
def archaeologist_refresh():
    """Build a fresh snapshot. Heavy — gated by daily per-tier quota."""
    user = g.user
    try:
        sp, _ = get_spotify_client()
    except Exception as e:
        return jsonify({"error": f"Could not get Spotify client: {e}"}), 500

    limit = ARCH_REFRESH_LIMITS.get(user.tier or "free", ARCH_REFRESH_LIMITS["free"])
    allowed, remaining = check_rate_limit(user.id, "archaeologist_refresh", daily_limit=limit)
    if not allowed:
        return jsonify({
            "error": "Daily snapshot refresh limit reached",
            "tier": user.tier,
            "limit": limit,
            "refresh_remaining": 0,
            "built_at": user.archaeologist_snapshot_built_at.isoformat() if user.archaeologist_snapshot_built_at else None,
        }), 429

    try:
        from datetime import datetime
        progress_log: list[str] = []

        def _log(stage, detail):
            progress_log.append(f"{stage}: {detail}")

        snapshot = build_snapshot(sp, progress=_log)
        user.archaeologist_snapshot = encode_blob(snapshot)
        user.archaeologist_snapshot_built_at = datetime.utcnow()
        db.session.commit()
        increment_usage(user.id, "archaeologist_refresh", tracks_count=snapshot["totals"].get("playlist_tracks", 0))
        return jsonify({
            "success": True,
            "totals": snapshot["totals"],
            "truncated": snapshot.get("truncated", []),
            "built_at": user.archaeologist_snapshot_built_at.isoformat(),
            "refresh_remaining": max(0, remaining - 1),
            "tier": user.tier,
            "daily_limit": limit,
        })
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _ensure_snapshot():
    """Helper for read endpoints: return (snapshot_dict, error_response_tuple) for g.user."""
    snap = _load_snapshot_for_user(g.user)
    if not snap:
        return None, (jsonify({"error": "No snapshot yet — call /archaeologist/snapshot/refresh first"}), 404)
    return snap, None


@app.route("/archaeologist/overview", methods=["POST"])
def archaeologist_overview():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_analytics.collection_overview(snap))


@app.route("/archaeologist/playlist_overlap", methods=["POST"])
def archaeologist_playlist_overlap():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_analytics.playlist_overlap(snap, top_n=data.get("top_n", 20)))


@app.route("/archaeologist/artist_dominance", methods=["POST"])
def archaeologist_artist_dominance():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_analytics.artist_dominance(snap, top_n=data.get("top_n", 50)))


@app.route("/archaeologist/forgotten", methods=["POST"])
def archaeologist_forgotten():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_analytics.forgotten_songs(
        snap,
        top_n=data.get("top_n", 50),
        months_threshold=data.get("months_threshold", 12),
    ))


@app.route("/archaeologist/playlist_health", methods=["POST"])
def archaeologist_playlist_health():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_analytics.playlist_health(snap))


@app.route("/archaeologist/evolution", methods=["POST"])
def archaeologist_evolution():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_analytics.listening_evolution(snap))


# ---- Phase 2: Library-internal discovery ----

@app.route("/archaeologist/genre_clusters", methods=["POST"])
def archaeologist_genre_clusters():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_discovery.genre_clusters(
        snap,
        min_artists=data.get("min_artists", 3),
        max_clusters=data.get("max_clusters", 12),
    ))


@app.route("/archaeologist/genre_outliers", methods=["POST"])
def archaeologist_genre_outliers():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_discovery.genre_outliers(snap, top_n=data.get("top_n", 30)))


@app.route("/archaeologist/hidden_gems", methods=["POST"])
def archaeologist_hidden_gems():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_discovery.hidden_gems(snap, top_n=data.get("top_n", 50)))


@app.route("/archaeologist/co_occurrence", methods=["POST"])
def archaeologist_co_occurrence():
    data = request.get_json() or {}
    snap, err = _ensure_snapshot()
    if err:
        return err
    return jsonify(arch_discovery.artist_co_occurrence(
        snap,
        min_playlists=data.get("min_playlists", 3),
        top_n=data.get("top_n", 20),
    ))


# Create database tables on startup
with app.app_context():
    try:
        db.create_all()
        print("[DB] Database tables created successfully")
    except Exception as e:
        print(f"[DB] Warning: Could not create tables: {e}")

    # Idempotent column adds — db.create_all() doesn't alter existing tables.
    # Wrapped so prod (Postgres) and dev (SQLite) both survive missing-column states.
    _column_adds = [
        ("users", "archaeologist_snapshot", "TEXT"),
        ("users", "archaeologist_snapshot_built_at", "TIMESTAMP"),
        # Session-auth refactor: OAuth token storage on identities.
        ("user_identities", "access_token", "TEXT"),
        ("user_identities", "refresh_token", "TEXT"),
        ("user_identities", "token_expires_at", "TIMESTAMP"),
        ("user_identities", "token_scope", "TEXT"),
    ]
    for table, col, coltype in _column_adds:
        try:
            db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            # SQLite lacks ADD COLUMN IF NOT EXISTS; fall back to a check + plain ADD.
            try:
                exists = db.session.execute(
                    db.text(f"SELECT 1 FROM pragma_table_info('{table}') WHERE name='{col}'")
                ).fetchone()
                if not exists:
                    db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                    db.session.commit()
            except Exception as inner:
                db.session.rollback()
                print(f"[DB] Could not ensure column {table}.{col}: {e} / {inner}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
