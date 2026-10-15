import csv
import io
import re
from string import Template

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import doctor as doctor_mod
from ..db import BUILTIN_TEMPLATE_ID, SIGNUP_CONTEXT_COLUMNS, SUBSCRIBER_STATUSES, new_id, utcnow
from ..render import SAMPLE_ISSUE_MARKDOWN, SAMPLE_ISSUE_SUBJECT, build_email, validate_template
from ..subscriptions import subscribe_address
from .common import (
    builtin_template,
    campaign_json,
    campaign_stats,
    get_campaign,
    get_list,
    get_subscriber,
    get_tag,
    get_template,
    normalize_email,
    require_api_key,
    slugify,
    subscriber_json,
    subscriber_tag_names,
    template_name,
    valid_email,
)

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


class ListIn(BaseModel):
    name: str
    slug: str | None = None
    template: str | None = None


class ListPatch(BaseModel):
    name: str | None = None
    slug: str | None = None
    template: str | None = None


class SubscriberIn(BaseModel):
    email: str
    name: str = ""
    tags: list[str] = []
    status: str = "active"


class SubscriberPatch(BaseModel):
    name: str | None = None
    status: str | None = None


class SignupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    name: str = Field(default="", max_length=200)
    # The reader's request as the website backend saw it; stored only when
    # MUSELETTER_RECORD_SIGNUP_CONTEXT is on.
    source: str = Field(default="", max_length=200)
    ip: str = Field(default="", max_length=64)
    user_agent: str = Field(default="", max_length=500)


class TagIn(BaseModel):
    name: str


class CampaignIn(BaseModel):
    subject: str
    body_markdown: str
    tag: str | None = None
    template: str | None = None
    track_opens: bool = True


class CampaignPatch(BaseModel):
    subject: str | None = None
    body_markdown: str | None = None
    tag: str | None = None
    template: str | None = None
    track_opens: bool | None = None


class TemplateIn(BaseModel):
    name: str
    html: str | None = None
    copy_of: str | None = None


class TemplatePatch(BaseModel):
    name: str | None = None
    html: str | None = None


class TestSendIn(BaseModel):
    to: str


class SendIn(BaseModel):
    confirm: bool = False
    dry_run: bool = False
    skip_test: bool = False


class SuppressionIn(BaseModel):
    email: str
    reason: str = "manual"


class ImportIn(BaseModel):
    csv: str


# ---------- lists ----------


async def _list_template_name(db, row) -> str:
    """What a list's campaigns render through when they don't pin their own
    template: its stored default, or the built-in."""
    return await template_name(db, row["template_id"]) or BUILTIN_TEMPLATE_ID


@router.get("/lists")
async def list_lists(request: Request):
    db = request.app.state.db
    async with db.execute(
        "SELECT l.*, (SELECT COUNT(*) FROM subscribers s WHERE s.list_id = l.id AND s.status = 'active') AS active_subscribers "
        "FROM lists l ORDER BY l.created_at"
    ) as cur:
        rows = await cur.fetchall()
    return {"lists": [{**dict(r), "template": await _list_template_name(db, r)} for r in rows]}


@router.post("/lists", status_code=201)
async def create_list(request: Request, body: ListIn):
    db = request.app.state.db
    slug = slugify(body.slug or body.name)
    async with db.execute("SELECT 1 FROM lists WHERE slug = ?", (slug,)) as cur:
        if await cur.fetchone():
            raise HTTPException(status_code=409, detail=f"list slug already exists: {slug}")
    template_id = (
        await _template_ref_to_id(db, body.template, for_list=True) if body.template is not None else None
    )
    list_id = new_id("list")
    await db.execute(
        "INSERT INTO lists (id, slug, name, created_at, template_id) VALUES (?, ?, ?, ?, ?)",
        (list_id, slug, body.name, utcnow(), template_id),
    )
    await db.commit()
    lst = await get_list(db, list_id)
    return {"id": list_id, "slug": slug, "name": body.name, "template": await _list_template_name(db, lst)}


@router.get("/lists/{ref}")
async def get_list_detail(request: Request, ref: str):
    db = request.app.state.db
    lst = await get_list(db, ref)
    async with db.execute(
        "SELECT status, COUNT(*) AS n FROM subscribers WHERE list_id = ? GROUP BY status", (lst["id"],)
    ) as cur:
        counts = {r["status"]: r["n"] for r in await cur.fetchall()}
    return {
        **dict(lst),
        "template": await _list_template_name(db, lst),
        "subscribers": {status: counts.get(status, 0) for status in SUBSCRIBER_STATUSES},
        "subscribe_url": f"{request.app.state.settings.base_url}/subscribe/{lst['slug']}",
    }


