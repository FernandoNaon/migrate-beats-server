import base64
import json
import os
from pathlib import Path
from urllib.parse import urlparse
import uuid

from flask import Flask, request, redirect, session, jsonify
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from dotenv import load_dotenv
import tidalapi

load_dotenv()

# Import database
from config import get_config
from models import db, User, UserIdentity, UserActivity, Migration, ApiUsage
from models import get_or_create_user, log_activity, check_rate_limit, increment_usage

app = Flask(__name__)

# Load configuration (includes DATABASE_URL)
app.config.from_object(get_config())

# Initialize database
db.init_app(app)

# CORS Configuration - support both local and production
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
FRONTEND_REDIRECT = os.environ.get("FRONTEND_REDIRECT", "http://localhost:5173/callback")


def origin_from_url(url):
    if not url:
        return None

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None

    return f"{parsed.scheme}://{parsed.netloc}"


def build_allowed_origins():
    configured_values = []
    for raw_value in [FRONTEND_URL, FRONTEND_REDIRECT]:
        if not raw_value:
            continue
        configured_values.extend(part.strip() for part in raw_value.split(",") if part.strip())

    local_dev_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://0.0.0.0:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://0.0.0.0:5173",
    ]

    allowed = []
    for value in configured_values + local_dev_origins:
        origin = origin_from_url(value)
        if origin and origin not in allowed:
            allowed.append(origin)

    return allowed


