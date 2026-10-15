import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import typer

from . import clientconf

app = typer.Typer(help="Museletter: a headless, agent-first newsletter engine", no_args_is_help=True)
lists_app = typer.Typer(help="Manage lists", no_args_is_help=True)
subs_app = typer.Typer(help="Manage subscribers", no_args_is_help=True)
tags_app = typer.Typer(help="Manage tags", no_args_is_help=True)
campaigns_app = typer.Typer(help="Manage campaigns", no_args_is_help=True)
templates_app = typer.Typer(help="Manage email templates", no_args_is_help=True)
suppressions_app = typer.Typer(help="Manage the suppression list", no_args_is_help=True)
skill_app = typer.Typer(help="Install the museletter agent skill", no_args_is_help=True)
service_app = typer.Typer(help="Run Museletter as a background service", no_args_is_help=True)
profiles_app = typer.Typer(help="Manage saved server profiles", no_args_is_help=True)
app.add_typer(lists_app, name="lists", rich_help_panel="Client commands")
app.add_typer(subs_app, name="subs", rich_help_panel="Client commands")
app.add_typer(tags_app, name="tags", rich_help_panel="Client commands")
app.add_typer(campaigns_app, name="campaigns", rich_help_panel="Client commands")
app.add_typer(templates_app, name="templates", rich_help_panel="Client commands")
app.add_typer(suppressions_app, name="suppressions", rich_help_panel="Client commands")
app.add_typer(skill_app, name="skill", rich_help_panel="Client commands")
app.add_typer(service_app, name="service", rich_help_panel="Server commands")
app.add_typer(profiles_app, name="profiles", rich_help_panel="Client commands")

STATE: dict = {"json": False, "profile": None}


def _version_callback(value: bool) -> None:
    if value:
        from . import __version__

        typer.echo(f"museletter {__version__}")
        raise typer.Exit()


@app.callback()
def _global(
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON responses"),
    profile: str = typer.Option(
        None,
        "--profile",
        "-p",
        help="Server profile to use (overrides MUSELETTER_URL and MUSELETTER_API_KEY)",
    ),
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit"
    ),
):
    STATE["json"] = json_output
    STATE["profile"] = profile


@app.command("help", rich_help_panel="Client commands")
def show_help(ctx: typer.Context):
    """Show commands and options."""
    if ctx.parent is not None:
        typer.echo(ctx.parent.get_help())


def _resolve():
    try:
        return clientconf.resolve(STATE["profile"])
    except clientconf.ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc


def _list_ref(explicit: str | None) -> str:
    """Which list a command targets: an explicit --list, else MUSELETTER_LIST,
    else the profile's active list (`lists use`), else 'default'."""
    if explicit:
        return explicit
    if os.environ.get("MUSELETTER_LIST"):
        return os.environ["MUSELETTER_LIST"]
    _, _, source = _resolve()
    if source == "env":  # env-configured server has no saved profile
        return "default"
    return clientconf.active_list(STATE["profile"]) or "default"


# Shared --list option: defaults to the active list (`lists use`) when omitted.
_LIST_OPT = typer.Option(None, "--list", help="newsletter slug (default: the active list)")


def _client() -> httpx.Client:
    url, api_key, _ = _resolve()
    return httpx.Client(base_url=url, headers={"Authorization": f"Bearer {api_key}"}, timeout=120)


