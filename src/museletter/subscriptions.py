"""Shared signup flow for public forms and authenticated website backends."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request

from .db import SIGNUP_CONTEXT_COLUMNS, new_id, utcnow
from .events import record_event, suppress
from .render import render_confirmation
from .tokens import make_token


async def subscribe_address(
    request: Request,
    lst,
    email: str,
    name: str,
    *,
    double_opt_in: bool,
    context: dict[str, str] | None = None,
) -> dict:
    """`context` maps SIGNUP_CONTEXT_COLUMNS to values; it is stored only when
    the operator enables record_signup_context, and only on the first signup."""
    db = request.app.state.db
    settings = request.app.state.settings
    recorded = context if settings.record_signup_context and context else {}
    signup = [recorded.get(column) or None for column in SIGNUP_CONTEXT_COLUMNS]
    ok = (
        {"status": "pending_confirmation", "message": "Check your inbox to confirm your subscription."}
        if double_opt_in
        else {"status": "subscribed", "message": "You're subscribed."}
    )

    async with db.execute("SELECT 1 FROM suppressions WHERE email = ?", (email,)) as cur:
        if await cur.fetchone():
            return ok

    now = utcnow()
    # The unique (list_id, email) constraint also deduplicates concurrent signups.
    await db.execute(
        "INSERT INTO subscribers (id, list_id, email, name, status, created_at, confirmed_at, "
        f"{', '.join(SIGNUP_CONTEXT_COLUMNS)}) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?{', ?' * len(SIGNUP_CONTEXT_COLUMNS)}) "
        "ON CONFLICT(list_id, email) DO NOTHING",
        (
            new_id("sub"),
            lst["id"],
            email,
            name,
            "unconfirmed" if double_opt_in else "active",
            now,
            None if double_opt_in else now,
            *signup,
        ),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM subscribers WHERE list_id = ? AND email = ?", (lst["id"], email)
    ) as cur:
        subscriber = await cur.fetchone()
    if subscriber is None:
        raise HTTPException(status_code=503, detail="subscription temporarily unavailable")

    # Never downgrade active readers or reopen an opt-out. Re-subscription needs
    # a separately versioned confirmation token, not the old link in an inbox.
    if subscriber["status"] != "unconfirmed":
        return ok
    subscriber_id = subscriber["id"]
    if name:
        await db.execute("UPDATE subscribers SET name = ? WHERE id = ?", (name, subscriber_id))
    if not double_opt_in:
        await db.execute(
            "UPDATE subscribers SET status = 'active', confirmed_at = ? WHERE id = ? "
            "AND status = 'unconfirmed'",
            (utcnow(), subscriber_id),
        )
        await db.commit()
        return ok

    # Claim the cooldown before the network call so two concurrent requests
    # cannot both send. A rejected send releases its own claim for a retry.
    claimed_at = utcnow()
    cutoff = (datetime.now(UTC) - timedelta(seconds=settings.confirmation_cooldown)).isoformat()
    async with db.execute(
        "UPDATE subscribers SET confirmation_sent_at = ? WHERE id = ? AND status = 'unconfirmed' "
        "AND (confirmation_sent_at IS NULL OR julianday(confirmation_sent_at) <= julianday(?)) "
        "AND NOT EXISTS (SELECT 1 FROM suppressions WHERE email = ?) RETURNING id",
        (claimed_at, subscriber_id, cutoff, email),
    ) as cur:
        claimed = await cur.fetchone()
    await db.commit()
    if claimed is None:
        return ok

    try:
        confirm_url = (
            f"{settings.base_url}/confirm/{make_token(request.app.state.secret, 'confirm', subscriber_id)}"
        )
        subject, html, text = render_confirmation(
            list_name=lst["name"],
            confirm_url=confirm_url,
            postal_address=settings.postal_address,
            attribution=settings.attribution,
        )
        result = await request.app.state.mailer.send_email(
            email,
            subject,
            html,
            text,
            from_email=settings.from_email,
            from_name=settings.from_name,
            reply_to=settings.reply_to,
        )
    except Exception as exc:
        await db.execute(
            "UPDATE subscribers SET confirmation_sent_at = NULL WHERE id = ? AND confirmation_sent_at = ?",
            (subscriber_id, claimed_at),
        )
        await db.commit()
        raise HTTPException(
            status_code=502, detail="could not send the confirmation email; try again later"
        ) from exc
    if result.outcome == "bounced":
        detail = result.detail or "confirmation rejected by provider"
        await suppress(db, email, "bounce", detail)
        await record_event(db, "bounce", email, result.message_id, detail)
        await db.commit()
    return ok
