import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    api_key: str = ""
    db_path: str = "museletter.db"
    base_url: str = ""
    from_email: str = ""
    from_name: str = ""
    reply_to: str = ""  # optional Reply-To on all outgoing email; replies go to from_email when empty
    postal_address: str = ""
    attribution: bool = True  # the "Sent with Museletter" line in email footers; false drops it
    opt_in: str = "double"  # "double" or "single"
    send_rate: float = 10.0  # emails per second, must stay under the provider's account rate
    email_provider: str = "ses"  # "ses" or "cloudflare"
    aws_region: str = "us-east-1"
    ses_configuration_set: str = ""
    sns_topic_arn: str = ""  # if set, only SNS events from this topic are accepted
    cloudflare_account_id: str = ""
    cloudflare_events_queue_id: str = ""  # queue holding cf.email.sending.* event subscriptions
    cloudflare_poll_seconds: float = 30.0  # how often the event poller drains the queue
    trust_proxy: bool = False  # read X-Forwarded-For for the client IP (set behind a proxy)
    public_subscribe: bool = True  # expose POST /subscribe/{slug}; disable if adding via the admin API
    turnstile_secret: str = ""  # if set, /subscribe requires a valid Cloudflare Turnstile token
    confirmation_cooldown: float = 3600.0  # min seconds between confirmation emails to one address
    record_signup_context: bool = False  # store IP, user agent, origin, referrer and source per signup
    secret: str = ""  # HMAC key for public links; auto-generated into the DB if empty
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            api_key=env.get("MUSELETTER_API_KEY", ""),
            db_path=env.get("MUSELETTER_DB_PATH", "museletter.db"),
            base_url=env.get("MUSELETTER_BASE_URL", "").rstrip("/"),
            from_email=env.get("MUSELETTER_FROM_EMAIL", ""),
            from_name=env.get("MUSELETTER_FROM_NAME", ""),
            reply_to=env.get("MUSELETTER_REPLY_TO", ""),
            postal_address=env.get("MUSELETTER_POSTAL_ADDRESS", ""),
            attribution=env.get("MUSELETTER_ATTRIBUTION", "true").lower() not in ("0", "false", "no"),
            opt_in=env.get("MUSELETTER_OPT_IN", "double"),
            send_rate=float(env.get("MUSELETTER_SEND_RATE", "10")),
            email_provider=env.get("MUSELETTER_EMAIL_PROVIDER", "ses").strip().lower(),
            aws_region=env.get("AWS_REGION", env.get("AWS_DEFAULT_REGION", "us-east-1")),
            ses_configuration_set=env.get("MUSELETTER_SES_CONFIGURATION_SET", ""),
            sns_topic_arn=env.get("MUSELETTER_SNS_TOPIC_ARN", ""),
            cloudflare_account_id=env.get("CLOUDFLARE_ACCOUNT_ID", ""),
            cloudflare_events_queue_id=env.get("MUSELETTER_CLOUDFLARE_EVENTS_QUEUE_ID", ""),
            cloudflare_poll_seconds=float(env.get("MUSELETTER_CLOUDFLARE_POLL_SECONDS", "30")),
            trust_proxy=env.get("MUSELETTER_TRUST_PROXY", "").lower() in ("1", "true", "yes"),
            public_subscribe=env.get("MUSELETTER_PUBLIC_SUBSCRIBE", "true").lower()
            not in ("0", "false", "no"),
            turnstile_secret=env.get("MUSELETTER_TURNSTILE_SECRET", ""),
            confirmation_cooldown=float(env.get("MUSELETTER_CONFIRMATION_COOLDOWN", "3600")),
            record_signup_context=env.get("MUSELETTER_RECORD_SIGNUP_CONTEXT", "").lower()
            in ("1", "true", "yes"),
            secret=env.get("MUSELETTER_SECRET", ""),
        )

    def missing_required(self) -> list[str]:
        problems = []
        if not self.api_key:
            problems.append(
                "MUSELETTER_API_KEY is not set (any long random string; it is the admin credential)"
            )
        if not self.base_url:
            problems.append(
                "MUSELETTER_BASE_URL is not set (public URL of this server, e.g. https://news.example.com)"
            )
        if not self.from_email:
            problems.append("MUSELETTER_FROM_EMAIL is not set (the address newsletters are sent from)")
        if self.opt_in not in ("double", "single"):
            problems.append(f"MUSELETTER_OPT_IN must be 'double' or 'single', got '{self.opt_in}'")
        if self.email_provider not in ("ses", "cloudflare"):
            problems.append(
                f"MUSELETTER_EMAIL_PROVIDER must be 'ses' or 'cloudflare', got '{self.email_provider}'"
            )
        elif self.email_provider == "cloudflare" and not self.cloudflare_account_id:
            problems.append(
                "CLOUDFLARE_ACCOUNT_ID is not set (the Cloudflare account that owns the sending domain)"
            )
        return problems