@router.patch("/lists/{ref}")
async def update_list(request: Request, ref: str, body: ListPatch):
    db = request.app.state.db
    lst = await get_list(db, ref)
    name = body.name if body.name is not None else lst["name"]
    slug = slugify(body.slug) if body.slug is not None else lst["slug"]
    if slug != lst["slug"]:
        async with db.execute("SELECT 1 FROM lists WHERE slug = ? AND id != ?", (slug, lst["id"])) as cur:
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail=f"list slug already exists: {slug}")
    template_id = (
        await _template_ref_to_id(db, body.template, for_list=True)
        if body.template is not None
        else lst["template_id"]
    )
    if template_id != lst["template_id"]:
        # Drafts that inherit this list's template now render differently, so
        # their prior test sends no longer show what would go out.
        await db.execute(
            "UPDATE campaigns SET test_sent_at = NULL "
            "WHERE status = 'draft' AND list_id = ? AND template_id IS NULL",
            (lst["id"],),
        )
    await db.execute(
        "UPDATE lists SET name = ?, slug = ?, template_id = ? WHERE id = ?",
        (name, slug, template_id, lst["id"]),
    )
    await db.commit()
    lst = await get_list(db, lst["id"])
    return {"id": lst["id"], "slug": slug, "name": name, "template": await _list_template_name(db, lst)}


@router.delete("/lists/{ref}", status_code=204)
async def delete_list(request: Request, ref: str):
    db = request.app.state.db
    lst = await get_list(db, ref)
    async with db.execute("SELECT COUNT(*) AS n FROM lists") as cur:
        if (await cur.fetchone())["n"] <= 1:
            raise HTTPException(status_code=409, detail="cannot delete the only list")
    async with db.execute(
        "SELECT 1 FROM campaigns WHERE list_id = ? AND status = 'sending' LIMIT 1", (lst["id"],)
    ) as cur:
        if await cur.fetchone():
            raise HTTPException(
                status_code=409, detail="a campaign on this list is currently sending; cannot delete"
            )
    await db.execute("DELETE FROM lists WHERE id = ?", (lst["id"],))
    await db.commit()
    return Response(status_code=204)


# ---------- subscribers ----------


@router.post("/lists/{ref}/subscribe", status_code=202)
async def subscribe_from_backend(request: Request, ref: str, body: SignupIn):
    """Request double opt-in without exposing membership or overriding opt-outs."""
    lst = await get_list(request.app.state.db, ref)
    email = normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(status_code=422, detail="a valid email is required")
    # The caller authenticates with the admin key and owns its bot verification.
    # This route remains available when public signup is disabled, and always
    # requires email confirmation regardless of the public form's opt-in mode.
    context = {
        "signup_ip": body.ip.strip(),
        "signup_user_agent": body.user_agent,
        "signup_source": body.source.strip(),
    }
    return await subscribe_address(
        request, lst, email, body.name.strip(), double_opt_in=True, context=context
    )