CORS(app,
     supports_credentials=True,
     origins=build_allowed_origins(),
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

# Store pending Tidal sessions in memory and completed sessions on disk.
tidal_sessions = {}
TIDAL_SESSION_DIR = Path(app.config.get("TIDAL_SESSION_DIR", ".tidal-sessions"))
TIDAL_SESSION_DIR.mkdir(parents=True, exist_ok=True)


def serialize_tidal_user(user):
    return {
        "id": str(user.id) if user else "unknown",
        "name": getattr(user, "name", None) or getattr(user, "first_name", None) or (str(user.id) if user else "Tidal User"),
    }


def get_tidal_session_path(session_id):
    return TIDAL_SESSION_DIR / f"{session_id}.json"


def save_tidal_session(session_id, tidal_session):
    tidal_session.save_session_to_file(get_tidal_session_path(session_id))


def remove_tidal_session(session_id):
    tidal_sessions.pop(session_id, None)
    session_path = get_tidal_session_path(session_id)
    if session_path.exists():
        session_path.unlink()


def load_persisted_tidal_session(session_id):
    session_path = get_tidal_session_path(session_id)
    if not session_path.exists():
        return None

    tidal_session = tidalapi.Session()
    if not tidal_session.load_session_from_file(session_path):
        session_path.unlink(missing_ok=True)
        return None

    if not tidal_session.check_login():
        session_path.unlink(missing_ok=True)
        return None

    return tidal_session


def get_tidal_session_data(session_id):
    if not session_id:
        return None

    tidal_data = tidal_sessions.get(session_id)
    if tidal_data and tidal_data.get("future") is not None:
        return tidal_data

    if tidal_data and tidal_data.get("session") is not None:
        try:
            if tidal_data["session"].check_login():
                return tidal_data
        except Exception:
            tidal_sessions.pop(session_id, None)

    persisted_session = load_persisted_tidal_session(session_id)
    if not persisted_session:
        return None

    tidal_data = {
        "session": persisted_session,
        "user": serialize_tidal_user(persisted_session.user),
    }
    tidal_sessions[session_id] = tidal_data
    return tidal_data


def encode_client_state(client_redirect_uri):
    if not client_redirect_uri:
        return None

    payload = json.dumps({"client_redirect_uri": client_redirect_uri}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("utf-8").rstrip("=")


def decode_client_state(state_value):
    if not state_value:
        return None

    padding = "=" * (-len(state_value) % 4)
    payload = base64.urlsafe_b64decode(f"{state_value}{padding}")
    return json.loads(payload.decode("utf-8"))


def is_allowed_client_redirect(client_redirect_uri):
    allowed_prefixes = [
        FRONTEND_URL,
        FRONTEND_REDIRECT,
        "http://localhost",
        "http://127.0.0.1",
        "playlistmover://",
    ]
    return any(client_redirect_uri.startswith(prefix) for prefix in allowed_prefixes)


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
    """Register or get current user from Spotify auth code."""
    data = request.get_json()
    code = data.get("code")

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

    try:
        sp, _ = get_spotify_client(code)
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
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
            "is_active": user.is_active,
            "usage": {
                "migrations_today": migrations_today,
                "migrations_limit": rate_limit,
            },
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
    data = request.get_json()
    code = data.get("code")
    limit = data.get("limit", 20)

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

    try:
        sp, _ = get_spotify_client(code)
        spotify_user = sp.current_user()

        # Find user by Spotify identity
        identity = UserIdentity.query.filter_by(
            provider="spotify",
            provider_user_id=spotify_user["id"]
        ).first()

        if not identity:
            return jsonify([])  # No user found, return empty history

        # Get migrations for this user
        migrations = Migration.query.filter_by(user_id=identity.user_id)\
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


# Extended scopes for dashboard insights
SCOPE = "playlist-read-private playlist-read-collaborative user-top-read user-read-recently-played user-library-read user-read-private user-follow-read"


def get_spotify_oauth():
    return SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPE
    )


def get_spotify_client(code):
    """Helper to get authenticated Spotify client from auth code."""
    sp_oauth = get_spotify_oauth()
    token_info = sp_oauth.get_access_token(code, as_dict=True)
    return spotipy.Spotify(auth=token_info['access_token']), token_info


# ==================== SPOTIFY AUTH ====================

@app.route("/login", methods=["GET"])
def login():
    client_redirect_uri = request.args.get("client_redirect_uri")
    state = None

    if client_redirect_uri:
        if not is_allowed_client_redirect(client_redirect_uri):
            return jsonify({"error": "Unsupported client redirect URI"}), 400
        state = encode_client_state(client_redirect_uri)

    auth_url = get_spotify_oauth().get_authorize_url(state=state)
    return jsonify({"auth_url": auth_url})


@app.route("/callback")
def spotify_callback_redirect():
    code = request.args.get("code")
    state = request.args.get("state")
    redirect_target = FRONTEND_REDIRECT

    if state:
        try:
            client_state = decode_client_state(state)
            client_redirect_uri = client_state.get("client_redirect_uri")
            if client_redirect_uri and is_allowed_client_redirect(client_redirect_uri):
                redirect_target = client_redirect_uri
        except Exception as e:
            print(f"[SPOTIFY] Invalid callback state: {e}")

    separator = "&" if "?" in redirect_target else "?"
    return redirect(f"{redirect_target}{separator}code={code}")


# ==================== SPOTIFY PLAYLISTS ====================

@app.route("/fetch_playlists", methods=["POST"])
def fetch_playlists():
    data = request.get_json()
    code = data.get("code")

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

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
    if not code:
        return jsonify({"error": "Authorization code required"}), 400

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
                        "name": track["name"],
                        "artist": ", ".join([artist["name"] for artist in track["artists"]]),
                        "artists": [artist["name"] for artist in track["artists"]],
                        "album": track["album"]["name"],
                        "duration_ms": track["duration_ms"],
                        "image": track["album"]["images"][0]["url"] if track["album"].get("images") else None
                    })

            if results.get("next"):
                offset += limit
            else:
                break

        return jsonify(tracks)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== SPOTIFY INSIGHTS/DASHBOARD ====================

@app.route("/user_profile", methods=["POST"])
def user_profile():
    data = request.get_json()
    code = data.get("code")

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

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

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

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

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

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

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

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

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

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


