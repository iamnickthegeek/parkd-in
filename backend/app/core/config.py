from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file's location: backend/app/core/ -> backend/../../../.env
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    """
    Application settings, loaded from environment variables or .env file.
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Mapbox
    MAPBOX_API_KEY: str = "placeholder_key"

    # PostgreSQL
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "parking_db"
    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432

    # Supabase (Pooled port 6543)
    SUPABASE_DATABASE_URL: str = (
        "postgresql://postgres:postgres@localhost:6543/postgres"
    )

    @property
    def DATABASE_URL(self) -> str:
        """Construct SQLAlchemy database connection string."""
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # Security
    SECRET_KEY: str = "placeholder_secret"
    ALGORITHM: str = "HS256"

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # General
    ENVIRONMENT: str = "development"
    # Set LOCAL_MODE=true to skip Supabase/R2/Redis and run fully offline.
    # Tiles are written to frontend/tiles/, DB is a local SQLite file.
    LOCAL_MODE: bool = False
    # PH6-006 — Add ingestion config vars for TfL, Camden, and London Datastore.
    TFL_API_KEY: str = ""
    CAMDEN_DATA_URL: str = "https://opendata.camden.gov.uk"
    LONDON_DATASTORE_URL: str = "https://data.london.gov.uk"
    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        """Return allowed CORS origins. Wildcard in development, parsed list in production."""
        if self.ENVIRONMENT == "development":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    # SCA-004 — Add SENTRY_DSN to the Settings schema.
    SENTRY_DSN: str | None = None
    # This code satisfies SCA-004. No additional functionality added.
    # SCA-007 — Add Upstash Redis REST credentials for Phase 3 write queue.
    UPSTASH_REDIS_REST_URL: str = "https://placeholder.upstash.io"
    UPSTASH_REDIS_REST_TOKEN: str = "placeholder_token"
    # This code satisfies SCA-007. No additional functionality added.
    # SCA-012 — Add Cloudflare R2 credentials and bucket config for Phase 4 CDN tiles.
    R2_ACCOUNT_ID: str = "placeholder_account_id"
    R2_ACCESS_KEY_ID: str = "placeholder_access_key"
    R2_SECRET_ACCESS_KEY: str = "placeholder_secret_key"
    R2_BUCKET_NAME: str = "placeholder_bucket"
    R2_PUBLIC_URL: str = "https://placeholder.r2.cloudflarestorage.com"
    # Jurisdiction-specific S3 API endpoint (e.g. WEUR). Falls back to global endpoint.
    R2_ENDPOINT_URL: str = ""
    # This code satisfies SCA-012. No additional functionality added.


settings = Settings()