@router.get("/lists/{ref}/subscribers")
async def list_subscribers(
    request: Request,
    ref: str,
    status: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    db = request.app.state.db
    lst = await get_list(db, ref)
    where = ["s.list_id = ?"]
    params: list = [lst["id"]]
    if status:
        where.append("s.status = ?")
        params.append(status)
    if tag:
        tag_row = await get_tag(db, lst["id"], tag)
        if tag_row is None:
            raise HTTPException(status_code=404, detail=f"tag not found: {tag}")
        where.append(
            "EXISTS (SELECT 1 FROM subscriber_tags st WHERE st.subscriber_id = s.id AND st.tag_id = ?)"
        )
        params.append(tag_row["id"])
    if q:
        where.append("(s.email LIKE ? OR s.name LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where_sql = " AND ".join(where)
    async with db.execute(f"SELECT COUNT(*) AS n FROM subscribers s WHERE {where_sql}", params) as cur:
        total = (await cur.fetchone())["n"]
    async with db.execute(
        f"SELECT s.* FROM subscribers s WHERE {where_sql} ORDER BY s.created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ) as cur:
        rows = await cur.fetchall()
    items = [subscriber_json(r, await subscriber_tag_names(db, r["id"])) for r in rows]
    return {"subscribers": items, "total": total, "limit": limit, "offset": offset}


async def _attach_tags(db, list_id: str, subscriber_id: str, tag_names: list[str]) -> None:
    for raw in tag_names:
        name = raw.strip()
        if not name:
            continue
        tag_row = await get_tag(db, list_id, name)
        if tag_row is None:
            tag_id = new_id("tag")
            await db.execute(
                "INSERT INTO tags (id, list_id, name, created_at) VALUES (?, ?, ?, ?)",
                (tag_id, list_id, name, utcnow()),
            )
        else:
            tag_id = tag_row["id"]
        await db.execute(
            "INSERT OR IGNORE INTO subscriber_tags (subscriber_id, tag_id) VALUES (?, ?)",
            (subscriber_id, tag_id),
        )


@router.post("/lists/{ref}/subscribers", status_code=201)
async def add_subscriber(request: Request, ref: str, body: SubscriberIn):
    db = request.app.state.db
    lst = await get_list(db, ref)
    email = normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(status_code=422, detail=f"invalid email: {body.email}")
    if body.status not in SUBSCRIBER_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {body.status}")
    async with db.execute(
        "SELECT id FROM subscribers WHERE list_id = ? AND email = ?", (lst["id"], email)
    ) as cur:
        if await cur.fetchone():
            raise HTTPException(status_code=409, detail=f"subscriber already exists: {email}")
    now = utcnow()
    sub_id = new_id("sub")
    confirmed_at = now if body.status == "active" else None
    await db.execute(
        "INSERT INTO subscribers (id, list_id, email, name, status, created_at, confirmed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sub_id, lst["id"], email, body.name.strip(), body.status, now, confirmed_at),
    )
    await _attach_tags(db, lst["id"], sub_id, body.tags)
    await db.commit()
    async with db.execute("SELECT 1 FROM suppressions WHERE email = ?", (email,)) as cur:
        suppressed = bool(await cur.fetchone())
    result = subscriber_json(await get_subscriber(db, sub_id), await subscriber_tag_names(db, sub_id))
    if suppressed:
        result["warning"] = "this email is on the suppression list and will be skipped at send time"
    return result


@router.get("/subscribers/{subscriber_id}")
async def get_subscriber_detail(request: Request, subscriber_id: str):
    db = request.app.state.db
    row = await get_subscriber(db, subscriber_id)
    return subscriber_json(row, await subscriber_tag_names(db, subscriber_id))


@router.patch("/subscribers/{subscriber_id}")
async def update_subscriber(request: Request, subscriber_id: str, body: SubscriberPatch):
    db = request.app.state.db
    row = await get_subscriber(db, subscriber_id)
    name = body.name if body.name is not None else row["name"]
    status = body.status if body.status is not None else row["status"]
    if status not in SUBSCRIBER_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status: {status}")
    await db.execute(
        "UPDATE subscribers SET name = ?, status = ? WHERE id = ?", (name, status, subscriber_id)
    )
    # Opting a subscriber out here must stop any in-flight campaign from
    # reaching them, exactly as the public unsubscribe path does.
    if status != "active" and row["status"] == "active":
        await db.execute(
            "UPDATE campaign_recipients SET status = 'suppressed', updated_at = ? "
            "WHERE subscriber_id = ? AND status = 'pending'",
            (utcnow(), subscriber_id),
        )
    await db.commit()
    return subscriber_json(
        await get_subscriber(db, subscriber_id), await subscriber_tag_names(db, subscriber_id)
    )


@router.delete("/subscribers/{subscriber_id}", status_code=204)
async def delete_subscriber(request: Request, subscriber_id: str):
    db = request.app.state.db
    await get_subscriber(db, subscriber_id)
    await db.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
    await db.commit()
    return Response(status_code=204)


@router.post("/subscribers/{subscriber_id}/tags")
async def tag_subscriber(request: Request, subscriber_id: str, body: TagIn):
    db = request.app.state.db
    row = await get_subscriber(db, subscriber_id)
    await _attach_tags(db, row["list_id"], subscriber_id, [body.name])
    await db.commit()
    return subscriber_json(row, await subscriber_tag_names(db, subscriber_id))


@router.delete("/subscribers/{subscriber_id}/tags/{tag_ref}")
async def untag_subscriber(request: Request, subscriber_id: str, tag_ref: str):
    db = request.app.state.db
    row = await get_subscriber(db, subscriber_id)
    tag_row = await get_tag(db, row["list_id"], tag_ref)
    if tag_row is None:
        raise HTTPException(status_code=404, detail=f"tag not found: {tag_ref}")
    await db.execute(
        "DELETE FROM subscriber_tags WHERE subscriber_id = ? AND tag_id = ?", (subscriber_id, tag_row["id"])
    )
    await db.commit()
    return subscriber_json(row, await subscriber_tag_names(db, subscriber_id))


# ---------- csv import / export ----------


@router.post("/lists/{ref}/subscribers/import")
async def import_subscribers(request: Request, ref: str, body: ImportIn):
    db = request.app.state.db
    lst = await get_list(db, ref)
    reader = csv.reader(io.StringIO(body.csv))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise HTTPException(status_code=422, detail="empty CSV")

    header = [c.strip().lower() for c in rows[0]]
    if "email" in header:
        col = {name: header.index(name) for name in ("email", "name", "tags", "status") if name in header}
        data_rows = rows[1:]
    else:
        # Headerless: first column email, optional second column name.
        col = {"email": 0, "name": 1} if len(rows[0]) > 1 else {"email": 0}
        data_rows = rows

    def cell(row: list[str], name: str) -> str:
        idx = col.get(name)
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    imported, skipped_existing, skipped_invalid = 0, 0, 0
    now = utcnow()
    for row in data_rows:
        email = normalize_email(cell(row, "email"))
        if not valid_email(email):
            skipped_invalid += 1
            continue
        status = cell(row, "status") or "active"
        if status not in SUBSCRIBER_STATUSES:
            status = "active"
        async with db.execute(
            "SELECT 1 FROM subscribers WHERE list_id = ? AND email = ?", (lst["id"], email)
        ) as cur:
            if await cur.fetchone():
                skipped_existing += 1
                continue
        sub_id = new_id("sub")
        await db.execute(
            "INSERT INTO subscribers (id, list_id, email, name, status, created_at, confirmed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sub_id, lst["id"], email, cell(row, "name"), status, now, now if status == "active" else None),
        )
        tags = [t for t in cell(row, "tags").split(";") if t.strip()]
        if tags:
            await _attach_tags(db, lst["id"], sub_id, tags)
        imported += 1
    await db.commit()
    return {"imported": imported, "skipped_existing": skipped_existing, "skipped_invalid": skipped_invalid}


@router.get("/lists/{ref}/subscribers/export")
async def export_subscribers(request: Request, ref: str):
    db = request.app.state.db
    lst = await get_list(db, ref)
    out = io.StringIO()
    writer = csv.writer(out)
    # The first five columns are what `import` reads back; the rest are history.
    writer.writerow(
        [
            "email",
            "name",
            "status",
            "tags",
            "created_at",
            "confirmed_at",
            "unsubscribed_at",
            *SIGNUP_CONTEXT_COLUMNS,
        ]
    )
    async with db.execute(
        "SELECT * FROM subscribers WHERE list_id = ? ORDER BY created_at", (lst["id"],)
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        tags = await subscriber_tag_names(db, row["id"])
        writer.writerow(
            [
                row["email"],
                row["name"],
                row["status"],
                ";".join(tags),
                row["created_at"],
                row["confirmed_at"] or "",
                row["unsubscribed_at"] or "",
                *(row[column] or "" for column in SIGNUP_CONTEXT_COLUMNS),
            ]
        )
    return Response(content=out.getvalue(), media_type="text/csv")


# ---------- tags ----------


@router.get("/lists/{ref}/tags")
async def list_tags(request: Request, ref: str):
    db = request.app.state.db
    lst = await get_list(db, ref)
    async with db.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM subscriber_tags st WHERE st.tag_id = t.id) AS subscriber_count "
        "FROM tags t WHERE t.list_id = ? ORDER BY t.name",
        (lst["id"],),
    ) as cur:
        rows = await cur.fetchall()
    return {"tags": [dict(r) for r in rows]}


@router.post("/lists/{ref}/tags", status_code=201)
async def create_tag(request: Request, ref: str, body: TagIn):
    db = request.app.state.db
    lst = await get_list(db, ref)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="tag name is required")
    if await get_tag(db, lst["id"], name):
        raise HTTPException(status_code=409, detail=f"tag already exists: {name}")
    tag_id = new_id("tag")
    await db.execute(
        "INSERT INTO tags (id, list_id, name, created_at) VALUES (?, ?, ?, ?)",
        (tag_id, lst["id"], name, utcnow()),
    )
    await db.commit()
    return {"id": tag_id, "list_id": lst["id"], "name": name}


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(request: Request, tag_id: str):
    db = request.app.state.db
    async with db.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail=f"tag not found: {tag_id}")
    await db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    await db.commit()
    return Response(status_code=204)


