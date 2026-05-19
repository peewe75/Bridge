from __future__ import annotations

from datetime import datetime
from typing import Optional
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

JSON_COMPAT = JSON().with_variant(JSONB, "postgresql")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    clerk_user_id: Mapped[Optional[str]] = mapped_column(String, unique=True, index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Plan(Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String)
    billing_mode: Mapped[str] = mapped_column(String)
    monthly_price_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    setup_price_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String, default="EUR")
    slot_limit_total: Mapped[int] = mapped_column(Integer, default=1)
    feature_flags: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    admin_wl_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("admin_wl.id"), nullable=True, index=True)
    # Canale segnali di riferimento (FK → signal_rooms): traccia l'Admin provider dell'utente
    signal_room_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("signal_rooms.id"), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String)
    telegram_username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    telegram_chat_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    country_code: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    fiscal_profile: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    status: Mapped[str] = mapped_column(String, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("clients.id"), nullable=True)
    plan_code: Mapped[Optional[str]] = mapped_column(String, ForeignKey("plans.code"), nullable=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String)
    period_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class License(Base):
    __tablename__ = "licenses"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("clients.id"), nullable=True)
    plan_code: Mapped[Optional[str]] = mapped_column(String, ForeignKey("plans.code"), nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    expiry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    install_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    mt_accounts: Mapped[dict] = mapped_column(JSON_COMPAT, default=lambda: {"MT4": [], "MT5": []})
    grace_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_from_license_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    replaced_by_license_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    replacement_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    replaced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_code_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    activation_code_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    activation_code_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_token_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("clients.id"), nullable=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("subscriptions.id"), nullable=True)
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_checkout_session_id: Mapped[Optional[str]] = mapped_column(String, unique=True, nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String, default="EUR")
    status: Mapped[str] = mapped_column(String)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON_COMPAT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("clients.id"), nullable=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("payments.id"), nullable=True)
    stripe_invoice_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    invoice_number: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String)
    fiscal_snapshot: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    tax_result: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    total_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String, default="EUR")
    pdf_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvoiceSequence(Base):
    __tablename__ = "invoice_sequences"
    __table_args__ = (UniqueConstraint("year", "series", name="uq_invoice_sequence_year_series"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    series: Mapped[str] = mapped_column(String, default="A")
    last_number: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StripeEvent(Base):
    __tablename__ = "stripe_events"
    __table_args__ = (UniqueConstraint("stripe_event_id", name="uq_stripe_event_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    stripe_event_id: Mapped[str] = mapped_column(String, unique=True)
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON_COMPAT)
    status: Mapped[str] = mapped_column(String, default="RECEIVED")
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ManualPaymentSubmission(Base):
    __tablename__ = "manual_payment_submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    invoice_id: Mapped[str] = mapped_column(String, ForeignKey("invoices.id"), index=True)
    client_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("clients.id"), nullable=True, index=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("payments.id"), nullable=True, index=True)
    method: Mapped[str] = mapped_column(String)  # BANK_TRANSFER / USDT_TRC20
    status: Mapped[str] = mapped_column(String, default="PENDING")
    submitted_amount_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    submitted_currency: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reference_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # CRO/TRN/TXID
    proof_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    reviewed_by_user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    actor_type: Mapped[str] = mapped_column(String)
    actor_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, index=True)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    level: Mapped[str] = mapped_column(String, default="INFO")
    details: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, index=True)
    file_name: Mapped[str] = mapped_column(String)
    storage_path: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    platform: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DownloadLog(Base):
    __tablename__ = "download_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    client_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("clients.id"), nullable=True)
    download_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("downloads.id"), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EaInstallation(Base):
    __tablename__ = "ea_installations"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    license_id: Mapped[str] = mapped_column(String, ForeignKey("licenses.id"))
    install_id: Mapped[str] = mapped_column(String)
    platform: Mapped[str] = mapped_column(String)
    account_number: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="ACTIVE")
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SignalRoom(Base):
    __tablename__ = "signal_rooms"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    client_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("clients.id"), nullable=True, index=True)
    # BYO bot: opzionale; se settato, la room è alimentata da quel bot (modello B).
    # Se NULL la room è alimentata dal bot globale di piattaforma (modello C).
    telegram_bot_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("telegram_bots.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String)
    source_type: Mapped[str] = mapped_column(String, default="TELEGRAM")
    source_chat_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    symbol_defaults: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    parser_policy: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SignalFormat(Base):
    __tablename__ = "signal_formats"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("signal_rooms.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String)
    parser_kind: Mapped[str] = mapped_column(String, default="REGEX_TEMPLATE")  # REGEX_TEMPLATE / STANDARD_OVERRIDE
    mode_hint: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # PIPS / PRICE / SHORTHAND / AUTO
    regex_pattern: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    field_map: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)  # future transforms/defaults
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SignalParseLog(Base):
    __tablename__ = "signal_parse_logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("signal_rooms.id"), nullable=True, index=True)
    source_chat_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    parser_used: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    result_mode: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    parsed_payload: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    errors: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminPlan(Base):
    __tablename__ = "admin_plans"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String)
    monthly_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String, default="EUR")
    grace_days_default: Mapped[int] = mapped_column(Integer, default=7)
    default_limits: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdminWL(Base):
    __tablename__ = "admin_wl"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    parent_super_admin_user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    email: Mapped[str] = mapped_column(String, index=True)
    contact_name: Mapped[str] = mapped_column(String)
    brand_name: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="PENDING_PAYMENT", index=True)
    admin_plan_code: Mapped[Optional[str]] = mapped_column(String, ForeignKey("admin_plans.code"), nullable=True)
    fee_pct_l1: Mapped[int] = mapped_column(Integer, default=70)  # integer percentage
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdminSubscription(Base):
    __tablename__ = "admin_subscriptions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    admin_wl_id: Mapped[str] = mapped_column(String, ForeignKey("admin_wl.id"), index=True)
    admin_plan_code: Mapped[str] = mapped_column(String, ForeignKey("admin_plans.code"))
    status: Mapped[str] = mapped_column(String, default="PENDING_PAYMENT", index=True)
    billing_cycle: Mapped[str] = mapped_column(String, default="MONTHLY")
    current_period_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdminBranding(Base):
    __tablename__ = "admin_branding"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    admin_wl_id: Mapped[str] = mapped_column(String, ForeignKey("admin_wl.id"), unique=True, index=True)
    brand_name: Mapped[str] = mapped_column(String)
    logo_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    primary_color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    secondary_color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    custom_domain: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sender_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sender_email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    config_json: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdminOperationalLimits(Base):
    __tablename__ = "admin_operational_limits"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    admin_wl_id: Mapped[str] = mapped_column(String, ForeignKey("admin_wl.id"), unique=True, index=True)
    source: Mapped[str] = mapped_column(String, default="PLAN")  # PLAN | OVERRIDE
    limits_json: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdminStatusHistory(Base):
    __tablename__ = "admin_status_history"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    admin_wl_id: Mapped[str] = mapped_column(String, ForeignKey("admin_wl.id"), index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    to_status: Mapped[str] = mapped_column(String)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminBillingDocument(Base):
    __tablename__ = "admin_billing_documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    admin_wl_id: Mapped[str] = mapped_column(String, ForeignKey("admin_wl.id"), index=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("admin_subscriptions.id"), nullable=True, index=True)
    invoice_number: Mapped[str] = mapped_column(String, unique=True, index=True)
    document_type: Mapped[str] = mapped_column(String, default="PROFORMA")  # PROFORMA | INVOICE
    status: Mapped[str] = mapped_column(String, default="ISSUED", index=True)
    payment_method: Mapped[str] = mapped_column(String, default="BANK_TRANSFER")
    invoice_channel: Mapped[str] = mapped_column(String, default="ADMIN_MANUAL")
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String, default="EUR")
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fiscal_snapshot: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    pdf_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AdminPayment(Base):
    __tablename__ = "admin_payments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    admin_wl_id: Mapped[str] = mapped_column(String, ForeignKey("admin_wl.id"), index=True)
    billing_document_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("admin_billing_documents.id"), nullable=True, index=True)
    subscription_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("admin_subscriptions.id"), nullable=True, index=True)
    method: Mapped[str] = mapped_column(String, default="BANK_TRANSFER")
    status: Mapped[str] = mapped_column(String, default="PENDING")
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String, default="EUR")
    stripe_checkout_session_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    stripe_payment_intent_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON_COMPAT, default=dict)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminManualPaymentSubmission(Base):
    __tablename__ = "admin_manual_payment_submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    admin_wl_id: Mapped[str] = mapped_column(String, ForeignKey("admin_wl.id"), index=True)
    billing_document_id: Mapped[str] = mapped_column(String, ForeignKey("admin_billing_documents.id"), index=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("admin_payments.id"), nullable=True, index=True)
    method: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="PENDING")
    submitted_amount_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    submitted_currency: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reference_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    proof_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    reviewed_by_user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SuperAdminFeeRule(Base):
    __tablename__ = "super_admin_fee_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    l0_pct: Mapped[int] = mapped_column(Integer, default=20)
    l1_pct_default: Mapped[int] = mapped_column(Integer, default=70)
    l2_pct: Mapped[int] = mapped_column(Integer, default=10)
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SuperAdminPayout(Base):
    __tablename__ = "super_admin_payouts"
    __table_args__ = (
        UniqueConstraint("period", "beneficiary_type", "beneficiary_ref", "level", name="uq_super_admin_payout_identity"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    period: Mapped[str] = mapped_column(String, index=True)
    beneficiary_type: Mapped[str] = mapped_column(String)  # ADMIN_WL / AFFILIATE_POOL
    beneficiary_ref: Mapped[str] = mapped_column(String, index=True)
    beneficiary_name: Mapped[str] = mapped_column(String)
    level: Mapped[str] = mapped_column(String)  # L1 / L2
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String, default="EUR")
    method: Mapped[str] = mapped_column(String, default="BANK")
    status: Mapped[str] = mapped_column(String, default="PENDING", index=True)  # PENDING / PAID / ON_HOLD
    meta_json: Mapped[dict] = mapped_column("metadata", JSON_COMPAT, default=dict)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TelegramBot(Base):
    __tablename__ = "telegram_bots"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_type: Mapped[str] = mapped_column(String, default="CLIENT", index=True)  # PLATFORM | ADMIN_WL | CLIENT
    owner_ref_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    bot_username: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    bot_token_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted token
    webhook_secret: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    webhook_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", index=True)  # ACTIVE/SUSPENDED/REVOKED
    last_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BridgeCommand(Base):
    __tablename__ = "bridge_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    license_id: Mapped[str] = mapped_column(String, ForeignKey("licenses.id"), index=True)
    cmd_uid: Mapped[str] = mapped_column(String, unique=True, index=True)  # SIG-xxx / CTRL-xxx
    platform: Mapped[str] = mapped_column(String, default="BOTH")  # MT4 / MT5 / BOTH
    cmd_kind: Mapped[str] = mapped_column(String, default="SIGNAL")  # SIGNAL / CONTROL
    payload_kv: Mapped[str] = mapped_column(Text)  # serialized k=v;... ready for EA/sidecar
    status: Mapped[str] = mapped_column(String, default="PENDING", index=True)  # PENDING/CONSUMED/FAILED/EXPIRED
    source_chat_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    room_id: Mapped[Optional[str]] = mapped_column(String, ForeignKey("signal_rooms.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class BridgeResult(Base):
    __tablename__ = "bridge_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    license_id: Mapped[str] = mapped_column(String, ForeignKey("licenses.id"), index=True)
    cmd_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("bridge_commands.id"), nullable=True, index=True)
    cmd_uid: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String)  # OK / ERROR
    msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ticket: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    extra: Mapped[dict] = mapped_column(JSON_COMPAT, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class VpsNode(Base):
    __tablename__ = "vps_nodes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String)
    ip: Mapped[str] = mapped_column(String)
    location: Mapped[str] = mapped_column(String)
    allocs: Mapped[int] = mapped_column(Integer, default=0)
    res_pct: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="PROVISIONING", index=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_reboot_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
