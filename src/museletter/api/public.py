import html as html_mod
import json
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from ..db import utcnow
from ..events import apply_events
from ..render import load_template
from ..sns import is_amazon_sns_url, parse_ses_events
from ..subscriptions import subscribe_address
from ..tokens import verify_open_token, verify_token
from .common import normalize_email, valid_email

router = APIRouter()

_OPEN_PIXEL = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


@router.api_route("/open/{token}.gif", methods=["GET", "HEAD"])
async def track_open(request: Request, token: str):
    recipient = verify_open_token(request.app.state.secret, token)
    if request.method == "GET" and recipient is not None:
        db = request.app.state.db
        now = utcnow()
        # A client can load the pixel while the provider call is still pending.
        # Only attempted recipients qualify; opens never change delivery state.
        await db.execute(
            "UPDATE campaign_recipients SET open_count = open_count + 1, "
            "first_opened_at = COALESCE(first_opened_at, ?), last_opened_at = ? "
            "WHERE campaign_id = ? AND subscriber_id = ? AND attempts > 0 "
            "AND EXISTS (SELECT 1 FROM campaigns WHERE id = campaign_id "
            "AND track_opens = 1 AND status != 'draft')",
            (now, now, *recipient),
        )
        await db.commit()
    # Invalid and deleted tokens get the same image, with no recipient details.
    return Response(
        content=_OPEN_PIXEL,
        media_type="image/gif",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"},
    )


class RateLimiter:
    """Small in-memory limiter for the public subscribe endpoint."""

    def __init__(self, limit: int = 10, window: float = 60.0):
        self.limit = limit
        self.window = window
        self.hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self.hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self.hits[key] = hits
            return False
        hits.append(now)
        self.hits[key] = hits
        if len(self.hits) > 10_000:
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}
        return True


def client_ip(request: Request) -> str:
    """The requester's IP. Behind a proxy (Fly/Railway/Cloudflare) the socket
    peer is the proxy, so all visitors share one bucket unless we read the
    forwarded header, and only when the operator opts in via trust_proxy."""
    if request.app.state.settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_PAGE_TEMPLATE = load_template("page.html")


_MARK_SVG = (
    '<svg class="mark" viewBox="0 0 32 32" aria-hidden="true">'
    '<rect x="9.40" y="7.85" width="13.20" height="2.8" rx="1.40" transform="rotate(0 16 9.25)"/>'
    '<rect x="9.40" y="7.85" width="13.20" height="2.8" rx="1.40" transform="rotate(60 16 9.25)"/>'
    '<rect x="9.40" y="7.85" width="13.20" height="2.8" rx="1.40" transform="rotate(120 16 9.25)"/>'
    '<rect x="1.60" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(0 8.2 22.75)"/>'
    '<rect x="1.60" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(60 8.2 22.75)"/>'
    '<rect x="1.60" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(120 8.2 22.75)"/>'
    '<rect x="17.20" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(0 23.8 22.75)"/>'
    '<rect x="17.20" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(60 23.8 22.75)"/>'
    '<rect x="17.20" y="21.35" width="13.20" height="2.8" rx="1.40" transform="rotate(120 23.8 22.75)"/>'
    "</svg>"
)


def _page(request: Request, *args, **kwargs) -> HTMLResponse:
    return _render_page(*args, attribution=request.app.state.settings.attribution, **kwargs)


def _render_page(
    heading: str,
    body: str,
    *,
    variant: str = "accent",
    list_name: str = "Museletter",
    action: str = "",
    attribution: bool = True,
) -> HTMLResponse:
    """Render a public page in the split-panel brand layout. `body` and `action`
    are trusted HTML built by the caller; `heading` and `list_name` are escaped.
    With attribution off, the Museletter mark and "Powered by" line are left out."""
    action_html = f'<div class="actions">{action}</div>' if action else ""
    foot = (
        '<div class="foot">Powered by '
        '<a href="https://github.com/sanketsaurav/museletter" style="text-decoration:underline;">Museletter</a>'
        "</div>"
        if attribution
        else ""
    )
    content = (
        f'<div class="panel {variant}">{_MARK_SVG if attribution else ""}'
        f'<div><div class="panel-name">{html_mod.escape(list_name)}</div></div></div>'
        f'<div class="content"><div><h1>{html_mod.escape(heading)}</h1>{body}{action_html}</div>'
        f"{foot}</div>"
    )
    return HTMLResponse(_PAGE_TEMPLATE.substitute(title=html_mod.escape(heading), content=content))