# ---------- templates ----------

RESERVED_TEMPLATE_NAMES = (BUILTIN_TEMPLATE_ID, "none")  # 'none' is the CLI's clear sentinel


def _template_json(t: dict, include_html: bool = False) -> dict:
    out = {
        "id": t["id"],
        "name": t["name"],
        "builtin": t["builtin"],
        "size": len(t["html"].encode("utf-8")),
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
    }
    if include_html:
        out["html"] = t["html"]
    return out


def _normalize_template_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    if not name:
        raise HTTPException(status_code=422, detail="template name must contain letters or numbers")
    if name in RESERVED_TEMPLATE_NAMES:
        raise HTTPException(status_code=422, detail=f"'{name}' is a reserved template name")
    return name


async def _template_ref_to_id(db, ref: str, *, for_list: bool) -> str | None:
    """Resolve a user-supplied template reference to what a template_id column
    stores. '' clears the field. The built-in maps to NULL on lists (there is
    nothing above a list to inherit) but to the literal 'default' on campaigns,
    which pins the built-in even when the list has a custom default."""
    if ref == "":
        return None
    t = await get_template(db, ref)
    if t["builtin"]:
        return None if for_list else BUILTIN_TEMPLATE_ID
    return t["id"]


@router.get("/templates")
async def list_templates(request: Request):
    db = request.app.state.db
    items = [_template_json(builtin_template())]
    async with db.execute("SELECT * FROM templates ORDER BY name") as cur:
        rows = await cur.fetchall()
    items += [_template_json({**dict(r), "builtin": False}) for r in rows]
    return {"templates": items}


