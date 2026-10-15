<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/museletter-lockup-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/museletter-lockup.svg">
  <img alt="Museletter" src="https://raw.githubusercontent.com/sanketsaurav/museletter/master/assets/museletter-lockup.png" height="40">
</picture>

<p></p>

Museletter is a **headless**, **agent-first** newsletter engine. One container, one SQLite
database, and your email provider: Amazon SES or Cloudflare Email Service.
There is no web UI. You (or your AI agent) operate it through a CLI and an
HTTP API.

I'm building Museletter to use it on my website [sanketsaurav.com](https://sanketsaurav.com?ref=gh_museletter). It has all essential primitives for running a professional newsletter:

- subscribers, lists, and tags (with CSV import/export)
- a public subscribe endpoint with double opt-in
- Markdown campaigns rendered into a clean email template
- RFC 8058 one-click unsubscribe, injected automatically on every send
- automatic bounce/complaint suppression, fed by SES+SNS webhooks or
  Cloudflare queue events
- a crash-safe send ledger that respects provider rate limits and resumes mid-blast
- per-campaign delivery stats and open tracking
- `museletter doctor`, which checks DNS, DKIM, DMARC, provider account/quota, config

## How it works

Three layers:

1. **The server** (`museletter serve`): a FastAPI app over one SQLite file.
   Its HTTP surface has two audiences. The **public endpoints**
   (`/subscribe`, `/confirm`, `/unsubscribe`, `/open`, `/webhooks/sns`) must be
   reachable from the internet, because readers click links in their inbox and
   Amazon SNS posts delivery events to the webhook (on Cloudflare, delivery
   events arrive by polling a queue instead: no inbound webhook). The
   **admin API** (`/v1/*`, bearer-authenticated) only needs to be reachable
   by you.
2. **The CLI** (`museletter`): the same binary runs the server *and* is the
   admin client for a running server, local or remote.
