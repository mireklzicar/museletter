"""Signup context (MUSELETTER_RECORD_SIGNUP_CONTEXT): IP, user agent, origin,
referrer and source stored once per subscriber, shown in the API and the CSV."""

import csv
import io

import httpx
import pytest

from conftest import AUTH, make_settings
from museletter.app import create_app

BROWSER = {
    "Origin": "https://site.example",
    "Referer": "https://site.example/news",
    "User-Agent": "TestBrowser/1.0",
    "X-Forwarded-For": "203.0.113.7, 10.0.0.1",
}


@pytest.fixture
async def recording(tmp_path):
    async def build(record: bool = True, **overrides):
        settings = make_settings(tmp_path)
        settings.opt_in = "single"
        settings.trust_proxy = True
        settings.record_signup_context = record
        for key, value in overrides.items():
            setattr(settings, key, value)
        app = create_app(settings)
        ctx = app.router.lifespan_context(app)
        await ctx.__aenter__()
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.local")
        opened.append((client, ctx))
        return app, client

    opened: list = []
    yield build
    for client, ctx in opened:
        await client.aclose()
        await ctx.__aexit__(None, None, None)


async def only_subscriber(client) -> dict:
    response = await client.get("/v1/lists/default/subscribers", headers=AUTH)
    (subscriber,) = response.json()["subscribers"]
    return subscriber


async def test_public_signup_records_context(recording):
    _, client = await recording()
    response = await client.post(
        "/subscribe/default", json={"email": "Reader@Example.com", "source": "footer"}, headers=BROWSER
    )
    assert response.status_code == 200
    subscriber = await only_subscriber(client)
    assert subscriber["email"] == "reader@example.com"
    assert subscriber["status"] == "active"
    assert subscriber["signup"] == {
        "ip": "203.0.113.7",
        "user_agent": "TestBrowser/1.0",
        "origin": "https://site.example",
        "referrer": "https://site.example/news",
        "source": "footer",
    }


async def test_context_is_kept_from_the_first_signup(recording):
    _, client = await recording()
    await client.post(
        "/subscribe/default", json={"email": "r@example.com", "source": "first"}, headers=BROWSER
    )
    other = {**BROWSER, "X-Forwarded-For": "198.51.100.9", "User-Agent": "Other/2.0"}
    await client.post(
        "/subscribe/default", json={"email": "r@example.com", "source": "second"}, headers=other
    )
    signup = (await only_subscriber(client))["signup"]
    assert signup["ip"] == "203.0.113.7"
    assert signup["source"] == "first"


async def test_nothing_recorded_when_disabled(recording):
    _, client = await recording(record=False)
    await client.post(
        "/subscribe/default", json={"email": "r@example.com", "source": "footer"}, headers=BROWSER
    )
    assert set((await only_subscriber(client))["signup"].values()) == {None}


async def test_honeypot_stores_nothing(recording):
    _, client = await recording()
    response = await client.post(
        "/subscribe/default", json={"email": "bot@example.com", "website": "spam"}, headers=BROWSER
    )
    assert response.status_code == 200
    listing = await client.get("/v1/lists/default/subscribers", headers=AUTH)
    assert listing.json()["subscribers"] == []


async def test_backend_signup_forwards_context(recording):
    _, client = await recording()
    response = await client.post(
        "/v1/lists/default/subscribe",
        json={"email": "r@example.com", "source": "waitlist", "ip": "192.0.2.4", "user_agent": "UA"},
        headers=AUTH,
    )
    assert response.status_code == 202
    signup = (await only_subscriber(client))["signup"]
    assert (signup["ip"], signup["user_agent"], signup["source"]) == ("192.0.2.4", "UA", "waitlist")
    assert signup["origin"] is None


async def test_export_includes_history_and_context(recording):
    _, client = await recording()
    await client.post(
        "/subscribe/default", json={"email": "r@example.com", "source": "footer"}, headers=BROWSER
    )
    export = await client.get("/v1/lists/default/subscribers/export", headers=AUTH)
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "r@example.com"
    assert row["confirmed_at"]
    assert row["unsubscribed_at"] == ""
    assert row["signup_ip"] == "203.0.113.7"
    assert row["signup_origin"] == "https://site.example"
    assert row["signup_source"] == "footer"

    # The export imports back: extra columns are ignored, duplicates skipped.
    reimport = await client.post(
        "/v1/lists/default/subscribers/import", json={"csv": export.text}, headers=AUTH
    )
    assert reimport.json() == {"imported": 0, "skipped_existing": 1, "skipped_invalid": 0}


async def test_existing_database_gains_the_columns(tmp_path):
    import aiosqlite

    from museletter.db import open_db

    path = tmp_path / "old.db"
    async with aiosqlite.connect(path) as db:
        await db.executescript(
            "CREATE TABLE subscribers (id TEXT PRIMARY KEY, list_id TEXT NOT NULL, email TEXT NOT NULL, "
            "name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'unconfirmed', "
            "created_at TEXT NOT NULL, confirmed_at TEXT, unsubscribed_at TEXT, UNIQUE (list_id, email));"
        )
    db = await open_db(str(path))
    async with db.execute("PRAGMA table_info(subscribers)") as cur:
        columns = {row["name"] for row in await cur.fetchall()}
    await db.close()
    assert {"signup_ip", "signup_user_agent", "signup_origin", "signup_referrer", "signup_source"} <= columns


async def test_public_pages_drop_museletter_branding_without_attribution(recording):
    from museletter.tokens import make_token

    for attribution, expected in ((True, True), (False, False)):
        app, client = await recording(attribution=attribution)
        page = await client.get(f"/unsubscribe/{make_token(app.state.secret, 'unsubscribe', 'sub_missing')}")
        assert page.status_code == 200
        assert ("Powered by" in page.text) is expected
        assert ('class="mark"' in page.text) is expected