@router.post("/templates", status_code=201)
async def create_template(request: Request, body: TemplateIn):
    db = request.app.state.db
    name = _normalize_template_name(body.name)
    if body.html is not None and body.copy_of is None:
        html = body.html
    elif body.copy_of is not None and body.html is None:
        html = (await get_template(db, body.copy_of))["html"]
    else:
        raise HTTPException(
            status_code=422,
            detail="provide either html or copy_of (an existing template to duplicate), not both",
        )
    problems = validate_template(html)
    if problems:
        raise HTTPException(status_code=422, detail="invalid template: " + "; ".join(problems))
    async with db.execute("SELECT 1 FROM templates WHERE name = ?", (name,)) as cur:
        if await cur.fetchone():
            raise HTTPException(status_code=409, detail=f"template already exists: {name}")
    template_id = new_id("tpl")
    now = utcnow()
    await db.execute(
        "INSERT INTO templates (id, name, html, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (template_id, name, html, now, now),
    )
    await db.commit()
    record = {"id": template_id, "name": name, "html": html, "builtin": False}
    return _template_json({**record, "created_at": now, "updated_at": now})


@router.get("/templates/{ref}")
async def get_template_detail(request: Request, ref: str):
    return _template_json(await get_template(request.app.state.db, ref), include_html=True)


@router.patch("/templates/{ref}")
async def update_template(request: Request, ref: str, body: TemplatePatch):
    db = request.app.state.db
    t = await get_template(db, ref)
    if t["builtin"]:
        raise HTTPException(
            status_code=409,
            detail="the built-in default template cannot be edited; "
            "duplicate it (copy_of=default) and customize the copy",
        )
    name = _normalize_template_name(body.name) if body.name is not None else t["name"]
    if name != t["name"]:
        async with db.execute("SELECT 1 FROM templates WHERE name = ? AND id != ?", (name, t["id"])) as cur:
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail=f"template already exists: {name}")
    html = t["html"]
    if body.html is not None and body.html != t["html"]:
        problems = validate_template(body.html)
        if problems:
            raise HTTPException(status_code=422, detail="invalid template: " + "; ".join(problems))
        # A mid-send design change would split one campaign across two looks.
        async with db.execute(
            "SELECT 1 FROM campaigns c JOIN lists l ON l.id = c.list_id WHERE c.status = 'sending' "
            "AND (c.template_id = ? OR (c.template_id IS NULL AND l.template_id = ?)) LIMIT 1",
            (t["id"], t["id"]),
        ) as cur:
            if await cur.fetchone():
                raise HTTPException(
                    status_code=409,
                    detail="a campaign using this template is currently sending; try again when it finishes",
                )
        html = body.html
        # What you tested is what you send: a design change invalidates prior
        # test sends on every draft that would render through this template.
        await db.execute(
            "UPDATE campaigns SET test_sent_at = NULL WHERE status = 'draft' AND (template_id = ? "
            "OR (template_id IS NULL AND list_id IN (SELECT id FROM lists WHERE template_id = ?)))",
            (t["id"], t["id"]),
        )
    await db.execute(
        "UPDATE templates SET name = ?, html = ?, updated_at = ? WHERE id = ?",
        (name, html, utcnow(), t["id"]),
    )
    await db.commit()
    return _template_json(await get_template(db, t["id"]))


