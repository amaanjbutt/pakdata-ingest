import os


class Settings:
    """Runtime configuration, read from environment."""

    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://pakdata:pakdata@localhost:5432/pakdata"
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Where raw source files are archived before parsing.
    raw_archive_dir: str = os.getenv("RAW_ARCHIVE_DIR", "./data/raw")

    # HTTP identity for scrapers (honest User-Agent per PRD §5.5).
    user_agent: str = os.getenv(
        "SCRAPER_USER_AGENT",
        "PakDataBot/1.0 (+https://pakdata.example; contact@pakdata.example)",
    )

    timezone: str = "Asia/Karachi"

    # Public base URL (for checkout redirects / links).
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "https://pakdatahub.com")

    # LemonSqueezy billing (Merchant of Record — works for PK-domiciled sellers).
    lemonsqueezy_api_key: str = os.getenv("LEMONSQUEEZY_API_KEY", "")
    lemonsqueezy_webhook_secret: str = os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET", "")
    lemonsqueezy_store_id: str = os.getenv("LEMONSQUEEZY_STORE_ID", "")
    # Product-variant id per paid plan (set after creating the products in LS).
    # Free has no variant — it is provisioned without checkout. `developer` reads
    # its own env, falling back to the legacy _BASIC name for a smooth rename.
    lemonsqueezy_variant_developer: str = os.getenv(
        "LEMONSQUEEZY_VARIANT_DEVELOPER", os.getenv("LEMONSQUEEZY_VARIANT_BASIC", "")
    )
    lemonsqueezy_variant_pro: str = os.getenv("LEMONSQUEEZY_VARIANT_PRO", "")
    lemonsqueezy_variant_business: str = os.getenv("LEMONSQUEEZY_VARIANT_BUSINESS", "")
    # Annual variants (2 months free). Empty => the plan is sold monthly only.
    lemonsqueezy_variant_developer_annual: str = os.getenv("LEMONSQUEEZY_VARIANT_DEVELOPER_ANNUAL", "")
    lemonsqueezy_variant_pro_annual: str = os.getenv("LEMONSQUEEZY_VARIANT_PRO_ANNUAL", "")
    lemonsqueezy_variant_business_annual: str = os.getenv("LEMONSQUEEZY_VARIANT_BUSINESS_ANNUAL", "")
    # Pakistan price: a LemonSqueezy discount code applied at checkout when the visitor is in
    # Pakistan (the website passes Cloudflare's country). Empty code => no Pakistan price shown.
    lemonsqueezy_discount_pk: str = os.getenv("LEMONSQUEEZY_DISCOUNT_PK", "")
    lemonsqueezy_discount_pk_percent: int = int(os.getenv("LEMONSQUEEZY_DISCOUNT_PK_PERCENT", "0") or 0)

    # Signs short-lived download links (app/api/downloads.py). Empty => a random key kept in Redis.
    download_signing_key: str = os.getenv("DOWNLOAD_SIGNING_KEY", "")

    # Whole-dataset files in Cloudflare R2 (S3 API). The nightly bulk_files job writes them; the
    # API signs short-lived links (GET /v1/bulk). Empty => bulk files are off.
    r2_endpoint: str = os.getenv("R2_ENDPOINT", "")
    r2_access_key_id: str = os.getenv("R2_ACCESS_KEY_ID", "")
    r2_secret_access_key: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    r2_bucket: str = os.getenv("R2_BULK_BUCKET", "pakdatahub-bulk")

    # Google sign-in (server-side OAuth auth-code flow). Empty => button hidden.
    google_client_id: str = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    google_redirect_uri: str = os.getenv(
        "GOOGLE_REDIRECT_URI", "https://api.pakdatahub.com/auth/google/callback"
    )

    # Transactional email (Resend). Empty => email sending is skipped (no error).
    resend_api_key: str = os.getenv("RESEND_API_KEY", "")
    email_from: str = os.getenv("EMAIL_FROM", "PakDataHub <noreply@pakdatahub.com>")

    # Admin panel access + operational notifications. Comma-separated emails whose
    # accounts may reach /v1/admin/* and who receive signup/subscription alerts.
    admin_emails: str = os.getenv("ADMIN_EMAILS", os.getenv("ALERT_EMAIL_TO", "amaanjbutt@gmail.com"))


settings = Settings()
