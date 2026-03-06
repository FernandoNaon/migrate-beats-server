"""
Configuration for Migrate Beats API.
Supports local development and Railway deployment.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Base configuration."""
    # Flask
    SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'supersecretkey')

    # Database - Railway provides DATABASE_URL automatically
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///migrate_beats.db')

    # Fix for Railway's postgres:// vs postgresql://
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }

    # CORS
    FRONTEND_URL = os.environ.get('FRONTEND_URL', 'http://localhost:5173')
    TIDAL_SESSION_DIR = os.environ.get('TIDAL_SESSION_DIR', '.tidal-sessions')

    # Rate Limits (per day)
    RATE_LIMIT_MIGRATIONS = int(os.environ.get('RATE_LIMIT_MIGRATIONS', 25))


def get_config():
    return Config
