import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATABASE_URL"] = "sqlite://"

import app as app_module
from models import Migration, User, UserIdentity, db


class FakeSpotifyClient:
    def current_user(self):
        return {"id": "spotify-user-1"}


class FakeTrack:
    def __init__(self, track_id):
        self.id = track_id


class FakePlaylist:
    def __init__(self, playlist_id, tracks=None):
        self.id = playlist_id
        self.name = f"Playlist {playlist_id}"
        self._tracks = tracks or []
        self.deleted = False
        self.added_batches = []

    def tracks(self):
        return self._tracks

    def add(self, track_ids):
        self.added_batches.append(list(track_ids))

    def delete(self):
        self.deleted = True


class FakeTidalSession:
    def __init__(self, playlists=None):
        self.user = SimpleNamespace(id=42, name="Tidal Tester")
        self._playlists = playlists or {}

    def check_login(self):
        return True

    def playlist(self, playlist_id):
        return self._playlists[playlist_id]


@pytest.fixture
def client(tmp_path):
    app_module.app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite://",
        TIDAL_SESSION_DIR=str(tmp_path / "tidal-sessions"),
    )
    app_module.TIDAL_SESSION_DIR = tmp_path / "tidal-sessions"
    app_module.TIDAL_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    app_module.tidal_sessions.clear()

    with app_module.app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()

    with app_module.app.test_client() as test_client:
        yield test_client

    with app_module.app.app_context():
        db.session.remove()
        db.drop_all()

    app_module.tidal_sessions.clear()


def test_user_history_returns_existing_migrations(client, monkeypatch):
    with app_module.app.app_context():
        user = User(email="test@example.com", display_name="Test User")
        db.session.add(user)
        db.session.flush()
        db.session.add(
            UserIdentity(
                user_id=user.id,
                provider="spotify",
                provider_user_id="spotify-user-1",
            )
        )
        migration = Migration(
            user_id=user.id,
            source_provider="spotify",
            target_provider="tidal",
            source_playlist_name="Road Trip",
            target_playlist_name="Road Trip Copy",
            migration_type="playlist",
            total_tracks=12,
            migrated_tracks=10,
            skipped_tracks=2,
            status="completed",
        )
        db.session.add(migration)
        db.session.commit()

    monkeypatch.setattr(
        app_module,
        "get_spotify_client",
        lambda code: (FakeSpotifyClient(), None),
    )

    response = client.post("/user/history", json={"code": "valid-code", "limit": 5})

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload) == 1
    assert payload[0]["source_playlist_name"] == "Road Trip"
    assert payload[0]["target_playlist_name"] == "Road Trip Copy"


def test_tidal_check_auth_accepts_persisted_session(client, monkeypatch):
    fake_session = FakeTidalSession()
    monkeypatch.setattr(
        app_module,
        "load_persisted_tidal_session",
        lambda session_id: fake_session,
    )

    response = client.post("/tidal/check_auth", json={"session_id": "persisted-session"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["authenticated"] is True
    assert payload["user"]["name"] == "Tidal Tester"


def test_tidal_delete_playlist_uses_restored_session(client, monkeypatch):
    playlist = FakePlaylist("to-delete")
    fake_session = FakeTidalSession({"to-delete": playlist})
    monkeypatch.setattr(
        app_module,
        "get_tidal_session_data",
        lambda session_id: {"session": fake_session, "user": {"id": "42", "name": "Tidal Tester"}},
    )

    response = client.post(
        "/tidal/delete_playlist",
        json={"session_id": "persisted-session", "playlist_id": "to-delete"},
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert playlist.deleted is True


def test_tidal_merge_playlists_uses_restored_session(client, monkeypatch):
    source = FakePlaylist("source", [FakeTrack("1"), FakeTrack("2")])
    target = FakePlaylist("target", [FakeTrack("2")])
    fake_session = FakeTidalSession({"source": source, "target": target})
    monkeypatch.setattr(
        app_module,
        "get_tidal_session_data",
        lambda session_id: {"session": fake_session, "user": {"id": "42", "name": "Tidal Tester"}},
    )

    response = client.post(
        "/tidal/merge_playlists",
        json={
            "session_id": "persisted-session",
            "source_playlist_id": "source",
            "target_playlist_id": "target",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["tracks_added"] == 1
    assert payload["tracks_skipped"] == 1
    assert payload["source_deleted"] is True
    assert target.added_batches == [["1"]]
    assert source.deleted is True
