"""Database URLs from the environment.

Locally a full URL is passed (DATABASE_URL). On AWS, ECS injects the host and
the credentials separately from Secrets Manager, and RDS generated passwords
can contain characters that must be escaped in a URL, so the URL is built here.
"""

from __future__ import annotations

import os
from urllib.parse import quote_plus


def _from_parts(user_var: str, password_var: str) -> str | None:
    host = os.environ.get("DB_HOST")
    if not host or user_var not in os.environ or password_var not in os.environ:
        return None
    user = quote_plus(os.environ[user_var])
    password = quote_plus(os.environ[password_var])
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "fraud")
    sslmode = os.environ.get("DB_SSLMODE", "require")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"


def app_database_url() -> str | None:
    """Connection for the API, monitor and auditor (INSERT and SELECT only)."""
    return os.environ.get("DATABASE_URL") or _from_parts("DB_USER", "DB_PASSWORD")


def admin_database_url() -> str | None:
    """Connection for the migration job only."""
    return os.environ.get("DATABASE_ADMIN_URL") or _from_parts("DB_ADMIN_USER", "DB_ADMIN_PASSWORD")