@app.route("/library_stats", methods=["POST"])
def library_stats():
    data = request.get_json()
    code = data.get("code")

    if not code:
        return jsonify({"error": "Authorization code required"}), 400

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

    if not session_id:
        return jsonify({"authenticated": False, "error": "Invalid session"}), 200

    try:
        tidal_data = get_tidal_session_data(session_id)
        if not tidal_data:
            print(f"[Tidal] Invalid session: {session_id}")
            return jsonify({"authenticated": False, "error": "Invalid session"}), 200

        tidal_session = tidal_data["session"]
        future = tidal_data.get("future")

        if future is None:
            if tidal_session.check_login():
                user = tidal_session.user
                return jsonify({
                    "authenticated": True,
                    "user": serialize_tidal_user(user)
                })

            remove_tidal_session(session_id)
            return jsonify({"authenticated": False, "error": "Session expired"}), 200

        print(f"[Tidal] Checking auth - future.done(): {future.done()}")

        # Check if the future is done (user completed login)
        if future.done():
            try:
                future.result()  # This will raise if there was an error
                # Authorization successful
                user = tidal_session.user
                save_tidal_session(session_id, tidal_session)
                tidal_sessions[session_id] = {
                    "session": tidal_session,
                    "future": None,
                    "login": None,
                    "user": serialize_tidal_user(user),
                }
                print(f"[Tidal] Auth successful! User: {user}")
                return jsonify({
                    "authenticated": True,
                    "user": serialize_tidal_user(user)
                })
            except Exception as e:
                print(f"[Tidal] Future result error: {e}")
                remove_tidal_session(session_id)
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

    tidal_data = get_tidal_session_data(session_id)
    if not tidal_data:
        return jsonify({"error": "Invalid session"}), 400

    try:
        tidal_session = tidal_data["session"]
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

    tidal_data = get_tidal_session_data(session_id)
    if not tidal_data:
        return jsonify({"error": "Invalid session"}), 400
    if not playlist_id:
        return jsonify({"error": "Playlist ID required"}), 400

    try:
        tidal_session = tidal_data["session"]
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

    tidal_data = get_tidal_session_data(session_id)
    if not tidal_data:
        return jsonify({"error": "Invalid session"}), 400
    if not playlist_id:
        return jsonify({"error": "Playlist ID required"}), 400

    try:
        tidal_session = tidal_data["session"]
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

    tidal_data = get_tidal_session_data(session_id)
    if not tidal_data:
        return jsonify({"error": "Invalid session"}), 400
    if not source_playlist_id or not target_playlist_id:
        return jsonify({"error": "Both source and target playlist IDs required"}), 400
    if source_playlist_id == target_playlist_id:
        return jsonify({"error": "Cannot merge a playlist with itself"}), 400

    try:
        tidal_session = tidal_data["session"]

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

    tidal_data = get_tidal_session_data(session_id)
    if not tidal_data:
        return jsonify({"error": "Invalid session"}), 400
    if not query:
        return jsonify({"error": "Query required"}), 400

    try:
        tidal_session = tidal_data["session"]
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

    tidal_data = get_tidal_session_data(session_id)
    if not tidal_data:
        return jsonify({"error": "Invalid session"}), 400
    if not name:
        return jsonify({"error": "Playlist name required"}), 400

    try:
        tidal_session = tidal_data["session"]

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

    tidal_data = get_tidal_session_data(session_id)
    if not tidal_data:
        return jsonify({"error": "Invalid session"}), 400

    try:
        tidal_session = tidal_data["session"]

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

        # tidalapi does not expose the total count consistently, so report the
        # minimum known total for the current window.
        total = offset + len(favorite_tracks)
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

    if not spotify_code:
        return jsonify({"error": "Spotify authorization required"}), 400
    tidal_data = get_tidal_session_data(tidal_session_id)
    if not tidal_data:
        return jsonify({"error": "Tidal authorization required"}), 400
    if not playlist_id:
        return jsonify({"error": "Playlist ID required"}), 400

    # Get user for tracking
    user_id = None

    try:
        # Get clients
        sp, _ = get_spotify_client(spotify_code)
        tidal_session = tidal_data["session"]

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

    if not spotify_code:
        return jsonify({"error": "Spotify authorization required"}), 400
    tidal_data = get_tidal_session_data(tidal_session_id)
    if not tidal_data:
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

    if not spotify_code:
        return jsonify({"error": "Spotify authorization required"}), 400
    tidal_data = get_tidal_session_data(tidal_session_id)
    if not tidal_data:
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
        tidal_session = tidal_data["session"]

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

    if not spotify_code:
        return jsonify({"error": "Spotify authorization required"}), 400
    tidal_data = get_tidal_session_data(tidal_session_id)
    if not tidal_data:
        return jsonify({"error": "Tidal authorization required"}), 400
    if not playlist_id:
        return jsonify({"error": "Playlist ID required"}), 400

    # Get user for tracking
    user_id = None

    try:
        # Get Spotify tracks
        sp, _ = get_spotify_client(spotify_code)
        tidal_session = tidal_data["session"]

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


# Create database tables on startup
with app.app_context():
    try:
        db.create_all()
        print("[DB] Database tables created successfully")
    except Exception as e:
        print(f"[DB] Warning: Could not create tables: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
