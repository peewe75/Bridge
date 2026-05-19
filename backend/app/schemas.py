from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field


class HealthResponse(BaseModel):
    status: str
    app: str
    env: str


class PlanOut(BaseModel):
    code: str
    display_name: str
    billing_mode: str
    monthly_price_cents: int | None
    setup_price_cents: int | None
    currency: str
    slot_limit_total: int
    feature_flags: dict[str, Any] = Field(default_factory=dict)


class CheckoutRequest(BaseModel):
    plan_code: Literal["BASIC", "PRO", "ENTERPRISE"]
    email: EmailStr
    full_name: str | None = None
    referral_code: str | None = None
    country_code: str | None = None
    language: str = "it"
    fiscal_profile: dict[str, Any] = Field(default_factory=dict)


class CheckoutResponse(BaseModel):
    checkout_url: str
    mode: str
    provider: str = "stripe"
    simulated: bool = False


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: Literal["SUPER_ADMIN", "ADMIN_WL", "AFFILIATE", "CLIENT"] = "CLIENT"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserMe(BaseModel):
    id: str
    email: EmailStr
    role: str
    status: str
    created_at: datetime | None = None


class TaxEvaluationRequest(BaseModel):
    customer_country: str
    issuer_country: str = "IT"
    is_business: bool = False
    customer_vat_id: str | None = None
    is_vat_exempt_declared: bool = False
    service_type: str = "DIGITAL_SOFTWARE"
    amount_cents: int
    currency: str = "EUR"


class TaxEvaluationResponse(BaseModel):
    treatment: str
    vat_rate: float
    vat_amount_cents: int
    net_amount_cents: int
    gross_amount_cents: int
    note: str
    legal_basis: str | None = None


class InvoicePreviewRequest(BaseModel):
    customer_name: str
    customer_email: EmailStr
    customer_country: str
    amount_cents: int
    currency: str = "EUR"
    is_business: bool = False
    customer_vat_id: str | None = None
    is_vat_exempt_declared: bool = False
    description: str = "SoftiBridge Subscription"


class ClientCreateRequest(BaseModel):
    full_name: str
    email: EmailStr | None = None
    telegram_username: str | None = None
    phone: str | None = None
    admin_wl_id: str | None = None
    country_code: str | None = None
    fiscal_profile: dict[str, Any] = Field(default_factory=dict)


class ClientOut(BaseModel):
    id: str
    admin_wl_id: str | None = None
    full_name: str
    email: EmailStr | None = None
    telegram_username: str | None = None
    telegram_chat_id: str | None = None
    phone: str | None = None
    country_code: str | None = None
    status: str


class LicenseCreateRequest(BaseModel):
    client_id: str | None = None
    plan_code: Literal["BASIC", "PRO", "ENTERPRISE"]
    days: int = Field(default=30, ge=1, le=3650)


class LicenseOut(BaseModel):
    id: str
    client_id: str | None
    plan_code: str | None
    status: str
    expiry_at: datetime | None = None
    install_id: str | None = None
    mt_accounts: dict[str, Any] = Field(default_factory=dict)
    grace_until: datetime | None = None
    replaced_from_license_id: str | None = None
    replaced_by_license_id: str | None = None
    replacement_reason: str | None = None


class AdminSummary(BaseModel):
    clients_total: int
    licenses_total: int
    licenses_active: int
    mrr_cents: int
    invoices_total_cents: int


class AuditLogOut(BaseModel):
    id: str
    actor_type: str
    actor_id: str | None = None
    action: str
    entity_type: str
    entity_id: str | None = None
    level: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class ClientDashboardResponse(BaseModel):
    client: ClientOut
    license: LicenseOut | None = None
    invoices: list[dict[str, Any]] = Field(default_factory=list)


class ClientEaConfigRequest(BaseModel):
    mt4_account: str | None = None
    mt5_account: str | None = None
    default_lots: float = Field(default=0.1, ge=0.01, le=100)
    max_daily_dd_pct: float = Field(default=5.0, ge=0.1, le=100)


class ClientEaConfigResponse(BaseModel):
    mt4_account: str | None = None
    mt5_account: str | None = None
    default_lots: float = 0.1
    max_daily_dd_pct: float = 5.0
    source: str = "default"


