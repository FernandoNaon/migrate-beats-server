# Playlist Mover Server

Flask backend for Playlist Mover. It handles:

- Spotify OAuth
- Spotify library reads
- Tidal device authorization
- playlist migration between Spotify and Tidal
- Tidal playlist merge/delete operations
- lightweight user/activity tracking

## Stack

- Flask
- Spotipy
- tidalapi
- SQLAlchemy
- PostgreSQL on Railway in production
- SQLite for lightweight local development

## Prerequisites

- Python 3.11+
- `pip`
- Spotify developer credentials
- Optional: a local Postgres instance if you want to mirror production

## Environment Variables

Copy `.env.example` to `.env` and fill in the values:

```bash
SPOTIPY_CLIENT_ID=your_spotify_client_id
SPOTIPY_CLIENT_SECRET=your_spotify_client_secret
SPOTIPY_REDIRECT_URI=http://127.0.0.1:5000/callback
FLASK_SECRET_KEY=replace_me
FRONTEND_URL=http://localhost:5173
FRONTEND_REDIRECT=http://localhost:5173/callback
DATABASE_URL=sqlite:///migrate_beats.db
RATE_LIMIT_MIGRATIONS=25
TIDAL_SESSION_DIR=.tidal-sessions
```

`TIDAL_CLIENT_ID` and `TIDAL_CLIENT_SECRET` can be left empty unless you move away from the library-managed device flow.

## Run Locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

The API will run on `http://127.0.0.1:5000`.

## Spotify Setup

In the Spotify developer dashboard:

- set the app redirect URI to `http://127.0.0.1:5000/callback`
- make sure the frontend URL matches your local client

For mobile, the backend now accepts an optional client redirect URI during `/login`, so it can forward the callback back to `playlistmover://callback`.

## Railway Notes

- Railway provides `DATABASE_URL` automatically in production.
- This project now stores completed Tidal OAuth sessions on disk and runs Gunicorn with a single worker to avoid breaking the Tidal device-auth flow during merge/delete operations.

## Useful Commands

```bash
python app.py
pytest
```

## Local Smoke Test

1. Start this server.
2. Start `playlist-mover-client`.
3. Connect Spotify.
4. Connect Tidal.
5. Open playlists and verify merge/delete on a test Tidal playlist.

## Testing

Integration-style endpoint coverage was added for:

- `POST /user/history`
- `POST /tidal/check_auth`
- `POST /tidal/delete_playlist`
- `POST /tidal/merge_playlists`

Run:

```bash
pytest
```