def _request(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
    with _client() as client:
        resp = client.request(method, path, json=body, headers=headers)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        typer.echo(f"error ({resp.status_code}): {detail}", err=True)
        raise typer.Exit(1)
    return resp


def _call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    resp = _request(method, path, body, headers)
    return resp.json() if resp.content else {}


def _emit(data, human: str | None = None) -> None:
    if STATE["json"] or human is None:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(human)


# Colors for the human-readable reports. Styling auto-strips when output is not
# a TTY (piped, or the --json path), so machine consumers are unaffected.
_STATUS_COLORS = {
    "active": "green", "unconfirmed": "yellow", "unsubscribed": "bright_black",
    "bounced": "red", "complained": "magenta",
    "draft": "bright_black", "sending": "cyan", "sent": "green",
    "scheduled": "yellow", "canceled": "bright_black", "failed": "red",
}  # fmt: skip
_DELIVERY_COLORS = {
    "delivered": "green", "sent": "cyan", "awaiting": "cyan", "pending": "bright_black",
    "bounced": "red", "complained": "magenta", "failed": "red", "suppressed": "yellow",
}  # fmt: skip


def _table(rows: list[dict], columns: list[str]) -> str:
    if not rows:
        return "(none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = "  ".join(typer.style(c.upper().ljust(widths[c]), bold=True) for c in columns)
    lines = [header]
    for r in rows:
        cells = []
        for c in columns:
            text = str(r.get(c, "")).ljust(widths[c])
            color = _STATUS_COLORS.get(str(r.get(c, ""))) if c == "status" else None
            cells.append(typer.style(text, fg=color) if color else text)
        lines.append("  ".join(cells))
    return "\n".join(lines)


def _short_ts(iso: str | None) -> str:
    return iso.replace("T", " ")[:16] if iso else "-"


def _bar(count: int, total: int, width: int = 22) -> str:
    filled = max(0, min(width, round(count / max(total, 1) * width)))
    return "█" * filled + "░" * (width - filled)


def _breakdown(counts: dict, order: list[str], total: int, colors: dict) -> str:
    """One row per status: colored label, right-aligned count, percent, and bar."""
    total = max(total, 1)
    label_w = max((len(s) for s in order), default=0)
    shown = {s: f"{counts.get(s, 0):,}" for s in order}
    num_w = max((len(v) for v in shown.values()), default=1)
    lines = []
    for s in order:
        n = counts.get(s, 0)
        color = colors.get(s)
        label = typer.style(s.ljust(label_w), fg=color)
        bar = typer.style(_bar(n, total), fg=color)
        lines.append(f"  {label}  {shown[s].rjust(num_w)}  {n / total * 100:5.1f}%  {bar}")
    return "\n".join(lines)


_SENT_STATUSES = ("delivered", "sent", "bounced", "complained")
_UNSENT_STATUSES = ("pending", "failed", "suppressed")
_RECIPIENT_STATUSES = _SENT_STATUSES + _UNSENT_STATUSES
_DELIVERY_NOTE = (
    "  Delivery bars and percentages are of recipients.\n  awaiting = sent, awaiting delivery confirmation"
)


def _campaign_percent(count: int, total: int) -> str:
    percent = count / max(total, 1) * 100
    if 0 < percent < 0.1:
        return "<0.1%"
    if 99.9 < percent < 100:
        return ">99.9%" if round(percent, 2) == 100 else f"{percent:.2f}%"
    return f"{percent:.1f}%"


def _campaign_breakdown(counts: dict, total: int) -> str:
    rows = [
        ("", "sent", sum(counts.get(s, 0) for s in _SENT_STATUSES)),
        ("├─ ", "delivered", counts.get("delivered", 0)),
        ("├─ ", "awaiting", counts.get("sent", 0)),
        ("├─ ", "bounced", counts.get("bounced", 0)),
        ("└─ ", "complained", counts.get("complained", 0)),
        *(("", s, counts.get(s, 0)) for s in _UNSENT_STATUSES),
    ]
    label_w = max(len(prefix + label) for prefix, label, _ in rows)
    num_w = max(len(f"{count:,}") for _, _, count in rows)
    lines = []
    for prefix, label, count in rows:
        if label == "pending":
            lines.append("")
        color = _DELIVERY_COLORS[label]
        name = typer.style(prefix, fg="bright_black") + typer.style(
            label.ljust(label_w - len(prefix)), fg=color
        )
        bar = typer.style(_bar(count, total), fg=color)
        lines.append(f"  {name}  {count:>{num_w},}  {_campaign_percent(count, total):>6}  {bar}")
    return "\n".join(lines)


def _render_opens(data: dict, stats: dict) -> str:
    if data.get("track_opens") is False:
        return "  open tracking: off"
    if "unique_opens" not in stats:
        return ""
    return (
        f"  opens: {stats['unique_opens']:,} unique  ·  {stats['total_opens']:,} total"
        f"  ·  open rate {stats['open_rate']:.1f}% of sent (estimated)"
    )


def _render_campaign_stats(data: dict) -> str:
    total = data.get("recipient_count") or sum(data.get(s, 0) for s in _RECIPIENT_STATUSES)
    header = (
        typer.style(data["id"], bold=True)
        + "  status "
        + typer.style(data["status"], fg=_STATUS_COLORS.get(data["status"]))
        + typer.style(f"  ·  {total:,} recipients", dim=True)
    )
    times = [
        t
        for t in (
            f"started {_short_ts(data['started_at'])}" if data.get("started_at") else "",
            f"completed {_short_ts(data['completed_at'])}" if data.get("completed_at") else "",
        )
        if t
    ]
    parts = [header]
    if times:
        parts.append(typer.style("  " + "  ·  ".join(times), dim=True))
    parts += ["", _campaign_breakdown(data, total), ""]
    parts.append(
        "  "
        + typer.style("delivery ", dim=True)
        + typer.style(_campaign_percent(data.get("delivered", 0), total), fg="green")
        + typer.style("   bounce ", dim=True)
        + typer.style(_campaign_percent(data.get("bounced", 0), total), fg="red")
        + typer.style("   complaint ", dim=True)
        + typer.style(_campaign_percent(data.get("complained", 0), total), fg="magenta")
    )
    if opens := _render_opens(data, data):
        parts.append(opens)
    parts += ["", typer.style(_DELIVERY_NOTE, dim=True)]
    return "\n".join(parts)


def _render_campaign_detail(data: dict) -> str:
    parts = [
        typer.style(data["subject"], bold=True),
        typer.style(data["id"], dim=True)
        + "  status "
        + typer.style(data["status"], fg=_STATUS_COLORS.get(data["status"])),
    ]
    meta = [f"recipients: {data.get('recipient_count', 0):,}"]
    if data.get("tag"):
        meta.append(f"tag: {data['tag']}")
    if "track_opens" in data:
        meta.append(f"open tracking: {'on' if data['track_opens'] else 'off'}")
    meta.append(f"created {_short_ts(data.get('created_at'))}")
    parts.append(typer.style("  " + "  ·  ".join(meta), dim=True))
    stats = data.get("stats")
    if stats:
        total = data.get("recipient_count") or sum(stats.get(s, 0) for s in _RECIPIENT_STATUSES)
        parts += ["", _campaign_breakdown(stats, total)]
        if opens := _render_opens(data, stats):
            parts.append(opens)
        parts += ["", typer.style(_DELIVERY_NOTE, dim=True)]
    return "\n".join(parts)


def _render_list_detail(data: dict) -> str:
    order = ["active", "unconfirmed", "unsubscribed", "bounced", "complained"]
    subs = data.get("subscribers", {})
    total = sum(subs.get(s, 0) for s in order)
    parts = [typer.style(data["name"], bold=True) + typer.style(f"  ({data['slug']})", dim=True)]
    if data.get("subscribe_url"):
        parts.append(typer.style("  subscribe: ", dim=True) + data["subscribe_url"])
    parts += ["", _breakdown(subs, order, total, _STATUS_COLORS), ""]
    parts.append(
        "  "
        + typer.style(f"{total:,} subscribers", bold=True)
        + typer.style(f"  ·  {subs.get('active', 0):,} active", fg="green")
    )
    return "\n".join(parts)


def _render_subscriber(data: dict) -> str:
    head = typer.style(data["email"], bold=True)
    if data.get("name"):
        head += f"  {data['name']}"
    parts = [
        head,
        typer.style(data["id"], dim=True)
        + "  status "
        + typer.style(data["status"], fg=_STATUS_COLORS.get(data["status"])),
    ]
    if data.get("tags"):
        parts.append(typer.style("  tags: ", dim=True) + ", ".join(data["tags"]))
    ts = [
        t
        for t in (
            f"joined {_short_ts(data['created_at'])}" if data.get("created_at") else "",
            f"confirmed {_short_ts(data['confirmed_at'])}" if data.get("confirmed_at") else "",
            f"unsubscribed {_short_ts(data['unsubscribed_at'])}" if data.get("unsubscribed_at") else "",
        )
        if t
    ]
    if ts:
        parts.append(typer.style("  " + "  ·  ".join(ts), dim=True))
    return "\n".join(parts)


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a .env into the environment without a shell, so
    the same unquoted file that `docker --env-file` reads also works for a local
    `serve`. Existing environment variables win; surrounding quotes are tolerated."""
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            os.environ.setdefault(key, val)


@app.command(rich_help_panel="Client commands")
def connect(
    token: str = typer.Argument(None, help="connect token (ml_...) from `museletter print-token`"),
    url: str = typer.Option(None, help="server URL (instead of a token)"),
    api_key: str = typer.Option(None, help="admin API key (with --url)"),
    name: str = typer.Option("default", "--name", help="profile name to save under"),
):
    """Connect this machine to a Museletter server and save it as a profile."""
    if token:
        try:
            url, api_key = clientconf.decode_token(token)
        except clientconf.ConfigError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    elif not url or not api_key:
        typer.echo("provide a connect token, or both --url and --api-key", err=True)
        raise typer.Exit(1)

    # Verify reachability and auth before saving, so a bad token fails loudly here.
    try:
        with httpx.Client(base_url=url.rstrip("/"), timeout=15) as client:
            health = client.get("/health")
            health.raise_for_status()
            authed = client.get("/v1/lists", headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        typer.echo(f"could not reach a Museletter server at {url}: {exc}", err=True)
        raise typer.Exit(1) from exc
    if authed.status_code == 401:
        typer.echo(f"reached {url} but the API key was rejected", err=True)
        raise typer.Exit(1)
    authed.raise_for_status()

    clientconf.save_profile(name, url, api_key)
    typer.echo(f"connected to {url} (profile '{name}', saved to {clientconf.CONFIG_PATH})")


@profiles_app.command("list")
def profiles_list():
    """List saved server profiles (the default is marked with *)."""
    config = clientconf.load_config()
    profs = config.get("profiles", {})
    default = config.get("default")
    if STATE["json"]:
        typer.echo(
            json.dumps(
                {
                    "default": default,
                    "profiles": {n: {"url": p.get("url"), "list": p.get("list")} for n, p in profs.items()},
                },
                indent=2,
            )
        )
        return
    if not profs:
        typer.echo("no profiles; run `museletter connect <token>`")
        return
    rows = [
        {"name": ("* " if n == default else "  ") + n, "url": p.get("url", ""), "list": p.get("list") or "-"}
        for n, p in profs.items()
    ]
    typer.echo(_table(rows, ["name", "url", "list"]))


@profiles_app.command("use")
def profiles_use(name: str):
    """Switch the default server profile."""
    try:
        clientconf.set_default_profile(name)
    except clientconf.ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"default profile is now '{name}'")


@profiles_app.command("rm")
def profiles_rm(name: str):
    """Remove a saved server profile."""
    try:
        clientconf.remove_profile(name)
    except clientconf.ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"removed profile '{name}'")


@app.command("print-token", rich_help_panel="Server commands")
def print_token():
    """Print a connect token for this server's configured URL + API key (run on the server)."""
    from .config import Settings

    settings = Settings.from_env()
    if not settings.base_url or not settings.api_key:
        typer.echo(
            "MUSELETTER_BASE_URL and MUSELETTER_API_KEY must be set to print a connect token",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(clientconf.encode_token(settings.base_url, settings.api_key))


@app.command(rich_help_panel="Client commands")
def status():
    """Show the configured server, its version, and connection status."""
    try:
        url, api_key, source = clientconf.resolve(STATE["profile"])
    except clientconf.ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    info: dict = {"url": url, "source": source, "version": None, "reachable": False, "authenticated": False}
    try:
        with httpx.Client(base_url=url, timeout=15) as client:
            health = client.get("/health")
            info["reachable"] = health.status_code == 200
            if info["reachable"]:
                try:
                    health_data = health.json()
                except ValueError:
                    health_data = {}
                if isinstance(health_data, dict):
                    info["version"] = health_data.get("version")
                lists = client.get("/v1/lists", headers={"Authorization": f"Bearer {api_key}"})
                info["authenticated"] = lists.status_code == 200
                if info["authenticated"]:
                    data = lists.json()["lists"]
                    info["lists"] = len(data)
                    info["active_subscribers"] = sum(item.get("active_subscribers", 0) for item in data)
                    info["by_list"] = [
                        {"slug": item.get("slug", "-"), "active": item.get("active_subscribers", 0)}
                        for item in data
                    ]
    except httpx.HTTPError as exc:
        info["error"] = str(exc)

    if STATE["json"]:
        typer.echo(json.dumps(info, indent=2))
    else:
        yn = lambda ok: typer.style("yes", fg="green") if ok else typer.style("no", fg="red")  # noqa: E731
        active = clientconf.active_list(STATE["profile"]) if source.startswith("profile:") else None
        typer.echo(f"server:        {info['url']}  " + typer.style(f"({source})", dim=True))
        typer.echo("version:       " + (info["version"] or "unknown"))
        typer.echo(f"reachable:     {yn(info['reachable'])}")
        typer.echo(f"authenticated: {yn(info['authenticated'])}")
        typer.echo("active list:   " + (active or typer.style("default", dim=True)))
        if info.get("authenticated"):
            typer.echo("active subs:   " + typer.style(str(info["active_subscribers"]), bold=True))
            for item in info.get("by_list", []):
                pointer = "→" if item["slug"] == active else " "
                typer.echo(f"  {pointer} " + item["slug"].ljust(20) + str(item["active"]))
    raise typer.Exit(0 if info["authenticated"] else 1)


@app.command(rich_help_panel="Server commands")
def serve(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8000, help="bind port"),
    env_file: Path = typer.Option(
        None, "--env-file", help="load config from a .env first (the same file docker --env-file uses)"
    ),
):
    """Run the Museletter server."""
    from .app import create_app
    from .config import Settings

    if env_file:
        _load_env_file(env_file)
    settings = Settings.from_env()
    problems = settings.missing_required()
    if problems:
        typer.echo("cannot start; fix configuration first:", err=True)
        for p in problems:
            typer.echo(f"  - {p}", err=True)
        raise typer.Exit(1)

    import uvicorn

    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


@app.command(rich_help_panel="Client commands")
def doctor():
    """Check DNS, email provider account status, and configuration."""
    data = _call("GET", "/v1/doctor")
    if STATE["json"]:
        typer.echo(json.dumps(data, indent=2))
        raise typer.Exit(0 if data["status"] != "fail" else 1)
    icons = {
        "ok": typer.style("✓", fg="green"),
        "warn": typer.style("!", fg="yellow"),
        "fail": typer.style("✗", fg="red"),
    }
    colors = {"ok": "green", "warn": "yellow", "fail": "red"}
    for check in data["checks"]:
        typer.echo(f"{icons[check['status']]} [{check['name']}] {check['detail']}")
    typer.echo("\noverall: " + typer.style(data["status"], fg=colors.get(data["status"]), bold=True))
    raise typer.Exit(0 if data["status"] != "fail" else 1)


@app.command(rich_help_panel="Client commands")
def health():
    """Check that the server is up."""
    data = _call("GET", "/health")
    label = typer.style("ok", fg="green") if data.get("ok") else typer.style("down", fg="red")
    _emit(data, f"{label}  {data.get('name', 'museletter')} {data.get('version', '')}".rstrip())


def _read_readme() -> str:
    from importlib.resources import files

    bundled = files("museletter") / "README.md"  # present in an installed wheel
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    repo_readme = Path(__file__).resolve().parent.parent.parent / "README.md"  # editable checkout
    if repo_readme.is_file():
        return repo_readme.read_text(encoding="utf-8")
    return "README not found; see https://github.com/sanketsaurav/museletter"


@app.command(rich_help_panel="Client commands")
def docs():
    """Print the full Museletter manual (the README), for humans and agents alike."""
    text = _read_readme()
    if sys.stdout.isatty():
        import pydoc

        pydoc.pager(text)
    else:
        typer.echo(text)


_TEMPLATE_FILES = ["email.html", "email-system.html", "page.html"]

_PREVIEW_INDEX = """<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Museletter preview</title>$favicon
<style>
:root{--bg:#f5f5f7;--fg:#1a1a1e;--sub:#6b6b74;--h2:#8a8a92;--frame:#e1e1e6;--card:#fff}
html[data-theme="dark"]{--bg:#0b0b0d;--fg:#f5f5f7;--sub:#9b9ba6;--h2:#9b9ba6;--frame:#2e2e35;--card:#111114}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;max-width:1000px;padding:0 40px 48px;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
.bar{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:16px;background:var(--bg);padding:20px 0 14px;border-bottom:1px solid var(--frame)}
h1{font-size:20px;margin:0}.sub{color:var(--sub);margin:16px 0 0;font-size:14px}
.head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:32px 0 10px}
h2{font-size:13px;color:var(--h2);margin:0;font-weight:600}
.open{color:var(--sub);font-size:12px;text-decoration:none;white-space:nowrap}.open:hover{color:var(--fg)}
.frame{border:1px solid var(--frame);border-radius:10px;overflow:hidden;background:var(--card)}
iframe{width:100%;border:0;display:block;background:var(--card)}.email iframe{height:640px}.page iframe{height:430px}
.seg{display:inline-flex;flex:none;border:1px solid var(--frame);border-radius:8px;overflow:hidden}
.seg button{appearance:none;-webkit-appearance:none;border:0;background:transparent;color:var(--sub);font:inherit;font-size:13px;padding:7px 15px;cursor:pointer}
.seg button.on{background:var(--fg);color:var(--bg)}</style>
</head><body>
<div class="bar"><h1>Museletter public surfaces</h1>
<div class="seg"><button data-theme="light">Light</button><button data-theme="dark">Dark</button></div></div>
<p class="sub">Rendered from the current templates, forced to each mode so you can review both regardless of your system theme.</p>
$rows
<script>
(function(){
  var root=document.documentElement;
  var btns=document.querySelectorAll('.seg button');
  var frames=document.querySelectorAll('iframe[data-base]');
  var links=document.querySelectorAll('a.open[data-base]');
  function file(el,theme){return el.getAttribute('data-base')+(theme==='dark'?'.dark.html':'.html')}
  function apply(theme){
    root.setAttribute('data-theme',theme);
    btns.forEach(function(b){b.classList.toggle('on',b.getAttribute('data-theme')===theme)});
    frames.forEach(function(f){var s=file(f,theme);if(f.getAttribute('src')!==s)f.setAttribute('src',s)});
    links.forEach(function(a){a.setAttribute('href',file(a,theme))});
  }
  btns.forEach(function(b){b.addEventListener('click',function(){apply(b.getAttribute('data-theme'))})});
  var dark=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;
  apply(dark?'dark':'light');
})();
</script>
</body></html>"""


_DARK_MEDIA = "@media (prefers-color-scheme: dark)"


def _theme_variants(html: str) -> tuple[str, str]:
    """Split a rendered surface into OS-independent light and dark renderings so
    the preview can force either mode. Dark promotes the prefers-color-scheme
    block to always-on (@media all); light removes it. The email templates guard
    their dark rules with !important, so forcing the block overrides the inline
    light styles."""
    dark = html.replace(_DARK_MEDIA, "@media all")
    light = html
    while (start := light.find(_DARK_MEDIA)) != -1:
        brace = light.find("{", start)
        if brace == -1:
            break
        depth, end = 0, brace
        while end < len(light):
            if light[end] == "{":
                depth += 1
            elif light[end] == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        light = light[:start] + light[end + 1 :]
    return light, dark


# The single-asterisk mark (matches page.html), for the browser-tab favicon.
_PREVIEW_FAVICON = (
    '<link rel="icon" href="data:image/svg+xml,'
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' fill='%230F7A6B'%3E"
    "%3Crect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(0 16 16)'/%3E"
    "%3Crect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(60 16 16)'/%3E"
    "%3Crect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(120 16 16)'/%3E"
    '%3C/svg%3E">'
)


def _finalize_preview(html: str, label: str) -> str:
    """Give a preview file a branded browser tab: a descriptive title, and the
    Museletter mark favicon for surfaces that carry none of their own (the
    emails; the pages already ship one)."""
    title = f"<title>{label} · Museletter</title>"
    html = re.sub(r"<title>.*?</title>", lambda _m: title, html, count=1, flags=re.S)
    if 'rel="icon"' not in html:
        html = html.replace("</title>", "</title>" + _PREVIEW_FAVICON, 1)
    return html


def _email_mark(fill: str) -> str:
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32' fill='{fill}'>"
        "<rect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(0 16 16)'/>"
        "<rect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(60 16 16)'/>"
        "<rect x='2.40' y='13.25' width='27.20' height='5.5' rx='2.75' transform='rotate(120 16 16)'/></svg>"
    )
    return "data:image/svg+xml," + quote(svg)


