"""Appwrite connectivity and idempotent schema bootstrap for Bot_CR.

All bot data (members, counters, challenges, modlog, settings) lives in the
club's Appwrite project. This module knows *how* to talk to Appwrite and what
the schema should look like; data/store.py wraps it in async, typed helpers.
"""

import logging

from appwrite.client import Client
from appwrite.enums.databases_index_type import DatabasesIndexType
from appwrite.exception import AppwriteException
from appwrite.services.databases import Databases

import config

LOG = logging.getLogger("bot.appwrite")

# ── Schema definition ──────────────────────────────────────────────────
# Collection id -> (display name, [attributes], [indexes])
COLLECTIONS = {
    "bot_members": (
        "Bot members",
        [
            {"key": "user_id", "type": "string", "size": 32, "required": True},
            {"key": "username", "type": "string", "size": 64},
            {"key": "display_name", "type": "string", "size": 64},
            {"key": "real_name", "type": "string", "size": 128},
            {"key": "birthday", "type": "string", "size": 16},
            {"key": "birthday_full", "type": "string", "size": 16},
            {"key": "joined_at", "type": "datetime"},
            {"key": "verified", "type": "boolean"},
            {"key": "xp", "type": "integer"},
            {"key": "messages", "type": "integer"},
            {"key": "voice_seconds", "type": "float"},
            {"key": "warnings", "type": "integer"},
            {"key": "last_xp_at", "type": "string", "size": 32},
            # Linked club account (Discord -> club account -> role -> cell).
            {"key": "club_id", "type": "string", "size": 64},
            {"key": "club_role", "type": "string", "size": 32},
            {"key": "cell", "type": "string", "size": 64},
            # Notification preferences (1 = enabled).
            {"key": "notify_tasks", "type": "boolean"},
            {"key": "notify_events", "type": "boolean"},
            {"key": "notify_competitions", "type": "boolean"},
            {"key": "notify_announcements", "type": "boolean"},
        ],
        [
            {"key": "uniq_user", "type": "unique", "attributes": ["user_id"]},
            {"key": "by_messages", "type": "key", "attributes": ["messages"]},
            {"key": "by_xp", "type": "key", "attributes": ["xp"]},
        ],
    ),
    "bot_counters": (
        "Bot global counters",
        [
            {"key": "total_messages", "type": "integer", "required": True},
            {"key": "total_voice_seconds", "type": "float", "required": True},
            {"key": "boot_at", "type": "datetime"},
            {"key": "last_flush_at", "type": "datetime"},
        ],
        [],
    ),
    "bot_challenges": (
        "Daily challenges",
        [
            {"key": "date", "type": "string", "size": 16, "required": True},
            {"key": "title", "type": "string", "size": 256, "required": True},
            {"key": "description", "type": "string", "size": 2048},
            {"key": "created_by", "type": "string", "size": 32},
            {"key": "claimed", "type": "string", "array": True},
        ],
        [{"key": "by_date", "type": "key", "attributes": ["date"]}],
    ),
    "bot_modlog": (
        "Moderation log",
        [
            {"key": "action", "type": "string", "size": 32, "required": True},
            {"key": "target_id", "type": "string", "size": 32, "required": True},
            {"key": "target_name", "type": "string", "size": 64},
            {"key": "moderator_id", "type": "string", "size": 32},
            {"key": "moderator_name", "type": "string", "size": 64},
            {"key": "reason", "type": "string", "size": 1024},
            {"key": "created_at", "type": "datetime", "required": True},
        ],
        [{"key": "by_created_at", "type": "key", "attributes": ["created_at"]}],
    ),
    "bot_settings": (
        "Bot settings",
        [{"key": "value", "type": "string", "size": 4096, "required": True}],
        [],
    ),
    "bot_tasks": (
        "Club tasks",
        [
            {"key": "task_id", "type": "string", "size": 16, "required": True},
            {"key": "title", "type": "string", "size": 256, "required": True},
            {"key": "description", "type": "string", "size": 2048},
            {"key": "assignee", "type": "string", "size": 32},
            {"key": "cell", "type": "string", "size": 64},
            {"key": "status", "type": "string", "size": 16},
            {"key": "priority", "type": "string", "size": 8},
            {"key": "due", "type": "string", "size": 16},
            {"key": "created_by", "type": "string", "size": 32},
            {"key": "created_at", "type": "datetime"},
        ],
        [{"key": "by_due", "type": "key", "attributes": ["due"]}],
    ),
    "bot_competitions": (
        "Club competitions",
        [
            {"key": "name", "type": "string", "size": 128, "required": True},
            {"key": "date", "type": "string", "size": 16},
            {"key": "location", "type": "string", "size": 128},
            {"key": "capacity", "type": "integer"},
            {"key": "registered", "type": "string", "array": True},
            {"key": "description", "type": "string", "size": 2048},
            {"key": "created_at", "type": "datetime"},
        ],
        [],
    ),
    "bot_events": (
        "Club events",
        [
            {"key": "title", "type": "string", "size": 128, "required": True},
            {"key": "date", "type": "string", "size": 16},
            {"key": "time", "type": "string", "size": 16},
            {"key": "location", "type": "string", "size": 128},
            {"key": "description", "type": "string", "size": 2048},
            {"key": "attendees", "type": "string", "array": True},
            {"key": "declined", "type": "string", "array": True},
            {"key": "created_by", "type": "string", "size": 32},
            {"key": "created_at", "type": "datetime"},
        ],
        [],
    ),
}


