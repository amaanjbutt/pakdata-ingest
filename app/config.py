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


settings = Settings()
