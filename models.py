"""
Database models for Migrate Beats.
Optimized for free-tier PostgreSQL (minimal storage, JSON caching).
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB
import uuid

db = SQLAlchemy()


def generate_uuid():
    return str(uuid.uuid4())


# ==================== USERS & AUTH ====================

class User(db.Model):
    """Core user table - internal user, linked to Spotify/Tidal identities."""
    __tablename__ = 'users'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    email = db.Column(db.String(255), unique=True, nullable=True)
    display_name = db.Column(db.String(255))
    avatar_url = db.Column(db.Text)
    tier = db.Column(db.String(20), default='free')  # free, plus, pro
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = db.Column(db.DateTime)

    # Archaeologist feature: gzipped+base64 JSON snapshot blob.
    # We use db.Text (not JSONB) because the payload is already compressed bytes-as-text;
    # storing it raw as JSONB would double the storage by re-decoding/parsing on every read.
    archaeologist_snapshot = db.Column(db.Text, nullable=True)
    archaeologist_snapshot_built_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    identities = db.relationship('UserIdentity', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    activities = db.relationship('UserActivity', backref='user', lazy='dynamic', cascade='all, delete-orphan')
    migrations = db.relationship('Migration', backref='user', lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'display_name': self.display_name,
            'avatar_url': self.avatar_url,
            'tier': self.tier,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_login_at': self.last_login_at.isoformat() if self.last_login_at else None,
        }


class UserIdentity(db.Model):
    """Links users to external providers (Spotify, Tidal) and stores their OAuth tokens.

    SECURITY: access_token is short-lived (1h) so stored plaintext; refresh_token is
    long-lived and stored Fernet-encrypted (see crypto.py).
    """
    __tablename__ = 'user_identities'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    provider = db.Column(db.String(20), nullable=False)  # spotify, tidal
    provider_user_id = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # OAuth token storage (added for session-based auth refactor)
    access_token = db.Column(db.Text, nullable=True)            # short-lived; plaintext OK
    refresh_token = db.Column(db.Text, nullable=True)           # long-lived; Fernet-encrypted
    token_expires_at = db.Column(db.DateTime, nullable=True)    # when access_token expires
    token_scope = db.Column(db.Text, nullable=True)             # granted scope string

    __table_args__ = (
        db.UniqueConstraint('provider', 'provider_user_id', name='uix_provider_user'),
    )


class Session(db.Model):
    """Opaque server-side session. The client holds the raw token; we store only its hash.

    SECURITY: this replaces sending the Spotify OAuth code as a de-facto password on every
    request. The raw token never touches the DB — a DB leak can't resurrect live sessions.
    """
    __tablename__ = 'sessions'

    token_hash = db.Column(db.String(64), primary_key=True)  # sha256 hex of the raw token
    user_id = db.Column(db.String(36), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)

    __table_args__ = (
        db.Index('idx_session_user', 'user_id'),
    )


class UserActivity(db.Model):
    """Structured activity log for analytics and debugging."""
    __tablename__ = 'user_activities'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    action = db.Column(db.String(50), nullable=False)  # login, migration, fetch_playlists, etc.
    details = db.Column(JSONB, default={})  # Flexible activity details
    success = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('idx_activity_user_time', 'user_id', 'created_at'),
    )


# ==================== MIGRATIONS ====================

class Migration(db.Model):
    """Tracks migration jobs from Spotify to Tidal."""
    __tablename__ = 'migrations'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    source_provider = db.Column(db.String(20), default='spotify')
    target_provider = db.Column(db.String(20), default='tidal')
    source_playlist_id = db.Column(db.String(255))
    source_playlist_name = db.Column(db.String(255))
    target_playlist_id = db.Column(db.String(255))
    target_playlist_name = db.Column(db.String(255))
    migration_type = db.Column(db.String(20))  # playlist, liked_songs, custom
    total_tracks = db.Column(db.Integer, default=0)
    migrated_tracks = db.Column(db.Integer, default=0)
    skipped_tracks = db.Column(db.Integer, default=0)
    not_found_tracks = db.Column(JSONB, default=[])  # Store first 10 not found
    status = db.Column(db.String(20), default='pending')  # pending, in_progress, completed, failed
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)

    __table_args__ = (
        db.Index('idx_migrations_user', 'user_id'),
        db.Index('idx_migrations_status', 'status'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'source_provider': self.source_provider,
            'target_provider': self.target_provider,
            'source_playlist_name': self.source_playlist_name,
            'target_playlist_name': self.target_playlist_name,
            'migration_type': self.migration_type,
            'total_tracks': self.total_tracks,
            'migrated_tracks': self.migrated_tracks,
            'skipped_tracks': self.skipped_tracks,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


# ==================== API USAGE / RATE LIMITING ====================

class ApiUsage(db.Model):
    """Tracks API usage per user for rate limiting and analytics."""
    __tablename__ = 'api_usage'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    action = db.Column(db.String(50), nullable=False)  # migration, fetch_liked, etc.
    count = db.Column(db.Integer, default=0)
    tracks_count = db.Column(db.Integer, default=0)  # Total tracks processed
    window_start = db.Column(db.Date, nullable=False)  # Daily window

    __table_args__ = (
        db.UniqueConstraint('user_id', 'action', 'window_start', name='uix_user_action_window'),
        db.Index('idx_usage_user_window', 'user_id', 'window_start'),
    )


# ==================== HELPER FUNCTIONS ====================

def get_or_create_user(spotify_user_id: str, email: str = None, display_name: str = None, avatar_url: str = None):
    """Get existing user or create new one from Spotify login."""
    # Check if identity exists
    identity = UserIdentity.query.filter_by(
        provider='spotify',
        provider_user_id=spotify_user_id
    ).first()

    if identity:
        user = identity.user
        # Update last login
        user.last_login_at = datetime.utcnow()
        if display_name:
            user.display_name = display_name
        if avatar_url:
            user.avatar_url = avatar_url
        db.session.commit()
        return user, False  # user, is_new

    # Create new user
    user = User(
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
        last_login_at=datetime.utcnow()
    )
    db.session.add(user)
    db.session.flush()  # Get user.id

    # Create identity link
    identity = UserIdentity(
        user_id=user.id,
        provider='spotify',
        provider_user_id=spotify_user_id
    )
    db.session.add(identity)
    db.session.commit()
    return user, True  # user, is_new


def log_activity(user_id: str, action: str, details: dict = None, success: bool = True):
    """Log user activity."""
    activity = UserActivity(
        user_id=user_id,
        action=action,
        details=details or {},
        success=success
    )
    db.session.add(activity)
    db.session.commit()
    return activity


def check_rate_limit(user_id: str, action: str, daily_limit: int = 100) -> tuple:
    """Check if user has exceeded daily rate limit. Returns (allowed, remaining)."""
    from datetime import date
    today = date.today()

    usage = ApiUsage.query.filter_by(
        user_id=user_id,
        action=action,
        window_start=today
    ).first()

    if not usage:
        return True, daily_limit

    remaining = daily_limit - usage.count
    return remaining > 0, max(0, remaining)


def increment_usage(user_id: str, action: str, tracks_count: int = 0):
    """Increment usage counter for rate limiting."""
    from datetime import date
    today = date.today()

    usage = ApiUsage.query.filter_by(
        user_id=user_id,
        action=action,
        window_start=today
    ).first()

    if usage:
        usage.count += 1
        usage.tracks_count += tracks_count
    else:
        usage = ApiUsage(
            user_id=user_id,
            action=action,
            window_start=today,
            count=1,
            tracks_count=tracks_count
        )
        db.session.add(usage)

    db.session.commit()
    return usage