def build_client() -> Client:
    """Build a project-scoped Appwrite client from config."""
    client = Client()
    client.set_endpoint(config.APPWRITE_ENDPOINT)
    client.set_project(config.APPWRITE_PROJECT_ID)
    client.set_key(config.APPWRITE_API_KEY)
    return client


def build_databases() -> Databases:
    """Build the Databases service (not bound to a specific database)."""
    return Databases(build_client())


def is_missing(exc: AppwriteException) -> bool:
    """True when an Appwrite exception is a 404 / not_found."""
    return getattr(exc, "code", None) == 404 or "not_found" in (getattr(exc, "type", "") or "")


def ensure_database(db: Databases) -> None:
    """Create the configured database if it does not exist yet."""
    db_id = config.APPWRITE_DATABASE_ID
    try:
        db.get(db_id)
    except AppwriteException as exc:
        if not is_missing(exc):
            raise
        db.create(db_id, f"Bot_CR data ({db_id})", enabled=True)
        LOG.info("Created database %r", db_id)


def _create_attribute(db: Databases, db_id: str, collection_id: str, spec: dict) -> None:
    """Create a single attribute on an existing collection."""
    key = spec["key"]
    kind = spec["type"]
    required = bool(spec.get("required", False))
    default = spec.get("default")
    array = spec.get("array")
    if kind == "string":
        size = spec.get("size", 64)
        db.create_string_attribute(db_id, collection_id, key, size, required,
                                   default=default, array=array)
    elif kind == "integer":
        db.create_integer_attribute(db_id, collection_id, key, required,
                                    default=default, array=array)
    elif kind == "float":
        db.create_float_attribute(db_id, collection_id, key, required,
                                  default=default, array=array)
    elif kind == "boolean":
        db.create_boolean_attribute(db_id, collection_id, key, required,
                                    default=default, array=array)
    elif kind == "datetime":
        db.create_datetime_attribute(db_id, collection_id, key, required,
                                     default=default, array=array)
    else:  # pragma: no cover - schema is internal
        raise ValueError(f"Unsupported attribute type: {kind}")
    LOG.info("Created attribute %s.%s.%s", db_id, collection_id, key)


def ensure_schema(db: Databases | None = None) -> None:
    """Idempotently ensure every collection Bot_CR needs exists.

    Safe to call on every boot: missing pieces are created, existing ones are
    left untouched. New attributes/indexes are back-filled where possible.
    """
    db = db or build_databases()
    db_id = config.APPWRITE_DATABASE_ID
    ensure_database(db)

    for collection_id, (name, attributes, indexes) in COLLECTIONS.items():
        try:
            existing = db.get_collection(db_id, collection_id)
        except AppwriteException as exc:
            if not is_missing(exc):
                raise
            existing = None

        if existing is None:
            db.create_collection(
                db_id,
                collection_id,
                name,
                permissions=[],
                document_security=False,
                enabled=True,
            )
            LOG.info("Created collection %r", collection_id)

        # Attributes must be created one by one through their dedicated
        # endpoints (the inline `attributes` payload is unreliable across
        # Appwrite versions). Idempotent: existing attributes are kept.
        if existing is None:
            existing = db.get_collection(db_id, collection_id)
        have_attrs = {attr.key for attr in (existing.attributes or [])}
        for spec in attributes:
            if spec["key"] not in have_attrs:
                _create_attribute(db, db_id, collection_id, spec)

        have_indexes = {idx.key for idx in (existing.indexes or [])}
        for spec in indexes:
            if spec["key"] not in have_indexes:
                db.create_index(
                    db_id,
                    collection_id,
                    spec["key"],
                    DatabasesIndexType(spec["type"]),
                    spec["attributes"],
                )
                LOG.info("Created index %s on %r", spec["key"], collection_id)