@router.delete("/templates/{ref}", status_code=204)
async def delete_template(request: Request, ref: str):
    db = request.app.state.db
    t = await get_template(db, ref)
    if t["builtin"]:
        raise HTTPException(status_code=409, detail="the built-in default template cannot be deleted")
    async with db.execute("SELECT slug FROM lists WHERE template_id = ? ORDER BY slug", (t["id"],)) as cur:
        slugs = [r["slug"] for r in await cur.fetchall()]
    if slugs:
        raise HTTPException(
            status_code=409,
            detail=f"template is the default for list(s): {', '.join(slugs)}; change those first",
        )
    async with db.execute(
        "SELECT COUNT(*) AS n FROM campaigns WHERE template_id = ? AND status IN ('draft', 'sending')",
        (t["id"],),
    ) as cur:
        in_use = (await cur.fetchone())["n"]
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"{in_use} unsent campaign(s) use this template; edit or delete those first",
        )
    await db.execute("DELETE FROM templates WHERE id = ?", (t["id"],))
    await db.commit()
    return Response(status_code=204)


@router.post("/templates/{ref}/test")
async def test_send_template(request: Request, ref: str, body: TestSendIn):
    """Send a sample issue rendered through the template, to judge it in a real inbox."""
    db = request.app.state.db
    settings = request.app.state.settings
    t = await get_template(db, ref)
    to = normalize_email(body.to)
    if not valid_email(to):
        raise HTTPException(status_code=422, detail=f"invalid email: {body.to}")
    async with db.execute("SELECT name FROM lists ORDER BY created_at LIMIT 1") as cur:
        first_list = await cur.fetchone()
    subject, html, text = build_email(
        SAMPLE_ISSUE_SUBJECT,
        SAMPLE_ISSUE_MARKDOWN,
        name="Sam",
        email=to,
        unsubscribe_url=f"{settings.base_url}/unsubscribe/preview",
        list_name=first_list["name"] if first_list else "Newsletter",
        postal_address=settings.postal_address,
        template=None if t["builtin"] else Template(t["html"]),
        attribution=settings.attribution,
    )
    try:
        result = await request.app.state.mailer.send_email(
            to,
            f"[template {t['name']}] {subject}",
            html,
            text,
            from_email=settings.from_email,
            from_name=settings.from_name,
            reply_to=settings.reply_to,
        )
    except Exception as exc:  # surface provider failures as a client-visible error
        raise HTTPException(status_code=502, detail=f"test send failed: {exc}") from exc
    return {"sent_to": to, "template": t["name"], "ses_message_id": result.message_id}


# ---------- campaigns ----------


async def _campaign_tag_name(db, row) -> str | None:
    if not row["tag_id"]:
        return None
    async with db.execute("SELECT name FROM tags WHERE id = ?", (row["tag_id"],)) as cur:
        tag = await cur.fetchone()
    return tag["name"] if tag else None


async def _resolve_tag_id(db, list_id: str, tag_ref: str | None) -> str | None:
    if tag_ref is None or tag_ref == "":
        return None
    tag_row = await get_tag(db, list_id, tag_ref)
    if tag_row is None:
        raise HTTPException(status_code=404, detail=f"tag not found: {tag_ref}")
    return tag_row["id"]


async def _campaign_json(db, row) -> dict:
    return campaign_json(row, await _campaign_tag_name(db, row), await template_name(db, row["template_id"]))


async def _effective_template(db, campaign, lst) -> tuple[str, Template | None]:
    """The template a send of this campaign would render through: the campaign's
    own, else the list default, else the built-in (returned as None). Raises 412
    when the stored template is missing or no longer valid - the send-guardrail
    preflight, shared by preview, test, and send so they can never disagree."""
    template_id = campaign["template_id"] or lst["template_id"]
    if not template_id or template_id == BUILTIN_TEMPLATE_ID:
        return BUILTIN_TEMPLATE_ID, None
    async with db.execute("SELECT name, html FROM templates WHERE id = ?", (template_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=412,
            detail=f"campaign references a missing template ({template_id}); set another one",
        )
    problems = validate_template(row["html"])
    if problems:
        raise HTTPException(
            status_code=412,
            detail=f"template '{row['name']}' failed validation: " + "; ".join(problems),
        )
    return row["name"], Template(row["html"])


@router.post("/lists/{ref}/campaigns", status_code=201)
async def create_campaign(request: Request, ref: str, body: CampaignIn):
    db = request.app.state.db
    lst = await get_list(db, ref)
    if not body.subject.strip():
        raise HTTPException(status_code=422, detail="subject is required")
    if not body.body_markdown.strip():
        raise HTTPException(status_code=422, detail="body_markdown is required")
    tag_id = await _resolve_tag_id(db, lst["id"], body.tag)
    template_id = (
        await _template_ref_to_id(db, body.template, for_list=False) if body.template is not None else None
    )
    campaign_id = new_id("cmp")
    await db.execute(
        "INSERT INTO campaigns (id, list_id, subject, body_markdown, tag_id, template_id, track_opens, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            campaign_id,
            lst["id"],
            body.subject.strip(),
            body.body_markdown,
            tag_id,
            template_id,
            body.track_opens,
            utcnow(),
        ),
    )
    await db.commit()
    return await _campaign_json(db, await get_campaign(db, campaign_id))