class LicenseUpgradeRequest(BaseModel):
    plan_code: Literal["BASIC", "PRO", "ENTERPRISE"]


class EaValidateRequest(BaseModel):
    license_id: str
    install_id: str
    account_number: str
    platform: Literal["MT4", "MT5"]
    timestamp: int
    signature: str


class EaHeartbeatRequest(BaseModel):
    license_id: str
    install_id: str
    account_number: str
    platform: Literal["MT4", "MT5"]
    timestamp: int
    signature: str


class EaValidateResponse(BaseModel):
    valid: bool
    reason: str | None = None
    license_status: str | None = None
    expiry_at: datetime | None = None


class EaCommandPullRequest(BaseModel):
    license_id: str
    install_id: str
    account_number: str
    platform: Literal["MT4", "MT5"]
    timestamp: int
    signature: str
    since_id: int = 0
    limit: int = Field(default=50, ge=1, le=500)


class EaCommandItem(BaseModel):
    id: int
    cmd_uid: str
    cmd_kind: str
    platform: str
    payload_kv: str
    created_at: datetime | None = None


class EaCommandPullResponse(BaseModel):
    license_status: str | None = None
    commands: list[EaCommandItem] = Field(default_factory=list)
    server_ts: int


class EaCommandAckRequest(BaseModel):
    license_id: str
    install_id: str
    account_number: str
    platform: Literal["MT4", "MT5"]
    timestamp: int
    signature: str
    cmd_id: int | None = None
    cmd_uid: str | None = None
    status: Literal["OK", "ERROR"] = "OK"
    msg: str | None = None
    ticket: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class DownloadOut(BaseModel):
    id: str
    code: str
    file_name: str
    version: str
    platform: str | None = None
    active: bool


class BillingPortalResponse(BaseModel):
    url: str
    simulated: bool = False


class SignalRoomCreateRequest(BaseModel):
    name: str
    source_type: str = "TELEGRAM"
    source_chat_id: str | None = None
    symbol_defaults: dict[str, Any] = Field(default_factory=dict)
    parser_policy: dict[str, Any] = Field(default_factory=dict)


class SignalRoomOut(BaseModel):
    id: str
    name: str
    source_type: str
    source_chat_id: str | None = None
    symbol_defaults: dict[str, Any] = Field(default_factory=dict)
    parser_policy: dict[str, Any] = Field(default_factory=dict)
    active: bool


class SignalFormatCreateRequest(BaseModel):
    room_id: str | None = None
    name: str
    parser_kind: str = "REGEX_TEMPLATE"
    mode_hint: str | None = "AUTO"
    regex_pattern: str | None = None
    field_map: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    enabled: bool = True


class SignalFormatOut(BaseModel):
    id: str
    room_id: str | None = None
    name: str
    parser_kind: str
    mode_hint: str | None = None
    regex_pattern: str | None = None
    field_map: dict[str, Any] = Field(default_factory=dict)
    priority: int
    enabled: bool


class SignalParseTestRequest(BaseModel):
    text: str
    room_id: str | None = None
    source_chat_id: str | None = None
    save_log: bool = True


class SignalIngestRequest(BaseModel):
    text: str
    room_id: str | None = None
    source_chat_id: str | None = None
    auto_enqueue_threshold: int = 85
    require_valid_logic: bool = True
    write_mt4: bool = True
    write_mt5: bool = True
    # Multi-tenant: licenze target per persistere su DB.
    # Se vuoto, scrive solo file (back-compat). Telegram webhook risolve auto da room.
    license_ids: list[str] = Field(default_factory=list)


class SignalParseResult(BaseModel):
    matched: bool
    parser_used: str | None = None
    confidence: int = 0
    mode: str | None = None
    canonical: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)


class SignalParseLogOut(BaseModel):
    id: str
    room_id: str | None = None
    source_chat_id: str | None = None
    parser_used: str | None = None
    result_mode: str | None = None
    confidence: int
    valid: bool
    parsed_payload: dict[str, Any] = Field(default_factory=dict)
    errors: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