@router.post("/subscribe/{slug}")
async def subscribe(request: Request, slug: str):
    db = request.app.state.db
    settings = request.app.state.settings

    if not settings.public_subscribe:
        # Operator adds subscribers via the authenticated admin API; hide this route.
        raise HTTPException(status_code=404, detail="not found")

    if not request.app.state.rate_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests; try again in a minute")

    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid JSON body") from None
    else:
        form = await request.form()
        data = dict(form)

    email = normalize_email(str(data.get("email", "")))
    name = str(data.get("name", "")).strip()[:200]
    honeypot = str(data.get("website", "")).strip()

    async with db.execute("SELECT * FROM lists WHERE slug = ?", (slug,)) as cur:
        lst = await cur.fetchone()
    if lst is None:
        raise HTTPException(status_code=404, detail="list not found")
    if not valid_email(email):
        raise HTTPException(status_code=422, detail="a valid email is required")

    if settings.turnstile_secret:
        token = str(data.get("cf-turnstile-response") or data.get("turnstile_token") or "")
        if not await request.app.state.turnstile_verify(settings.turnstile_secret, token, client_ip(request)):
            raise HTTPException(status_code=403, detail="human verification failed; please try again")

    # One response for every success path, so an attacker cannot probe whether an
    # address is already on the list.
    ok = (
        {"status": "pending_confirmation", "message": "Check your inbox to confirm your subscription."}
        if settings.opt_in == "double"
        else {"status": "subscribed", "message": "You're subscribed."}
    )

    # Bots fill the hidden field; respond without changing the subscriber list.
    if honeypot:
        return JSONResponse(ok)
    context = {
        "signup_ip": client_ip(request),
        "signup_user_agent": request.headers.get("user-agent", "")[:500],
        "signup_origin": request.headers.get("origin", "")[:200],
        "signup_referrer": request.headers.get("referer", "")[:500],
        "signup_source": str(data.get("source", "")).strip()[:200],
    }
    return JSONResponse(
        await subscribe_address(
            request, lst, email, name, double_opt_in=settings.opt_in == "double", context=context
        )
    )