def _inline_email_marks(html: str) -> str:
    """Swap the CDN-hosted email mark for inline data-URI marks so a rendered
    email is self-contained in a browser (the CDN 404s while the repo is private,
    and previews should not depend on the network)."""
    base = "https://cdn.jsdelivr.net/gh/sanketsaurav/museletter@master/assets/email"
    return html.replace(f"{base}/mark-light.png", _email_mark("#0F7A6B")).replace(
        f"{base}/mark-dark.png", _email_mark("#5FC9B6")
    )


@app.command(rich_help_panel="Server commands")
def preview(
    out: str = typer.Option(None, "--out", help="directory to write previews to (default: a temp dir)"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="open the previews in a browser"),
    eject: str = typer.Option(None, "--eject", help="copy the templates to this dir to customize them"),
):
    """Preview every public surface (emails and pages), or eject the templates to customize."""
    import tempfile
    import webbrowser
    from pathlib import Path

    from .render import template_source

    if eject:
        dest = Path(eject)
        dest.mkdir(parents=True, exist_ok=True)
        for f in _TEMPLATE_FILES:
            (dest / f).write_text(template_source(f), encoding="utf-8")
        typer.echo(f"ejected {len(_TEMPLATE_FILES)} templates to {dest}")
        typer.echo(f"edit them, then run the server (or preview) with MUSELETTER_TEMPLATE_DIR={dest}")
        return

    from .api.public import _render_page
    from .config import Settings
    from .render import SAMPLE_ISSUE_MARKDOWN, SAMPLE_ISSUE_SUBJECT, build_email, render_confirmation

    def page_html(*a, **k) -> str:
        # HTMLResponse.body is typed bytes | memoryview; coerce before decode.
        return bytes(_render_page(*a, attribution=attribution, **k).body).decode()

    ln, addr = "Your Newsletter", "123 Main St, Your City"
    # Read from the environment like MUSELETTER_TEMPLATE_DIR, so the preview
    # shows the footer the server would actually send.
    attribution = Settings.from_env().attribution
    _, issue_html, _ = build_email(
        SAMPLE_ISSUE_SUBJECT,
        SAMPLE_ISSUE_MARKDOWN,
        name="Ada Lovelace",
        email="ada@example.com",
        unsubscribe_url="#",
        list_name=ln,
        postal_address=addr,
        attribution=attribution,
    )
    _, confirm_html, _ = render_confirmation(
        list_name=ln, confirm_url="#", postal_address=addr, attribution=attribution
    )
    unsub_action = '<form method="post" action="#"><button class="btn btn-danger" type="submit">Unsubscribe</button></form>'
    surfaces = [
        ("email-issue", "Campaign / issue email", _inline_email_marks(issue_html)),
        ("email-confirm", "Confirmation email", _inline_email_marks(confirm_html)),
        (
            "page-subscribed",
            "Page: subscribed",
            page_html(
                "You're subscribed",
                f"<p>Welcome to <strong>{ln}</strong>. Your first issue is on its way.</p>",
                list_name=ln,
            ),
        ),
        (
            "page-unsubscribe",
            "Page: unsubscribe confirm",
            page_html(
                "Unsubscribe?",
                f"<p>You will stop receiving emails from <strong>{ln}</strong>. This takes effect immediately.</p>",
                variant="gray",
                list_name=ln,
                action=unsub_action,
            ),
        ),
        (
            "page-unsubscribed",
            "Page: unsubscribed",
            page_html(
                "Unsubscribed",
                "<p>You won't receive these emails anymore. You can re-subscribe any time from the website.</p>",
                list_name=ln,
            ),
        ),
        (
            "page-closed",
            "Page: subscription closed",
            page_html(
                "Subscription closed",
                f"<p>This address is not subscribed to {ln}. Sign up again from the website if that is a mistake.</p>",
                variant="muted",
                list_name=ln,
            ),
        ),
        (
            "page-invalid",
            "Page: invalid link",
            page_html(
                "This link has gone stale",
                "<p>Confirmation links last 48 hours and work once. Sign up again from the website to get a fresh one.</p>",
                variant="muted",
                list_name="Museletter",
            ),
        ),
    ]
    out_dir = Path(out) if out else Path(tempfile.mkdtemp(prefix="museletter-preview-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    for slug, title, html in surfaces:
        light, dark = _theme_variants(_finalize_preview(html, title))
        (out_dir / f"{slug}.html").write_text(light, encoding="utf-8")
        (out_dir / f"{slug}.dark.html").write_text(dark, encoding="utf-8")
    rows = "".join(
        f'<div class="head"><h2>{title}</h2>'
        f'<a class="open" data-base="{slug}" target="_blank" rel="noopener">open full page &#8599;</a></div>'
        f'<div class="frame {"email" if slug.startswith("email") else "page"}">'
        f'<iframe data-base="{slug}" title="{title}"></iframe></div>'
        for slug, title, _ in surfaces
    )
    index = out_dir / "index.html"
    index.write_text(
        _PREVIEW_INDEX.replace("$favicon", _PREVIEW_FAVICON).replace("$rows", rows), encoding="utf-8"
    )
    typer.echo(f"wrote {len(surfaces)} surfaces (light + dark) to {out_dir}")
    if open_browser:
        webbrowser.open(index.as_uri())


# ---------- lists ----------


@lists_app.command("list")
def lists_list():
    """Show all lists."""
    data = _call("GET", "/v1/lists")
    _emit(data, _table(data["lists"], ["id", "slug", "name", "active_subscribers", "template"]))


@lists_app.command("use")
def lists_use(
    slug: str = typer.Argument(None, help="list slug to make active; omit or 'none' to clear"),
):
    """Pin the newsletter that --list defaults to for this server (like a current branch)."""
    clear = slug in (None, "", "none")
    if not clear:
        slug = _call("GET", f"/v1/lists/{slug}")["slug"]  # 404s if the slug is unknown
    try:
        name = clientconf.set_active_list(None if clear else slug, STATE["profile"])
    except clientconf.ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if clear:
        typer.echo(f"cleared active list for profile '{name}' (commands now default to 'default')")
    else:
        typer.echo(f"active list for profile '{name}' is now '{slug}'")


@lists_app.command("create")
def lists_create(name: str, slug: str = typer.Option(None)):
    body = {"name": name}
    if slug:
        body["slug"] = slug
    data = _call("POST", "/v1/lists", body)
    _emit(data, f"created list {data['id']} (slug: {data['slug']})")


@lists_app.command("show")
def lists_show(ref: str):
    """Show a list with its subscriber breakdown by status."""
    data = _call("GET", f"/v1/lists/{ref}")
    _emit(data, _render_list_detail(data))


@lists_app.command("edit")
def lists_edit(
    ref: str,
    name: str = typer.Option(None, "--name", help="new display name (the publication name)"),
    slug: str = typer.Option(None, "--slug", help="new URL slug"),
    template: str = typer.Option(
        None, "--template", help="default email template for this list ('default' for the built-in)"
    ),
):
    """Rename a list, change its slug, or set its default email template."""
    body = {}
    if name is not None:
        body["name"] = name
    if slug is not None:
        body["slug"] = slug
    if template is not None:
        body["template"] = _template_body_value(template)
    if not body:
        typer.echo("nothing to update (pass --name, --slug, and/or --template)", err=True)
        raise typer.Exit(1)
    data = _call("PATCH", f"/v1/lists/{ref}", body)
    template_note = f", template: {data['template']}" if data.get("template") else ""
    _emit(data, f"updated list {data['id']} (name: {data['name']}, slug: {data['slug']}{template_note})")


@lists_app.command("rm")
def lists_rm(ref: str, yes: bool = typer.Option(False, "--yes", "-y")):
    if not yes and not typer.confirm(f"delete list '{ref}' and all its subscribers?"):
        raise typer.Exit(1)
    _call("DELETE", f"/v1/lists/{ref}")
    typer.echo(f"deleted {ref}")


# ---------- subscribers ----------


def _resolve_subscriber(list_ref: str, ref: str) -> str:
    if ref.startswith("sub_"):
        return ref
    data = _call("GET", f"/v1/lists/{list_ref}/subscribers?q={quote(ref, safe='')}&limit=5")
    matches = [s for s in data["subscribers"] if s["email"] == ref.lower().strip()]
    if not matches:
        typer.echo(f"no subscriber found for '{ref}' in list '{list_ref}'", err=True)
        raise typer.Exit(1)
    return matches[0]["id"]


@subs_app.command("add")
def subs_add(
    email: str,
    list_ref: str = _LIST_OPT,
    name: str = typer.Option("", "--name"),
    tag: list[str] = typer.Option([], "--tag"),
):
    list_ref = _list_ref(list_ref)
    data = _call("POST", f"/v1/lists/{list_ref}/subscribers", {"email": email, "name": name, "tags": tag})
    note = f" ({data['warning']})" if "warning" in data else ""
    _emit(data, f"added {data['email']} [{data['id']}] to {list_ref}{note}")


@subs_app.command("list")
def subs_list(
    list_ref: str = _LIST_OPT,
    status: str = typer.Option(None),
    tag: str = typer.Option(None),
    q: str = typer.Option(None),
    limit: int = typer.Option(100),
    offset: int = typer.Option(0),
):
    list_ref = _list_ref(list_ref)
    params = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    if tag:
        params["tag"] = tag
    if q:
        params["q"] = q
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    data = _call("GET", f"/v1/lists/{list_ref}/subscribers?{query}")
    rows = [{**s, "tags": ",".join(s["tags"])} for s in data["subscribers"]]
    human = _table(rows, ["id", "email", "name", "status", "tags"]) + f"\n({data['total']} total)"
    _emit(data, human)


@subs_app.command("show")
def subs_show(ref: str, list_ref: str = _LIST_OPT):
    """Show one subscriber: status, tags, and key dates."""
    list_ref = _list_ref(list_ref)
    sub_id = _resolve_subscriber(list_ref, ref)
    data = _call("GET", f"/v1/subscribers/{sub_id}")
    _emit(data, _render_subscriber(data))


@subs_app.command("rm")
def subs_rm(ref: str, list_ref: str = _LIST_OPT):
    list_ref = _list_ref(list_ref)
    sub_id = _resolve_subscriber(list_ref, ref)
    _call("DELETE", f"/v1/subscribers/{sub_id}")
    typer.echo(f"deleted {sub_id}")


@subs_app.command("tag")
def subs_tag(ref: str, tag: str, list_ref: str = _LIST_OPT):
    list_ref = _list_ref(list_ref)
    sub_id = _resolve_subscriber(list_ref, ref)
    data = _call("POST", f"/v1/subscribers/{sub_id}/tags", {"name": tag})
    _emit(data, f"tagged {data['email']}: {', '.join(data['tags'])}")


@subs_app.command("untag")
def subs_untag(ref: str, tag: str, list_ref: str = _LIST_OPT):
    list_ref = _list_ref(list_ref)
    sub_id = _resolve_subscriber(list_ref, ref)
    data = _call("DELETE", f"/v1/subscribers/{sub_id}/tags/{tag}")
    _emit(data, f"untagged {data['email']}: {', '.join(data['tags']) or '(no tags)'}")


@subs_app.command("import")
def subs_import(file: Path, list_ref: str = _LIST_OPT):
    """Import subscribers from a CSV file (email[,name,tags,status])."""
    list_ref = _list_ref(list_ref)
    csv_text = sys.stdin.read() if str(file) == "-" else file.read_text()
    data = _call("POST", f"/v1/lists/{list_ref}/subscribers/import", {"csv": csv_text})
    _emit(
        data,
        f"imported {data['imported']} into {list_ref}, skipped {data['skipped_existing']} existing, "
        f"{data['skipped_invalid']} invalid",
    )


@subs_app.command("export")
def subs_export(list_ref: str = _LIST_OPT):
    """Export subscribers as CSV to stdout."""
    list_ref = _list_ref(list_ref)
    typer.echo(_request("GET", f"/v1/lists/{list_ref}/subscribers/export").text, nl=False)


# ---------- tags ----------


@tags_app.command("list")
def tags_list(list_ref: str = _LIST_OPT):
    list_ref = _list_ref(list_ref)
    data = _call("GET", f"/v1/lists/{list_ref}/tags")
    _emit(data, _table(data["tags"], ["id", "name", "subscriber_count"]))


@tags_app.command("create")
def tags_create(name: str, list_ref: str = _LIST_OPT):
    list_ref = _list_ref(list_ref)
    data = _call("POST", f"/v1/lists/{list_ref}/tags", {"name": name})
    _emit(data, f"created tag {data['id']} ({data['name']})")


@tags_app.command("rm")
def tags_rm(name: str, list_ref: str = _LIST_OPT):
    """Delete a tag from a list (by name or id)."""
    list_ref = _list_ref(list_ref)
    tags = _call("GET", f"/v1/lists/{list_ref}/tags")["tags"]
    match = next((t for t in tags if name in (t["name"], t["id"])), None)
    if not match:
        typer.echo(f"no tag '{name}' in list '{list_ref}'", err=True)
        raise typer.Exit(1)
    _call("DELETE", f"/v1/tags/{match['id']}")
    typer.echo(f"deleted tag {match['name']}")


# ---------- campaigns ----------


@campaigns_app.command("list")
def campaigns_list(list_ref: str = typer.Option(None, "--list"), status: str = typer.Option(None)):
    params = []
    if list_ref:
        params.append(f"list={list_ref}")
    if status:
        params.append(f"status={status}")
    query = ("?" + "&".join(params)) if params else ""
    data = _call("GET", f"/v1/campaigns{query}")
    slugs = {lst["id"]: lst["slug"] for lst in _call("GET", "/v1/lists")["lists"]}
    rows = [
        {**c, "list": slugs.get(c["list_id"], "-"), "created_at": _short_ts(c.get("created_at"))}
        for c in data["campaigns"]
    ]
    _emit(data, _table(rows, ["id", "list", "subject", "status", "recipient_count", "created_at"]))


@campaigns_app.command("create")
def campaigns_create(
    subject: str = typer.Option(..., "--subject"),
    file: Path = typer.Option(..., "--file", help="markdown file, or - for stdin"),
    list_ref: str = _LIST_OPT,
    tag: str = typer.Option(None, "--tag", help="only send to subscribers with this tag"),
    template: str = typer.Option(
        None, "--template", help="email template for this campaign (default: the list's template)"
    ),
    track_opens: bool = typer.Option(
        True, "--track-opens/--no-track-opens", help="track opens with a per-recipient image"
    ),
):
    list_ref = _list_ref(list_ref)
    markdown = sys.stdin.read() if str(file) == "-" else file.read_text()
    body = {"subject": subject, "body_markdown": markdown}
    if tag:
        body["tag"] = tag
    if template:
        body["template"] = _template_body_value(template)
    if not track_opens:
        body["track_opens"] = False
    data = _call("POST", f"/v1/lists/{list_ref}/campaigns", body)
    _emit(data, f'created draft {data["id"]}: "{data["subject"]}"')


@campaigns_app.command("show")
def campaigns_show(campaign_id: str):
    """Show a campaign with its delivery report (once sent)."""
    data = _call("GET", f"/v1/campaigns/{campaign_id}")
    _emit(data, _render_campaign_detail(data))


@campaigns_app.command("edit")
def campaigns_edit(
    campaign_id: str,
    subject: str = typer.Option(None, "--subject"),
    file: Path = typer.Option(None, "--file"),
    tag: str = typer.Option(None, "--tag"),
    template: str = typer.Option(
        None, "--template", help="email template for this campaign ('none' to use the list's template)"
    ),
    track_opens: bool | None = typer.Option(
        None, "--track-opens/--no-track-opens", help="enable or disable open tracking"
    ),
):
    body = {}
    if subject is not None:
        body["subject"] = subject
    if file is not None:
        body["body_markdown"] = sys.stdin.read() if str(file) == "-" else file.read_text()
    if tag is not None:
        body["tag"] = tag
    if template is not None:
        body["template"] = _template_body_value(template)
    if track_opens is not None:
        body["track_opens"] = track_opens
    if not body:
        typer.echo("nothing to update", err=True)
        raise typer.Exit(1)
    data = _call("PATCH", f"/v1/campaigns/{campaign_id}", body)
    _emit(data, f"updated {data['id']} (test send required again before sending)")


@campaigns_app.command("preview")
def campaigns_preview(campaign_id: str, html_out: Path = typer.Option(None, "--html")):
    data = _call("GET", f"/v1/campaigns/{campaign_id}/preview")
    if html_out:
        # Inline the mark so the saved preview renders standalone in a browser.
        html_out.write_text(_inline_email_marks(data["html"]))
        typer.echo(f"wrote {html_out}")
    else:
        _emit(data, f"subject: {data['subject']}\n\n{data['text']}")


@campaigns_app.command("test")
def campaigns_test(campaign_id: str, to: str = typer.Option(..., "--to")):
    data = _call("POST", f"/v1/campaigns/{campaign_id}/test", {"to": to})
    _emit(data, f"test sent to {data['sent_to']}")


@campaigns_app.command("send")
def campaigns_send(
    campaign_id: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    skip_test: bool = typer.Option(False, "--skip-test"),
):
    """Send a campaign to its audience (asks for confirmation)."""
    preview = _call("POST", f"/v1/campaigns/{campaign_id}/send", {"dry_run": True})
    if dry_run:
        _emit(
            preview,
            f"would send to {preview['recipient_count']} subscribers\nsample: {', '.join(preview['sample'])}",
        )
        return
    if not yes and not typer.confirm(f"send to {preview['recipient_count']} subscribers?"):
        raise typer.Exit(1)
    data = _call(
        "POST",
        f"/v1/campaigns/{campaign_id}/send",
        {"confirm": True, "skip_test": skip_test},
        headers={"Idempotency-Key": f"send-{campaign_id}"},
    )
    _emit(
        data,
        f"sending to {data['recipient_count']} subscribers; check `museletter campaigns stats {campaign_id}`",
    )


@campaigns_app.command("stats")
def campaigns_stats(campaign_id: str):
    """Show a campaign's delivery report with rates."""
    data = _call("GET", f"/v1/campaigns/{campaign_id}/stats")
    _emit(data, _render_campaign_stats(data))


@campaigns_app.command("rm")
def campaigns_rm(campaign_id: str, yes: bool = typer.Option(False, "--yes", "-y")):
    if not yes and not typer.confirm(f"delete campaign {campaign_id}?"):
        raise typer.Exit(1)
    _call("DELETE", f"/v1/campaigns/{campaign_id}")
    typer.echo(f"deleted {campaign_id}")


# ---------- templates ----------


def _template_body_value(ref: str) -> str:
    """CLI sentinel: --template none clears the setting (inherit / built-in)."""
    return "" if ref == "none" else ref


@templates_app.command("list")
def templates_list():
    """Show all email templates (the built-in default plus your custom ones)."""
    data = _call("GET", "/v1/templates")
    rows = [
        {**t, "builtin": "yes" if t["builtin"] else "", "updated_at": _short_ts(t["updated_at"])}
        for t in data["templates"]
    ]
    _emit(data, _table(rows, ["name", "id", "builtin", "size", "updated_at"]))


@templates_app.command("create")
def templates_create(
    name: str,
    file: Path = typer.Option(None, "--file", help="template HTML file, or - for stdin"),
    from_ref: str = typer.Option(
        None, "--from", help="duplicate an existing template (e.g. 'default') instead of --file"
    ),
):
    """Create a template from an HTML file, or by copying an existing one."""
    if (file is None) == (from_ref is None):
        typer.echo("pass exactly one of --file or --from", err=True)
        raise typer.Exit(1)
    body: dict = {"name": name}
    if file is not None:
        body["html"] = sys.stdin.read() if str(file) == "-" else file.read_text()
    else:
        body["copy_of"] = from_ref
    data = _call("POST", "/v1/templates", body)
    _emit(data, f"created template {data['name']} ({data['id']})")


@templates_app.command("show")
def templates_show(ref: str, out: Path = typer.Option(None, "--out", help="write the HTML to a file")):
    """Show a template, or fetch its HTML with --out to edit locally."""
    data = _call("GET", f"/v1/templates/{ref}")
    if out is not None:
        out.write_text(data["html"])
        typer.echo(f"wrote {out}")
        return
    builtin = "  built-in (read-only; copy it to customize)" if data["builtin"] else ""
    human = (
        typer.style(data["name"], bold=True)
        + typer.style(f"  ({data['id']})", dim=True)
        + builtin
        + f"\nsize: {data['size']} bytes"
        + (f"  ·  updated {_short_ts(data['updated_at'])}" if data["updated_at"] else "")
        + f"\nfetch the HTML with: museletter templates show {data['name']} --out template.html"
    )
    _emit(data, human)


@templates_app.command("edit")
def templates_edit(
    ref: str,
    file: Path = typer.Option(None, "--file", help="new template HTML file, or - for stdin"),
    name: str = typer.Option(None, "--name", help="rename the template"),
):
    """Update a template's HTML (drafts using it will need a fresh test send) or rename it."""
    body: dict = {}
    if file is not None:
        body["html"] = sys.stdin.read() if str(file) == "-" else file.read_text()
    if name is not None:
        body["name"] = name
    if not body:
        typer.echo("nothing to update (pass --file and/or --name)", err=True)
        raise typer.Exit(1)
    data = _call("PATCH", f"/v1/templates/{ref}", body)
    note = " (campaigns using it need a fresh test send)" if "html" in body else ""
    _emit(data, f"updated template {data['name']}{note}")


@templates_app.command("test")
def templates_test(ref: str, to: str = typer.Option(..., "--to")):
    """Email yourself a sample issue rendered through the template."""
    data = _call("POST", f"/v1/templates/{ref}/test", {"to": to})
    _emit(data, f"test of template '{data['template']}' sent to {data['sent_to']}")


@templates_app.command("rm")
def templates_rm(ref: str, yes: bool = typer.Option(False, "--yes", "-y")):
    if not yes and not typer.confirm(f"delete template '{ref}'?"):
        raise typer.Exit(1)
    _call("DELETE", f"/v1/templates/{ref}")
    typer.echo(f"deleted {ref}")


# ---------- suppressions ----------


@suppressions_app.command("list")
def suppressions_list():
    data = _call("GET", "/v1/suppressions")
    _emit(
        data, _table(data["suppressions"], ["email", "reason", "created_at"]) + f"\n({data['total']} total)"
    )


@suppressions_app.command("add")
def suppressions_add(email: str):
    data = _call("POST", "/v1/suppressions", {"email": email})
    _emit(data, f"suppressed {data['email']}")


@suppressions_app.command("rm")
def suppressions_rm(email: str):
    _call("DELETE", f"/v1/suppressions/{email}")
    typer.echo(f"removed {email} from suppressions")


# ---------- skill ----------


@skill_app.command("install")
def skill_install(
    project: bool = typer.Option(
        False, "--project", help="install into ./.claude/skills instead of ~/.claude"
    ),
    directory: str = typer.Option(None, "--dir", help="explicit skills directory to install into"),
    force: bool = typer.Option(False, "--force", help="overwrite an existing install"),
):
    """Install the bundled agent skill into a Claude Code skills directory."""
    import shutil
    from importlib.resources import as_file, files

    if directory:
        base = Path(directory)
    elif project:
        base = Path.cwd() / ".claude" / "skills"
    else:
        base = Path.home() / ".claude" / "skills"
    target = base / "museletter"

    if target.exists() and not force:
        typer.echo(f"{target} already exists; pass --force to overwrite", err=True)
        raise typer.Exit(1)

    source = files("museletter") / "skill"
    with as_file(source) as skill_dir:
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill_dir, target)
    typer.echo(f"installed skill to {target}")


# ---------- init ----------


@app.command(rich_help_panel="Server commands")
def init(
    base_url: str = typer.Option(None, "--base-url", help="public URL of the server"),
    from_email: str = typer.Option(None, "--from-email", help="sender address"),
    from_name: str = typer.Option("", "--from-name", help="sender display name"),
    postal_address: str = typer.Option("", "--postal-address", help="footer postal address"),
    provider: str = typer.Option("ses", "--provider", help="email provider: ses or cloudflare"),
    region: str = typer.Option("us-east-1", "--region", help="AWS region (ses provider)"),
    cloudflare_account_id: str = typer.Option(
        "", "--cloudflare-account-id", help="Cloudflare account id (cloudflare provider)"
    ),
    api_key: str = typer.Option(None, "--api-key", help="admin API key (generated if omitted)"),
    env_file: str = typer.Option(".env", "--env-file", help="path to write env to; '-' to print only"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="never prompt; use flags/defaults"),
):
    """Bootstrap a server: generate an API key, write an env file, print a connect token."""
    import secrets

    provider = provider.strip().lower()
    if provider not in ("ses", "cloudflare"):
        typer.echo("--provider must be 'ses' or 'cloudflare'", err=True)
        raise typer.Exit(1)
    if not non_interactive:
        base_url = base_url or typer.prompt("Public base URL (e.g. https://news.example.com)")
        from_email = from_email or typer.prompt("Sender email (on your verified sending domain)")
        from_name = from_name or typer.prompt("Sender name", default="")
        postal_address = postal_address or typer.prompt("Postal address (CAN-SPAM)", default="")
        if provider == "cloudflare":
            cloudflare_account_id = cloudflare_account_id or typer.prompt("Cloudflare account id")
    if not base_url or not from_email:
        typer.echo("--base-url and --from-email are required", err=True)
        raise typer.Exit(1)
    if provider == "cloudflare" and not cloudflare_account_id:
        typer.echo("--cloudflare-account-id is required with --provider cloudflare", err=True)
        raise typer.Exit(1)
    api_key = api_key or secrets.token_hex(32)

    env = {
        "MUSELETTER_API_KEY": api_key,
        "MUSELETTER_BASE_URL": base_url.rstrip("/"),
        "MUSELETTER_FROM_EMAIL": from_email,
        "MUSELETTER_FROM_NAME": from_name,
        "MUSELETTER_POSTAL_ADDRESS": postal_address,
    }
    if provider == "cloudflare":
        env["MUSELETTER_EMAIL_PROVIDER"] = "cloudflare"
        env["CLOUDFLARE_ACCOUNT_ID"] = cloudflare_account_id
        # Left empty on purpose: fill it in after creating the events queue.
        env["MUSELETTER_CLOUDFLARE_EVENTS_QUEUE_ID"] = ""
    else:
        env["AWS_REGION"] = region
    # Written unquoted so `docker --env-file` reads it correctly (it keeps quotes
    # literally); `museletter serve --env-file` and systemd read this format too.
    env_text = "".join(f"{k}={v}\n" for k, v in env.items())
    token = clientconf.encode_token(base_url, api_key)

    # Write the env file in every mode (it's the command's side effect); only the
    # rendering differs between --json and human output.
    wrote_path = None
    if env_file != "-":
        path = Path(env_file)
        path.write_text(env_text)
        path.chmod(0o600)
        wrote_path = str(path)

    if STATE["json"]:
        typer.echo(json.dumps({"env": env, "connect_token": token, "env_file": wrote_path}))
        return

    if env_file == "-":
        typer.echo(env_text)
    else:
        typer.echo(f"wrote {wrote_path} (keep it secret)")
    typer.echo("\nStart the server with this env, then connect a client with:\n")
    typer.echo(f"  museletter connect {token}\n")
    if provider == "cloudflare":
        typer.echo(
            "Set CLOUDFLARE_API_TOKEN too (Email Sending + Queues permissions), then run `museletter doctor`."
        )
    else:
        typer.echo(
            "Set your AWS credentials (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY) too, "
            "then run `museletter doctor`."
        )


# ---------- service ----------


@service_app.command("install")
def service_install(
    host: str = typer.Option("127.0.0.1", help="bind address"),
    port: int = typer.Option(8000, help="bind port"),
    env_file: str = typer.Option(".env", "--env-file", help="env file the service loads"),
    start: bool = typer.Option(True, "--start/--no-start", help="start the service after installing"),
):
    """Install Museletter as a launchd (macOS) or systemd (Linux) user service."""
    from . import service

    try:
        path = service.install(host=host, port=port, env_file=str(Path(env_file).resolve()), start=start)
    except service.ServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"installed service unit at {path}" + (" and started it" if start else ""))


@service_app.command("uninstall")
def service_uninstall():
    """Stop and remove the Museletter user service."""
    from . import service

    try:
        service.uninstall()
    except service.ServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("service removed")


@service_app.command("restart")
def service_restart():
    """Restart the service (e.g. after upgrading the package)."""
    from . import service

    try:
        service.restart()
    except service.ServiceError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo("service restarted")


@service_app.command("status")
def service_status():
    """Show whether the Museletter user service is installed and running."""
    from . import service

    typer.echo(service.status())


def main():
    app()


if __name__ == "__main__":
    main()
