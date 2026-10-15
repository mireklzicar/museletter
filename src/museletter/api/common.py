import hmac
import re

import aiosqlite
from fastapi import HTTPException, Request

from ..db import BUILTIN_TEMPLATE_ID, SIGNUP_CONTEXT_COLUMNS
from ..render import template_source

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email)) and len(email) <= 320


def normalize_email(email: str) -> str:
    return email.strip().lower()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "list"


async def require_api_key(request: Request) -> None:
    settings = request.app.state.settings
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not settings.api_key or not hmac.compare_digest(token, settings.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


async def get_list(db: aiosqlite.Connection, ref: str) -> aiosqlite.Row:
    """Look up a list by id or slug."""
    async with db.execute("SELECT * FROM lists WHERE id = ? OR slug = ?", (ref, ref)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"list not found: {ref}")
    return row


async def get_subscriber(db: aiosqlite.Connection, subscriber_id: str) -> aiosqlite.Row:
    async with db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"subscriber not found: {subscriber_id}")
    return row


async def get_campaign(db: aiosqlite.Connection, campaign_id: str) -> aiosqlite.Row:
    async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"campaign not found: {campaign_id}")
    return row


def builtin_template() -> dict:
    """The packaged default campaign template as a virtual, read-only row. It has
    no DB row so package upgrades (or a MUSELETTER_TEMPLATE_DIR override on the
    server) keep improving it."""
    return {
        "id": BUILTIN_TEMPLATE_ID,
        "name": BUILTIN_TEMPLATE_ID,
        "html": template_source("email.html"),
        "builtin": True,
        "created_at": None,
        "updated_at": None,
    }


async def get_template(db: aiosqlite.Connection, ref: str) -> dict:
    """Look up a template by id or name; 'default' resolves to the built-in."""
    if ref == BUILTIN_TEMPLATE_ID:
        return builtin_template()
    async with db.execute("SELECT * FROM templates WHERE id = ? OR name = ?", (ref, ref)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"template not found: {ref}")
    return {**dict(row), "builtin": False}


async def template_name(db: aiosqlite.Connection, template_id: str | None) -> str | None:
    """Display name for a stored template_id; None stays None (unset/inherit),
    and a dangling id (template deleted after a campaign was sent) becomes None."""
    if template_id is None:
        return None
    if template_id == BUILTIN_TEMPLATE_ID:
        return BUILTIN_TEMPLATE_ID
    async with db.execute("SELECT name FROM templates WHERE id = ?", (template_id,)) as cur:
        row = await cur.fetchone()
    return row["name"] if row else None


async def get_tag(db: aiosqlite.Connection, list_id: str, ref: str) -> aiosqlite.Row | None:
    """Look up a tag by id or name within a list."""
    async with db.execute(
        "SELECT * FROM tags WHERE list_id = ? AND (id = ? OR name = ?)", (list_id, ref, ref)
    ) as cur:
        return await cur.fetchone()


async def subscriber_tag_names(db: aiosqlite.Connection, subscriber_id: str) -> list[str]:
    async with db.execute(
        "SELECT t.name FROM tags t JOIN subscriber_tags st ON st.tag_id = t.id "
        "WHERE st.subscriber_id = ? ORDER BY t.name",
        (subscriber_id,),
    ) as cur:
        return [r["name"] for r in await cur.fetchall()]


def subscriber_json(row: aiosqlite.Row, tags: list[str]) -> dict:
    return {
        "id": row["id"],
        "list_id": row["list_id"],
        "email": row["email"],
        "name": row["name"],
        "status": row["status"],
        "tags": tags,
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "unsubscribed_at": row["unsubscribed_at"],
        "signup": {column.removeprefix("signup_"): row[column] for column in SIGNUP_CONTEXT_COLUMNS},
    }


def campaign_json(row: aiosqlite.Row, tag_name: str | None = None, template: str | None = None) -> dict:
    return {
        "id": row["id"],
        "list_id": row["list_id"],
        "subject": row["subject"],
        "body_markdown": row["body_markdown"],
        "tag": tag_name,
        "template": template,
        "track_opens": bool(row["track_opens"]),
        "status": row["status"],
        "recipient_count": row["recipient_count"],
        "test_sent_at": row["test_sent_at"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


async def campaign_stats(db: aiosqlite.Connection, campaign_id: str) -> dict:
    async with db.execute(
        "SELECT status, COUNT(*) AS n, COUNT(first_opened_at) AS opened, SUM(open_count) AS opens "
        "FROM campaign_recipients WHERE campaign_id = ? GROUP BY status",
        (campaign_id,),
    ) as cur:
        rows = await cur.fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    sent = [r for r in rows if r["status"] in ("sent", "delivered", "bounced", "complained")]
    sent_count = sum(r["n"] for r in sent)
    return {
        **{
            status: counts.get(status, 0)
            for status in ("pending", "sent", "delivered", "bounced", "complained", "failed", "suppressed")
        },
        "unique_opens": sum(r["opened"] for r in rows),
        "total_opens": sum(r["opens"] for r in rows),
        "open_rate": round(sum(r["opened"] for r in sent) / sent_count * 100, 2) if sent_count else 0.0,
    }
