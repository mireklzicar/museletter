import secrets
from datetime import UTC, datetime

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lists (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    template_id TEXT
);

CREATE TABLE IF NOT EXISTS subscribers (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unconfirmed',
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    unsubscribed_at TEXT,
    confirmation_sent_at TEXT,
    signup_ip TEXT,
    signup_user_agent TEXT,
    signup_origin TEXT,
    signup_referrer TEXT,
    signup_source TEXT,
    UNIQUE (list_id, email)
);
CREATE INDEX IF NOT EXISTS idx_subscribers_list_status ON subscribers(list_id, status);
CREATE INDEX IF NOT EXISTS idx_subscribers_email ON subscribers(email);

CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (list_id, name)
);

CREATE TABLE IF NOT EXISTS subscriber_tags (
    subscriber_id TEXT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (subscriber_id, tag_id)
);

CREATE TABLE IF NOT EXISTS suppressions (
    email TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    html TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    tag_id TEXT REFERENCES tags(id) ON DELETE SET NULL,
    template_id TEXT,
    track_opens INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    recipient_count INTEGER NOT NULL DEFAULT 0,
    test_sent_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS campaign_recipients (
    campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    subscriber_id TEXT NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    ses_message_id TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    open_count INTEGER NOT NULL DEFAULT 0,
    first_opened_at TEXT,
    last_opened_at TEXT,
    PRIMARY KEY (campaign_id, subscriber_id)
);
CREATE INDEX IF NOT EXISTS idx_recipients_status ON campaign_recipients(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_recipients_msgid ON campaign_recipients(ses_message_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    ses_message_id TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (key, path)
);
"""

# Evidence of how an address signed up (MUSELETTER_RECORD_SIGNUP_CONTEXT), kept
# with the subscriber so consent can be shown later. Written once, on insert.
SIGNUP_CONTEXT_COLUMNS = (
    "signup_ip",
    "signup_user_agent",
    "signup_origin",
    "signup_referrer",
    "signup_source",
)

SUBSCRIBER_STATUSES = ("unconfirmed", "active", "unsubscribed", "bounced", "complained")

# A template_id column holds a templates.id, the literal 'default' (the packaged
# built-in, which has no row), or NULL: inherit the list default on campaigns,
# use the built-in on lists. No FK on purpose - 'default' is virtual; the API
# guards deletion of referenced templates instead.
BUILTIN_TEMPLATE_ID = "default"
CAMPAIGN_STATUSES = ("draft", "sending", "sent", "failed")
RECIPIENT_STATUSES = ("pending", "sent", "delivered", "bounced", "complained", "failed", "suppressed")


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


async def open_db(path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.executescript(SCHEMA)
    await _migrate(db)
    await db.commit()
    return db


async def _migrate(db: aiosqlite.Connection) -> None:
    """Add columns introduced after the initial schema to pre-existing databases."""
    async with db.execute("PRAGMA table_info(subscribers)") as cur:
        columns = {row["name"] for row in await cur.fetchall()}
    if "confirmation_sent_at" not in columns:
        await db.execute("ALTER TABLE subscribers ADD COLUMN confirmation_sent_at TEXT")
    for column in SIGNUP_CONTEXT_COLUMNS:
        if column not in columns:
            await db.execute(f"ALTER TABLE subscribers ADD COLUMN {column} TEXT")
    for table in ("lists", "campaigns"):
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        if "template_id" not in columns:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN template_id TEXT")
    async with db.execute("PRAGMA table_info(campaigns)") as cur:
        columns = {row["name"] for row in await cur.fetchall()}
    if "track_opens" not in columns:
        await db.execute("ALTER TABLE campaigns ADD COLUMN track_opens INTEGER NOT NULL DEFAULT 1")
        # Already queued or sent messages have no pixel; do not imply otherwise.
        await db.execute("UPDATE campaigns SET track_opens = 0 WHERE status != 'draft'")
    async with db.execute("PRAGMA table_info(campaign_recipients)") as cur:
        columns = {row["name"] for row in await cur.fetchall()}
    for column, definition in (
        ("open_count", "INTEGER NOT NULL DEFAULT 0"),
        ("first_opened_at", "TEXT"),
        ("last_opened_at", "TEXT"),
    ):
        if column not in columns:
            await db.execute(f"ALTER TABLE campaign_recipients ADD COLUMN {column} {definition}")


async def get_meta(db: aiosqlite.Connection, key: str) -> str | None:
    async with db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else None


async def set_meta(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def ensure_secret(db: aiosqlite.Connection, configured: str = "") -> str:
    """Signing secret for public links. Stored in the DB so tokens survive restarts
    and container moves without any extra configuration."""
    if configured:
        return configured
    existing = await get_meta(db, "secret")
    if existing:
        return existing
    generated = secrets.token_hex(32)
    await set_meta(db, "secret", generated)
    return generated


async def ensure_default_list(db: aiosqlite.Connection) -> None:
    async with db.execute("SELECT COUNT(*) AS n FROM lists") as cur:
        row = await cur.fetchone()
    if row is not None and row["n"] == 0:
        await db.execute(
            "INSERT INTO lists (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
            (new_id("list"), "default", "Newsletter", utcnow()),
        )
        await db.commit()
