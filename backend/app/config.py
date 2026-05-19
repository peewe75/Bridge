from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="SoftiBridge API", alias="APP_NAME")
    app_env: str = Field(default="development", alias="APP_ENV")
    debug: bool = Field(default=True, alias="DEBUG")
    api_prefix: str = Field(default="/api", alias="API_PREFIX")
    cors_allow_origins: str = Field(default="*", alias="CORS_ALLOW_ORIGINS")

    database_url: str = Field(alias="DATABASE_URL")

    jwt_secret: str = Field(alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_expire_minutes: int = Field(default=60, alias="JWT_EXPIRE_MINUTES")
    clerk_enabled: bool = Field(default=False, alias="CLERK_ENABLED")
    clerk_secret_key: str = Field(default="", alias="CLERK_SECRET_KEY")
    clerk_publishable_key: str = Field(default="", alias="CLERK_PUBLISHABLE_KEY")
    clerk_issuer: str = Field(default="", alias="CLERK_ISSUER")
    clerk_jwks_url: str = Field(default="", alias="CLERK_JWKS_URL")
    clerk_audience: str = Field(default="", alias="CLERK_AUDIENCE")
    clerk_api_url: str = Field(default="https://api.clerk.com", alias="CLERK_API_URL")
    ea_hmac_secret: str = Field(default="change-me-ea", alias="EA_HMAC_SECRET")

    telegram_bot_username: str = Field(default="@softibridge", alias="TELEGRAM_BOT_USERNAME")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_mode: str = Field(default="webhook", alias="TELEGRAM_MODE")
    telegram_webhook_url: str = Field(default="", alias="TELEGRAM_WEBHOOK_URL")
    telegram_webhook_secret: str = Field(default="", alias="TELEGRAM_WEBHOOK_SECRET")
    telegram_admin_super_chat_id: str = Field(default="", alias="TELEGRAM_ADMIN_SUPER_CHAT_ID")
    telegram_admin_alerts_chat_id: str = Field(default="", alias="TELEGRAM_ADMIN_ALERTS_CHAT_ID")
    telegram_support_chat_id: str = Field(default="", alias="TELEGRAM_SUPPORT_CHAT_ID")
    # BYO bot — base URL pubblico per setWebhook dei bot client (es. https://api.softibridge.com)
    telegram_webhook_base_url: str = Field(default="", alias="TELEGRAM_WEBHOOK_BASE_URL")
    # Fernet 32-byte url-safe base64 key per cifrare i token bot dei client a riposo
    telegram_token_enc_key: str = Field(default="", alias="TELEGRAM_TOKEN_ENC_KEY")

    stripe_secret_key: str = Field(default="", alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")
    stripe_billing_portal_return_url: str = Field(default="", alias="STRIPE_BILLING_PORTAL_RETURN_URL")
    stripe_publishable_key: str = Field(default="", alias="STRIPE_PUBLISHABLE_KEY")
    stripe_success_url: str = Field(default="", alias="STRIPE_SUCCESS_URL")
    stripe_cancel_url: str = Field(default="", alias="STRIPE_CANCEL_URL")

    default_currency: str = Field(default="EUR", alias="DEFAULT_CURRENCY")
    invoice_issuer_name: str = Field(default="SoftiBridge", alias="INVOICE_ISSUER_NAME")
    invoice_issuer_country: str = Field(default="IT", alias="INVOICE_ISSUER_COUNTRY")
    invoice_issuer_vat_id: str = Field(default="", alias="INVOICE_ISSUER_VAT_ID")
    invoice_output_dir: str = Field(default="./generated_invoices", alias="INVOICE_OUTPUT_DIR")
    downloads_dir: str = Field(default="./downloads", alias="DOWNLOADS_DIR")
    softibridge_file_bridge_base: str = Field(default="", alias="SOFTIBRIDGE_FILE_BRIDGE_BASE")
    # Multi-tenant: in prod il file-bridge è scritto dal sidecar sul VPS client, non dal backend.
    # Default false (DB-only). Mettere true solo per dev locale single-tenant.
    softibridge_bridge_file_legacy: bool = Field(default=False, alias="SOFTIBRIDGE_BRIDGE_FILE_LEGACY")

    billing_invoice_series: str = Field(default="A", alias="BILLING_INVOICE_SERIES")
    bank_account_name: str = Field(default="", alias="BANK_ACCOUNT_NAME")
    bank_name: str = Field(default="", alias="BANK_NAME")
    bank_iban: str = Field(default="", alias="BANK_IBAN")
    bank_bic_swift: str = Field(default="", alias="BANK_BIC_SWIFT")
    bank_payment_reason_template: str = Field(default="SOFTIBRIDGE {invoice_number}", alias="BANK_PAYMENT_REASON_TEMPLATE")
    usdt_tron_wallet_address: str = Field(default="", alias="USDT_TRON_WALLET_ADDRESS")
    usdt_tron_network_label: str = Field(default="TRC20", alias="USDT_TRON_NETWORK_LABEL")
    usdt_price_buffer_pct: float = Field(default=1.0, alias="USDT_PRICE_BUFFER_PCT")
    manual_payment_proofs_dir: str = Field(default="./manual_payment_proofs", alias="MANUAL_PAYMENT_PROOFS_DIR")

    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from_email: str = Field(default="", alias="SMTP_FROM_EMAIL")
    smtp_from_name: str = Field(default="SoftiBridge", alias="SMTP_FROM_NAME")
    smtp_use_tls: bool = Field(default=True, alias="SMTP_USE_TLS")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[arg-type]