@router.get("/campaigns")
async def list_campaigns(request: Request, list: str | None = None, status: str | None = None):
    db = request.app.state.db
    where, params = ["1=1"], []
    if list:
        lst = await get_list(db, list)
        where.append("c.list_id = ?")
        params.append(lst["id"])
    if status:
        where.append("c.status = ?")
        params.append(status)
    async with db.execute(
        f"SELECT c.* FROM campaigns c WHERE {' AND '.join(where)} ORDER BY c.created_at DESC", params
    ) as cur:
        rows = await cur.fetchall()
    return {"campaigns": [await _campaign_json(db, r) for r in rows]}


@router.get("/campaigns/{campaign_id}")
async def get_campaign_detail(request: Request, campaign_id: str):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    result = await _campaign_json(db, row)
    if row["status"] != "draft":
        result["stats"] = await campaign_stats(db, campaign_id)
    return result


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(request: Request, campaign_id: str, body: CampaignPatch):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    if row["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"campaign is {row['status']}; only drafts can be edited")
    subject = body.subject.strip() if body.subject is not None else row["subject"]
    markdown = body.body_markdown if body.body_markdown is not None else row["body_markdown"]
    tag_id = await _resolve_tag_id(db, row["list_id"], body.tag) if body.tag is not None else row["tag_id"]
    template_id = (
        await _template_ref_to_id(db, body.template, for_list=False)
        if body.template is not None
        else row["template_id"]
    )
    track_opens = body.track_opens if body.track_opens is not None else row["track_opens"]
    await db.execute(
        "UPDATE campaigns SET subject = ?, body_markdown = ?, tag_id = ?, template_id = ?, track_opens = ?, "
        "test_sent_at = NULL WHERE id = ?",
        (subject, markdown, tag_id, template_id, track_opens, campaign_id),
    )
    await db.commit()
    return await _campaign_json(db, await get_campaign(db, campaign_id))


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(request: Request, campaign_id: str):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    if row["status"] == "sending":
        raise HTTPException(status_code=409, detail="campaign is currently sending; cannot delete")
    await db.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    await db.commit()
    return Response(status_code=204)


def _render_campaign_preview(request: Request, row, lst, template: Template | None) -> tuple[str, str, str]:
    settings = request.app.state.settings
    return build_email(
        row["subject"],
        row["body_markdown"],
        name="Sam",
        email="sample@example.com",
        unsubscribe_url=f"{settings.base_url}/unsubscribe/preview",
        list_name=lst["name"],
        postal_address=settings.postal_address,
        template=template,
        attribution=settings.attribution,
    )


@router.get("/campaigns/{campaign_id}/preview")
async def preview_campaign(request: Request, campaign_id: str):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    lst = await get_list(db, row["list_id"])
    name, template = await _effective_template(db, row, lst)
    subject, html, text = _render_campaign_preview(request, row, lst, template)
    return {"subject": subject, "html": html, "text": text, "template": name}


@router.post("/campaigns/{campaign_id}/test")
async def test_send_campaign(request: Request, campaign_id: str, body: TestSendIn):
    db = request.app.state.db
    settings = request.app.state.settings
    row = await get_campaign(db, campaign_id)
    lst = await get_list(db, row["list_id"])
    to = normalize_email(body.to)
    if not valid_email(to):
        raise HTTPException(status_code=422, detail=f"invalid email: {body.to}")
    _, template = await _effective_template(db, row, lst)
    subject, html, text = _render_campaign_preview(request, row, lst, template)
    try:
        result = await request.app.state.mailer.send_email(
            to,
            f"[test] {subject}",
            html,
            text,
            from_email=settings.from_email,
            from_name=settings.from_name,
            reply_to=settings.reply_to,
        )
    except Exception as exc:  # surface provider failures as a client-visible error
        raise HTTPException(status_code=502, detail=f"test send failed: {exc}") from exc
    await db.execute("UPDATE campaigns SET test_sent_at = ? WHERE id = ?", (utcnow(), campaign_id))
    await db.commit()
    return {"sent_to": to, "ses_message_id": result.message_id}


def _audience_query(list_id: str, tag_id: str | None) -> tuple[str, list]:
    sql = (
        "FROM subscribers s WHERE s.list_id = ? AND s.status = 'active' "
        "AND NOT EXISTS (SELECT 1 FROM suppressions sup WHERE sup.email = s.email)"
    )
    params: list = [list_id]
    if tag_id:
        sql += (
            " AND EXISTS (SELECT 1 FROM subscriber_tags st WHERE st.subscriber_id = s.id AND st.tag_id = ?)"
        )
        params.append(tag_id)
    return sql, params


@router.post("/campaigns/{campaign_id}/send")
async def send_campaign(request: Request, campaign_id: str, body: SendIn):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    if row["status"] != "draft":
        raise HTTPException(status_code=409, detail=f"campaign is already {row['status']}")

    # Preflight the template on the dry run too, so a doomed send surfaces there.
    lst = await get_list(db, row["list_id"])
    effective_template, _ = await _effective_template(db, row, lst)

    audience_sql, params = _audience_query(row["list_id"], row["tag_id"])
    async with db.execute(f"SELECT COUNT(*) AS n {audience_sql}", params) as cur:
        recipient_count = (await cur.fetchone())["n"]

    if body.dry_run:
        async with db.execute(f"SELECT s.email {audience_sql} LIMIT 10", params) as cur:
            sample = [r["email"] for r in await cur.fetchall()]
        return {
            "dry_run": True,
            "recipient_count": recipient_count,
            "sample": sample,
            "template": effective_template,
        }

    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=f"this would email {recipient_count} people; pass confirm=true to proceed "
            "(or dry_run=true to see the audience)",
        )
    if row["test_sent_at"] is None and not body.skip_test:
        raise HTTPException(
            status_code=412,
            detail="no test send yet; POST /test with your address first, or pass skip_test=true",
        )
    if recipient_count == 0:
        raise HTTPException(status_code=400, detail="audience is empty; nothing to send")

    now = utcnow()
    await db.execute(
        f"INSERT INTO campaign_recipients (campaign_id, subscriber_id, email, name, status, updated_at) "
        f"SELECT ?, s.id, s.email, s.name, 'pending', ? {audience_sql}",
        [campaign_id, now, *params],
    )
    await db.execute(
        "UPDATE campaigns SET status = 'sending', recipient_count = ?, started_at = ? WHERE id = ?",
        (recipient_count, now, campaign_id),
    )
    await db.commit()
    request.app.state.sender.wake()
    return await _campaign_json(db, await get_campaign(db, campaign_id))