@router.get("/confirm/{token}")
async def confirm(request: Request, token: str):
    db = request.app.state.db
    invalid = ("Invalid link", "<p>This confirmation link is not valid.</p>")
    subscriber_id = verify_token(request.app.state.secret, token, "confirm")
    if subscriber_id is None:
        return _page(request, *invalid, variant="muted")
    async with db.execute(
        "SELECT s.*, l.name AS list_name FROM subscribers s JOIN lists l ON l.id = s.list_id WHERE s.id = ?",
        (subscriber_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return _page(request, *invalid, variant="muted")
    lst = row["list_name"]
    escaped = html_mod.escape(lst)
    # Only a pending double-opt-in may be confirmed. A confirm link is a GET
    # that link scanners and prefetchers follow, so it must never resurrect a
    # subscriber who has since unsubscribed, bounced, or complained.
    if row["status"] == "unconfirmed":
        await db.execute(
            "UPDATE subscribers SET status = 'active', confirmed_at = ? WHERE id = ?",
            (utcnow(), subscriber_id),
        )
        await db.commit()
        return _page(
            request,
            "You're subscribed",
            f"<p>Welcome to <strong>{escaped}</strong>. Your first issue is on its way.</p>",
            list_name=lst,
        )
    if row["status"] == "active":
        return _page(
            request,
            "You're subscribed",
            f"<p>You are already subscribed to <strong>{escaped}</strong>.</p>",
            list_name=lst,
        )
    return _page(
        request,
        "Subscription closed",
        f"<p>This address is not subscribed to {escaped}. "
        f"Sign up again from the website if that is a mistake.</p>",
        variant="muted",
        list_name=lst,
    )


async def _subscriber_list_name(db, subscriber_id: str) -> str:
    async with db.execute(
        "SELECT l.name AS list_name FROM subscribers s JOIN lists l ON l.id = s.list_id WHERE s.id = ?",
        (subscriber_id,),
    ) as cur:
        row = await cur.fetchone()
    return row["list_name"] if row else "Museletter"


@router.get("/unsubscribe/{token}")
async def unsubscribe_page(request: Request, token: str):
    # GET must not mutate: link scanners and prefetchers follow every URL in an
    # email, and a destructive GET would silently unsubscribe real readers.
    subscriber_id = verify_token(request.app.state.secret, token, "unsubscribe")
    if subscriber_id is None:
        return _page(request, "Invalid link", "<p>This unsubscribe link is not valid.</p>", variant="muted")
    list_name = await _subscriber_list_name(request.app.state.db, subscriber_id)
    action = (
        f'<form method="post" action="/unsubscribe/{html_mod.escape(token)}">'
        '<button class="btn btn-danger" type="submit">Unsubscribe</button></form>'
    )
    return _page(
        request,
        "Unsubscribe?",
        f"<p>You will stop receiving emails from <strong>{html_mod.escape(list_name)}</strong>. "
        "This takes effect immediately.</p>",
        variant="gray",
        list_name=list_name,
        action=action,
    )


@router.post("/unsubscribe/{token}")
async def unsubscribe(request: Request, token: str):
    db = request.app.state.db
    subscriber_id = verify_token(request.app.state.secret, token, "unsubscribe")
    if subscriber_id is None:
        return _page(request, "Invalid link", "<p>This unsubscribe link is not valid.</p>", variant="muted")
    list_name = await _subscriber_list_name(db, subscriber_id)
    async with db.execute("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,)) as cur:
        row = await cur.fetchone()
    if row is not None and row["status"] != "unsubscribed":
        await db.execute(
            "UPDATE subscribers SET status = 'unsubscribed', unsubscribed_at = ? WHERE id = ?",
            (utcnow(), subscriber_id),
        )
        await db.execute(
            "UPDATE campaign_recipients SET status = 'suppressed', updated_at = ? "
            "WHERE subscriber_id = ? AND status = 'pending'",
            (utcnow(), subscriber_id),
        )
        await db.commit()
    return _page(
        request,
        "Unsubscribed",
        "<p>You won't receive these emails anymore. You can re-subscribe any time from the website.</p>",
        list_name=list_name,
    )


@router.post("/webhooks/sns")
async def sns_webhook(request: Request):
    db = request.app.state.db
    settings = request.app.state.settings
    try:
        envelope = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid JSON") from None
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=400, detail="invalid SNS message")

    # A valid SNS signature only proves the message came from *some* AWS SNS
    # topic. Pin it to our own topic so an attacker can't sign a fake bounce
    # from a topic in their own account and suppress our subscribers.
    if settings.sns_topic_arn and envelope.get("TopicArn") != settings.sns_topic_arn:
        raise HTTPException(status_code=403, detail="unexpected SNS topic")

    if not settings.extra.get("skip_sns_verify"):
        if not await request.app.state.sns_verifier.verify(envelope):
            raise HTTPException(status_code=403, detail="SNS signature verification failed")

    msg_type = envelope.get("Type", "")

    if msg_type == "SubscriptionConfirmation":
        subscribe_url = envelope.get("SubscribeURL", "")
        if not is_amazon_sns_url(subscribe_url):
            raise HTTPException(status_code=400, detail="invalid SubscribeURL")
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(subscribe_url)
        return {"ok": True, "confirmed": True}

    if msg_type != "Notification":
        return {"ok": True, "ignored": msg_type}

    message = envelope.get("Message", "")
    events = parse_ses_events(message)
    if not events:
        await apply_events(
            db,
            [{"type": "other", "email": "", "message_id": "", "permanent": False, "detail": str(message)}],
        )
        return {"ok": True, "processed": 0}

    # SES events carry the message id every ledger row stored at send time, so
    # no email fallback here: an id that matches nothing must stay a no-op.
    await apply_events(db, events)
    return {"ok": True, "processed": len(events)}