3. **The skill**: a bundled set of recipes so an agent can drive the CLI. See
   [Agent-first design](#agent-first-design).

## Install

```bash
pip install museletter          # the CLI and server
```

On macOS, Homebrew works too:

```bash
brew install sanketsaurav/tap/museletter
```

Or run the server as a container (see [Deployment](#deployment)):

```bash
docker run ghcr.io/sanketsaurav/museletter:latest
```

## Quickstart

Museletter is two installs: the **server** on an always-on host, and the
**CLI** on your machine pointed at that server. On a single machine (a Mac
mini, say) they are the same install talking to `localhost`.

### 1. Bootstrap and run the server

On the host, generate config (this writes a `.env` and prints a connect
token):

```bash
museletter init --base-url https://news.example.com --from-email you@example.com
```

Add your AWS credentials to the `.env` (see [AWS SES setup](#aws-ses-setup-once);
to send through Cloudflare instead, pass `--provider cloudflare` and see
[Cloudflare Email Service setup](#cloudflare-email-service-setup-once)), then
start the server with that environment. Any container host works; the
simplest is Docker:

```bash
docker run -d --env-file .env -v museletter:/data -p 8000:8000 \
  ghcr.io/sanketsaurav/museletter:latest
```

Or run it directly without a container (`--env-file` loads the `.env` init just
wrote; no shell sourcing needed):

```bash
museletter serve --env-file .env
```

`museletter init` prints a **connect token**: one `ml_...` blob that encodes
the server URL and admin API key. Copy it.

### 2. Point your CLI at the server

On your laptop:

```bash
pip install museletter
museletter connect ml_...        # paste the token; verifies reachability + auth
museletter skill install         # drop the agent skill into ~/.claude/skills
museletter doctor                # confirm provider, DNS, and config are healthy
museletter status                # server version, reachability, subscriber counts
```

`connect` saves a named profile in `~/.config/museletter/config.toml`. Manage
several servers with `--name` on connect and `--profile` (`-p`) before the
command, for example `museletter -p second status`. An explicit profile takes
precedence over the server URL and API key in your environment.

### 3. Send your first issue

```bash
museletter subs add reader@example.com --name "First Reader"
museletter campaigns create --subject "Hello" --file issue.md
museletter campaigns preview cmp_xxx                     # review it (or --html out.html)
museletter campaigns test cmp_xxx --to you@example.com   # test send to yourself
museletter campaigns send cmp_xxx --dry-run              # show the audience
museletter campaigns send cmp_xxx                        # asks to confirm
museletter campaigns stats cmp_xxx                       # delivery stats and opens
```

Every command accepts `--json`. The HTTP API is browsable at `/docs` and
authenticates with `Authorization: Bearer <api key>`.

Campaign reports group delivered, awaiting confirmation, bounced, and complained
emails under a cumulative `sent` total. Pending, failed, and suppressed emails
appear separately. Delivery bars and percentages use the total recipient count.
In `--json` and API responses, `sent` keeps its raw meaning: emails awaiting a
delivery outcome, excluding those already delivered, bounced, or complained.

### Campaign open tracking

New campaigns track opens by default through a signed, per-recipient 1x1 image
in the HTML email. This works with both SES and Cloudflare, including custom
templates. Preview, test, and confirmation emails do not contain tracking pixels;
plain-text emails cannot report opens. Links are left unchanged.

Use `campaigns stats <id>` or `campaigns show <id>` to see opens. The stats API
and `--json` output include:

- `track_opens`: whether tracking is enabled for the campaign.
- `unique_opens`: recipients whose pixel has loaded at least once.
- `total_opens`: all recorded pixel loads, including repeats.
- `open_rate`: the percentage of recipients in `sent`, `delivered`, `bounced`,
  or `complained` state whose pixel has loaded, rounded to two decimal places.
  Pending, failed, and suppressed recipients are excluded from both sides of
  this rate. The rate is zero until there are sent recipients.

Opens are estimates of image loads, not proof that a person read the email.
[Apple Mail Privacy Protection](https://www.apple.com/legal/privacy/data/en/mail-privacy-protection/)
can preload images without the reader opening a message. Image blocking can
hide opens, while caching can hide repeat opens. Forwarded messages retain the
original recipient's pixel. The recipient ledger stores counts and first/last
open times, without IP addresses or user agents.
Opens never change delivery status or suppressions.

To disable tracking on a new campaign or an existing draft:

```bash
museletter campaigns create --subject "Hello" --file issue.md --no-track-opens
museletter campaigns edit cmp_xxx --no-track-opens
museletter campaigns edit cmp_xxx --track-opens           # re-enable on a draft
```

The API accepts `track_opens: false` on campaign create or update. Changing a
draft clears its test-send flag as usual. Once sending starts, the setting is
fixed. Upgrading preserves existing data: old drafts enable tracking, while
campaigns already queued or sent remain untracked. Previously sent emails
cannot gain tracking retroactively.

Allow public GET requests to `/open/<token>.gif` under `MUSELETTER_BASE_URL`
through your reverse proxy or tunnel, and disable caching for this route. The
endpoint requires no admin key and sends cache-prevention headers. HEAD requests
do not record opens.

## AWS SES setup (once)

SES is the default provider (`MUSELETTER_EMAIL_PROVIDER=ses`) and has to be
set up once; prefer Cloudflare? See
[Cloudflare Email Service setup](#cloudflare-email-service-setup-once). All of
this is scriptable, and the bundled skill has a copy-paste recipe
(`museletter skill install`, then see `recipes/aws-ses-setup.md`).

1. **Verify your sending domain** (creates DKIM keys):

   ```bash
   aws sesv2 create-email-identity --email-identity example.com
   aws sesv2 get-email-identity --email-identity example.com \
     --query 'DkimAttributes.Tokens'
   ```

   Add each returned token as a CNAME:
   `<token>._domainkey.example.com -> <token>.dkim.amazonses.com`.

2. **Add a DMARC record.** Gmail and Yahoo require one for bulk senders. A
   minimal TXT record on `_dmarc.example.com`: `v=DMARC1; p=none`.

3. **Wire bounce/complaint events back to Museletter.** Without this, bounces
   are never suppressed and SES will eventually suspend your account.

   ```bash
   aws sesv2 create-configuration-set --configuration-set-name museletter
   aws sns create-topic --name museletter-events   # note the TopicArn
   aws sesv2 create-configuration-set-event-destination \
     --configuration-set-name museletter \
     --event-destination-name sns \
     --event-destination '{"Enabled":true,"MatchingEventTypes":["BOUNCE","COMPLAINT","DELIVERY"],"SnsDestination":{"TopicArn":"<TopicArn>"}}'
   aws sns subscribe --topic-arn <TopicArn> --protocol https \
     --notification-endpoint https://news.example.com/webhooks/sns
   ```

   Museletter auto-confirms the SNS subscription. Then set
   `MUSELETTER_SES_CONFIGURATION_SET=museletter` and
   `MUSELETTER_SNS_TOPIC_ARN=<TopicArn>` in your environment. Setting the topic
   ARN is important: it makes the webhook reject events from any other topic.

4. **Request production access.** New SES accounts are sandboxed (only verified
   recipients, 200/day). This is a support form in the SES console.

5. **Minimal IAM policy** for the server's credentials: `ses:SendEmail`,
   `ses:GetAccount`, `ses:GetEmailIdentity`.

Run `museletter doctor` at any point; it reports exactly which of these is
missing.

## Cloudflare Email Service setup (once)

Museletter can send through
[Cloudflare Email Service](https://developers.cloudflare.com/email-service/)
instead of SES. Email Sending is in public beta and requires a Workers Paid
plan; treat the provider as experimental until Cloudflare declares it GA.
Set `MUSELETTER_EMAIL_PROVIDER=cloudflare` and configure once:

1. **Verify your sending domain.** In the Cloudflare dashboard, open your zone
   and enable Email Sending (Email > Email Sending) on the domain or a
   subdomain such as `news.example.com`, then add the DNS records it shows
   (SPF + DKIM). `MUSELETTER_FROM_EMAIL` must be on that domain. Add the same
   DMARC record as with SES: `_dmarc.example.com TXT "v=DMARC1; p=none"`.

2. **Create an API token** (My Profile > API Tokens) with **Email Sending**
   write access on the account, plus **Queues** read + write for step 3. Set:

   ```bash
   CLOUDFLARE_API_TOKEN=<token>
   CLOUDFLARE_ACCOUNT_ID=<account id from the dashboard sidebar>
   ```

3. **Route delivery events to a queue.** Bounces and complaints arrive as
   queue messages and Museletter polls the queue over HTTPS: no Worker and no
   inbound webhook to host. Create a queue and register an HTTP pull consumer
   on it (the pull API rejects queues without one), then subscribe the queue
   to Email Sending events for your sending domain (`message.delivered`,
   `message.bounced`, `message.complained`, `message.failed`,
   `message.rejected`) from the queue's Event Subscriptions settings in the
   dashboard:

   ```bash
   npx wrangler queues create museletter-email-events
   npx wrangler queues consumer http add museletter-email-events
   ```

   Set `MUSELETTER_CLOUDFLARE_EVENTS_QUEUE_ID` to the queue id shown on the
   queue's page. Without it Museletter still sends, but bounces are only
   caught when Cloudflare reports them synchronously in the send response,
   and `doctor` warns.

4. **Run `museletter doctor`.** It verifies the token, the Email Sending API,
   and the events queue.

One provider quirk to know: the Cloudflare REST send API returns no
per-message id, so Museletter correlates delivery events by recipient
address. Each send goes to exactly one recipient, which keeps that
correlation narrow.

## Deployment

The server is a single stateless process plus one SQLite file. Run the
**Docker image** anywhere and mount a volume at `/data`. That is the primary
and best-supported path; the platform notes below are thin wrappers around it.

The one hard requirement on every platform: the **public endpoints must be
reachable from the internet** over HTTPS, and `MUSELETTER_BASE_URL` must be
that public URL (it goes into every confirm/unsubscribe link, open-tracking
pixel, and the SNS subscription).

### Docker (primary)

```bash
docker run -d --name museletter --restart unless-stopped \
  --env-file .env \
  -v museletter-data:/data \
  -p 8000:8000 \
  ghcr.io/sanketsaurav/museletter:latest
```

Build it yourself instead of pulling: `docker build -t museletter .`. The
database lives at `/data/museletter.db` (set by the image); back up that one
file and you have backed up everything. Put the container behind a reverse
proxy (Caddy, nginx, your platform's router) for TLS.

### Backups and durability

Everything lives in one SQLite file at `/data/museletter.db`, so backing it up
comes down to where you keep `/data`:

- put `/data` on a persistent or replicated volume (most hosts offer one), or
- run [Litestream](https://litestream.io) next to the container to stream the
  file to S3-compatible storage, which gets you point-in-time restore.

There's no remote-database option, by design: one local file is what keeps the
send ledger fast (the database is the queue) and the whole thing easy to move.

### Render (or any PaaS with a Dockerfile)

Create a service from this repo; Render detects the Dockerfile. Then:

1. Attach a **persistent disk** mounted at `/data`. Without it, a redeploy
   wipes your subscribers.
2. Set the environment variables from the table below.
3. Disable scale-to-zero / sleeping. The send loop runs in-process, so a
   sleeping instance pauses mid-campaign (it resumes safely, just late).

### A plain VPS

Any small box works. Run the container as above, or `pip install museletter`
and run `museletter serve` under systemd. Front it with Caddy for automatic
HTTPS:

```
news.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

### Self-host (Mac mini or any always-on machine)

Run the server locally and expose only the public endpoints through a tunnel
(a home LAN is not internet-reachable on its own). A
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
is the cleanest (no open ports, free, stable hostname); Tailscale Funnel and
ngrok work the same way. Point the tunnel at `http://127.0.0.1:8000` and set
`MUSELETTER_BASE_URL` to the tunnel's public hostname.

Keep it running across reboots with the built-in service installer (launchd on
macOS, a systemd user unit on Linux):

```bash
museletter service install --env-file .env    # starts on boot, restarts on crash
museletter service status
museletter service uninstall
```

Drive it from the same machine over localhost:
`museletter connect http://127.0.0.1:8000 --api-key <key>`.

## Updating

There is no auto-update; you update the two pieces yourself.

The **CLI** on your machine:

```bash
pip install -U museletter          # or: pipx upgrade museletter
```

The **server**:

- **Docker:** pull the new image and recreate the container. The database lives
  on the `/data` volume, so it survives:

  ```bash
  docker pull ghcr.io/sanketsaurav/museletter:latest
  docker rm -f museletter && docker run -d --name museletter \
    --env-file .env -v museletter-data:/data -p 8000:8000 \
    ghcr.io/sanketsaurav/museletter:latest
  ```

  Every release also publishes `:X.Y.Z` and `:X.Y` tags; pin to one for
  controlled upgrades instead of `:latest`.

- **pip or a service install:** `pip install -U museletter`, then
  `museletter service restart` (launchd/systemd) or restart your process.

Check versions with `museletter --version` (the CLI) and `museletter status`
(the running server). `museletter health` also reports the server version.

## Configuration

Set these in the server's environment (`museletter init` writes most of them).

| Variable | Required | Meaning |
|---|---|---|
| `MUSELETTER_API_KEY` | yes | admin credential (any long random string) |
| `MUSELETTER_BASE_URL` | yes | public URL used in confirm/unsubscribe links and open-tracking pixels |
| `MUSELETTER_FROM_EMAIL` | yes | sender address (on a domain verified with your provider) |
| `MUSELETTER_FROM_NAME` | no | sender display name |
| `MUSELETTER_REPLY_TO` | no | Reply-To address on all outgoing email; when unset, replies go to the from address |
| `MUSELETTER_POSTAL_ADDRESS` | no* | postal address in the footer (*required by CAN-SPAM) |
| `MUSELETTER_ATTRIBUTION` | no | `false` removes the "Sent with Museletter" line from email footers and the Museletter mark and "Powered by" line from public pages (default `true`) |
| `MUSELETTER_OPT_IN` | no | `double` (default) or `single` |
| `MUSELETTER_EMAIL_PROVIDER` | no | `ses` (default) or `cloudflare` |
| `MUSELETTER_SEND_RATE` | no | emails/sec, default 10; keep under your provider's rate |
| `MUSELETTER_SES_CONFIGURATION_SET` | no | ses: configuration set for event feedback |
| `MUSELETTER_SNS_TOPIC_ARN` | recommended | ses: your SNS topic ARN; the webhook rejects events from any other topic |
| `MUSELETTER_TRUST_PROXY` | no | `true` when behind a proxy, so rate limiting uses `X-Forwarded-For` not the proxy IP |
| `MUSELETTER_PUBLIC_SUBSCRIBE` | no | `false` disables public signup; backends can still use authenticated `/v1/lists/{ref}/subscribe` |
| `MUSELETTER_TURNSTILE_SECRET` | no | Cloudflare Turnstile secret; when set, `/subscribe` requires a valid Turnstile token |
| `MUSELETTER_RECORD_SIGNUP_CONTEXT` | no | `true` stores the signup's IP, user agent, `Origin`, `Referer` and optional `source` field with the subscriber (first signup only), shown in the API and the CSV export as evidence of consent (default `false`) |
| `MUSELETTER_CONFIRMATION_COOLDOWN` | no | min seconds between confirmation emails to one address, default 3600 |
| `MUSELETTER_TEMPLATE_DIR` | no | server-side directory overriding the packaged templates (issue templates are better managed with `museletter templates`) |
| `MUSELETTER_DB_PATH` | no | SQLite path, default `museletter.db` (the image uses `/data/museletter.db`) |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | yes (ses) | SES credentials |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | yes (cloudflare) | API token (Email Sending + Queues permissions) and account id |
| `MUSELETTER_CLOUDFLARE_EVENTS_QUEUE_ID` | recommended (cloudflare) | queue holding the Email Sending event subscription; feeds bounce/complaint suppression |
| `MUSELETTER_CLOUDFLARE_POLL_SECONDS` | no | cloudflare: seconds between event queue polls, default 30 |

The client CLI selects its server in this order:

1. An explicit `--profile <name>` (`-p <name>`).
2. `MUSELETTER_URL` with `MUSELETTER_API_KEY` from the environment.
3. The profile named by `MUSELETTER_PROFILE`.
4. The default profile in `~/.config/museletter/config.toml` (written by
   `museletter connect` or selected with `museletter profiles use`).

An explicit profile uses its saved URL and API key together, even when server
environment variables are set. An unknown explicit profile is an error.
List selection uses `--list`, then `MUSELETTER_LIST`, then the selected
profile's pinned list (`museletter lists use`), then `default`. A server
selected through `MUSELETTER_URL` has no pinned list.

## Connect your website

How you collect subscribers depends on whether your site has a backend, but both
paths feed the same `default` list (or any slug). Authenticated signup always
requires double opt-in. Public signup defaults to double opt-in;
`MUSELETTER_OPT_IN=single` skips its confirmation email. Campaign emails carry
a one-click RFC 8058 unsubscribe.

### If you have a backend (recommended when you can)

Request a confirmation email through the authenticated signup API, so your
API key never touches the browser:

```bash
curl -X POST https://news.example.com/v1/lists/default/subscribe \
  -H "Authorization: Bearer $MUSELETTER_API_KEY" \
  -H "content-type: application/json" \
  -d '{"email":"reader@example.com","name":"Reader"}'
```

This returns HTTP 202 with `status: "pending_confirmation"`. New readers are
stored as `unconfirmed` and become active only after clicking the email link,
even when `MUSELETTER_OPT_IN=single`. An optional `name` is accepted; a `status`
override is not. The list reference can be a slug or id.

Active, opted-out, and suppressed addresses receive the same response, with no
new email or status change. Pending readers share the public form's
`MUSELETTER_CONFIRMATION_COOLDOWN` (default one hour), including concurrent
requests. Provider errors return 502 and allow a retry. Existing opt-outs are
not reopened, so old confirmation links cannot reactivate them.

Validate your site's form and apply bot protection/rate limiting in your
backend before forwarding the request. This authenticated endpoint does not
require the public form's Turnstile token or share its per-IP rate limit.

The separate `POST /v1/lists/{ref}/subscribers` endpoint is for admin imports:
it does **not** send confirmation emails, including with `status: "unconfirmed"`.
Use `status: "active"` there only for readers whose consent you already have.

Deploy a server exposing the new signup route before switching your website
(releases through 1.4.0 do not have it). Once your backend uses it, you can turn
the public endpoint off entirely; confirmation and unsubscribe links stay live:

```bash
MUSELETTER_PUBLIC_SUBSCRIBE=false
```

### If you have a static site (no backend)

Point a form at the public `/subscribe/{slug}` endpoint. CORS is open, so
client-side JavaScript can call it directly from any domain. This keeps the
reader on your page and shows the result inline:

```html
<form id="newsletter">
  <input type="email" name="email" placeholder="you@example.com" required>
  <!-- honeypot: hidden from humans; bots fill it and are silently dropped -->
  <input type="text" name="website" tabindex="-1" autocomplete="off"
         style="position:absolute;left:-9999px" aria-hidden="true">
  <button type="submit">Subscribe</button>
  <p id="newsletter-msg"></p>
</form>

<script>
document.getElementById('newsletter').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const res = await fetch('https://news.example.com/subscribe/default', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: form.email.value, website: form.website.value }),
  });
  const data = await res.json();
  document.getElementById('newsletter-msg').textContent =
    res.ok ? data.message : (data.detail || 'Something went wrong.');
  if (res.ok) form.reset();
});
</script>
```

A plain `<form action="..." method="post">` works too, but without JavaScript
the browser navigates to the endpoint's JSON response, so the reader lands on a
raw JSON page. Use the fetch version above for real visitors.

The public endpoint is hardened against abuse: a honeypot field, a per-IP rate
limit, a per-address cooldown so it cannot be used to flood a victim with
confirmation emails, and a uniform response that does not reveal who is already
subscribed. For a high-traffic or targeted form, turn on
[Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/) (free): set
`MUSELETTER_TURNSTILE_SECRET`, add the Turnstile widget to your form, and the
widget's `cf-turnstile-response` token is verified on every submit.

## Sending a campaign

Campaign bodies are Markdown. Personalization tokens are `{{name}}`,
`{{first_name}}` (the first word of the name), and `{{email}}`, with fallbacks
like `{{first_name|there}}`. The unsubscribe footer and postal address are added
automatically; never write your own unsubscribe link. The footer ends with a
small "Sent with Museletter" line; set `MUSELETTER_ATTRIBUTION=false` on the
server to leave it out.

Preview the rendered result before sending: `campaigns preview <id>` prints the
plain-text version, and `campaigns preview <id> --html out.html` writes the full
HTML (self-contained, mark inlined) to open in a browser.

Sending is guarded so an automated caller cannot blast the wrong thing:

- `--dry-run` reports the audience size and a sample without sending.
- A **test send** is required before the real send (or pass `--skip-test`).
- The real send needs explicit confirmation (`--yes` for automation).
- The send is idempotent: re-running `campaigns send` for a campaign that is
  already sending does nothing.

Target a subset with tags: `campaigns create ... --tag vip` sends only to
subscribers carrying that tag. Pick a list with `--list <slug>` (a `default`
list exists out of the box).

## Look and feel

There are only a few reader-facing surfaces: two emails (the issue and the double
opt-in confirmation) and the public pages (subscribed, unsubscribe, invalid link,
and friends). Preview them all at once, rendered from the current templates with
sample data:

```bash
museletter preview            # writes them to a temp dir and opens a browser
```

The gallery has a light/dark toggle and an "open full page" link on each surface,
so you can check both themes and inspect any surface on its own.

The **publication name** on every surface is just the list's name, so rename it
with `museletter lists edit <slug> --name "Field Notes"`. The **mark and accent
color** are Museletter's brand by default. To change anything else, customize
the templates.

### Issue templates, from the CLI

The issue email's shell (layout, colors, logo - everything around your
Markdown) is a template, and templates live on the server and are managed
entirely through the CLI, so a running server never needs touching. Museletter
ships a built-in `default`; copy it, restyle the copy, and email yourself a
sample issue to judge it in a real inbox:

```bash
museletter templates create mine --from default   # duplicate the built-in
museletter templates show mine --out mine.html    # fetch the HTML to edit
# restyle mine.html (or hand it to your agent), then push it back and test:
museletter templates edit mine --file mine.html
museletter templates test mine --to you@example.com
```

A template is one HTML file with `string.Template` placeholders: `$content`
(the rendered issue) and `$footer` (the unsubscribe link and postal address)
are required; `$subject` and `$header` are optional. Every create and edit is
validated - unknown placeholders, a missing `$content`/`$footer`, or a size
past Gmail's clip point are rejected - and the send path re-checks the
template as a preflight, so a broken template cannot reach subscribers.

Then pick what renders where; a campaign's own template beats its list's
default, which beats the built-in:

```bash
museletter lists edit default --template mine     # default for the whole list
museletter campaigns create ... --template mine   # or pin a single campaign
```

Two guardrails to know about: the built-in `default` can be copied but never
edited or deleted, and changing a template's HTML clears the test-send state
of every draft that renders through it - the test you approved is always the
email that goes out.

### Everything else, on the server

The confirmation email and the public pages are packaged templates too.
Overriding those (or replacing the shipped default issue template itself)
happens on the server's filesystem:

```bash
museletter preview --eject ./templates   # copies email.html, email-system.html, page.html
# edit them (colors, logo, layout), then run the server with:
export MUSELETTER_TEMPLATE_DIR=./templates
```

`museletter preview` re-renders from your ejected copies, so you can iterate on
the look without sending a single email.

## CLI reference

```
museletter serve                 run the server
museletter init                  bootstrap server config (.env + connect token)
museletter print-token           print a connect token for a configured server
museletter service <cmd>         install|restart|uninstall|status (launchd/systemd)

museletter connect <token|--url> point the CLI at a server, save a profile
museletter profiles <cmd>        list|use|rm (switch between servers)
museletter status                server version, reachability, auth, per-list counts
museletter doctor                DNS/DKIM/DMARC/provider/config health checks
museletter health                liveness of the configured server
museletter docs                  print this README (offline, agent-readable)
museletter preview               open every reader-facing surface in a browser
museletter skill install         install the agent skill into .claude/skills

museletter lists <cmd>           list|use|create|edit|show|rm
museletter subs <cmd>            add|show|list|rm|tag|untag|import|export
museletter tags <cmd>            list|create|rm
museletter campaigns <cmd>       create|show|edit|preview|test|send|stats|rm
museletter templates <cmd>       list|create|show|edit|test|rm
museletter suppressions <cmd>    list|add|rm
```

Run `museletter help` (or `museletter --help`) to see commands grouped into
client and server commands. Run any command with `--help` for its flags, or
`--json` for machine output.

## Multiple newsletters

Newsletters that send from the same address are just separate lists on one
server. Pick the one you're working on with `museletter lists use <slug>`, and
`subs`, `campaigns`, and `tags` all target it until you switch, so you're not
repeating `--list` everywhere (you can still pass `--list` for a one-off, or set
`MUSELETTER_LIST` in a script). `museletter status` shows which list is active
and how many subscribers each one has.

When newsletters send from different domains, give each its own server, since a
server has a single `from_email`. Connect to each with `museletter connect
--name <name>` and switch between them with `museletter profiles use <name>`.

## Troubleshooting

Start with `museletter doctor` (server health) and `museletter status` (can
the CLI reach and authenticate). Common cases:

- **`doctor` says the account is in the SES sandbox:** you can only email
  verified addresses until you request production access (SES console).
- **Emails land in spam / DKIM or DMARC failing:** re-check the CNAME and TXT
  records from [AWS SES setup](#aws-ses-setup-once); `doctor` reports each.
- **Bounces or complaints are not being suppressed:** the event feed is not
  wired. On SES, confirm the configuration set, the HTTPS subscription to
  `/webhooks/sns`, and that `MUSELETTER_SNS_TOPIC_ARN` matches your topic. On
  Cloudflare, confirm the queue's Email Sending event subscription exists and
  that `MUSELETTER_CLOUDFLARE_EVENTS_QUEUE_ID` is set; `doctor` checks both.
- **The subscribe form returns 429 under load:** you are behind a proxy and
  rate limiting sees the proxy IP as one client. Set `MUSELETTER_TRUST_PROXY=true`.
- **`campaigns send` refuses with 412:** do a test send first, or pass
  `--skip-test`.
- **`connect` reports "API key was rejected":** the token or key is stale;
  regenerate one on the server with `museletter print-token`.
- **The database:** everything is in one SQLite file (`MUSELETTER_DB_PATH`, or
  `/data/museletter.db` in the image). Copy it to back up; delete it to reset.

## Agent-first design

Museletter ships with a ready-made skill for Claude Code and any agent that
reads Markdown. Install it into a skills directory with:

```bash
museletter skill install            # ~/.claude/skills/museletter (all projects)
museletter skill install --project  # ./.claude/skills/museletter (this repo)
```

The skill's recipes cover publishing an issue, first-time provider setup (SES
or Cloudflare), migrating from another platform, and a periodic health check. The skill source lives at
[`src/museletter/skill/`](src/museletter/skill/).

The whole tool is built for agents: idempotency keys on mutations, dry runs,
mandatory confirm-to-send, test-send-before-send guardrails, machine-readable
JSON on every command, `doctor` for self-diagnosis, and `museletter docs` so
the full manual is available offline from the CLI itself.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest            # tests
.venv/bin/ruff check .      # lint (--fix to autofix)
.venv/bin/ruff format .     # format
.venv/bin/ty check          # typecheck
```

All four must pass before a PR. See [AGENTS.md](AGENTS.md) for architecture and
conventions.

## License

MIT