@router.get("/campaigns/{campaign_id}/stats")
async def get_campaign_stats(request: Request, campaign_id: str):
    db = request.app.state.db
    row = await get_campaign(db, campaign_id)
    return {
        "id": campaign_id,
        "status": row["status"],
        "recipient_count": row["recipient_count"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "track_opens": bool(row["track_opens"]),
        **await campaign_stats(db, campaign_id),
    }


# ---------- suppressions ----------


@router.get("/suppressions")
async def list_suppressions(
    request: Request, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)
):
    db = request.app.state.db
    async with db.execute("SELECT COUNT(*) AS n FROM suppressions") as cur:
        total = (await cur.fetchone())["n"]
    async with db.execute(
        "SELECT * FROM suppressions ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
    ) as cur:
        rows = await cur.fetchall()
    return {"suppressions": [dict(r) for r in rows], "total": total}


@router.post("/suppressions", status_code=201)
async def add_suppression(request: Request, body: SuppressionIn):
    db = request.app.state.db
    email = normalize_email(body.email)
    if not valid_email(email):
        raise HTTPException(status_code=422, detail=f"invalid email: {body.email}")
    await db.execute(
        "INSERT OR REPLACE INTO suppressions (email, reason, detail, created_at) VALUES (?, ?, ?, ?)",
        (email, body.reason, "added via API", utcnow()),
    )
    await db.commit()
    return {"email": email, "reason": body.reason}


@router.delete("/suppressions/{email}", status_code=204)
async def remove_suppression(request: Request, email: str):
    db = request.app.state.db
    await db.execute("DELETE FROM suppressions WHERE email = ?", (normalize_email(email),))
    await db.commit()
    return Response(status_code=204)


# ---------- doctor ----------


@router.get("/doctor")
async def run_doctor(request: Request):
    return await doctor_mod.run_checks(
        request.app.state.settings, request.app.state.mailer, request.app.state.db
    )
