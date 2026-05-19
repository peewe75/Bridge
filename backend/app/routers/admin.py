from __future__ import annotations

import uuid
from datetime import datetime, timezone

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_roles
from app.models import (
    AdminBillingDocument,
    AdminBranding,
    AdminManualPaymentSubmission,
    AdminOperationalLimits,
    AdminPayment,
    AdminPlan,
    AdminStatusHistory,
    AdminSubscription,
    AdminWL,
    AuditLog,
    Client,
    Download,
    Invoice,
    License,
    ManualPaymentSubmission,
    Payment,
    Plan,
    SuperAdminFeeRule,
    SuperAdminPayout,
    VpsNode,
)
from app.schemas import (
    AdminSummary,
    AuditLogOut,
    ClientCreateRequest,
    ClientOut,
    LicenseCreateRequest,
    LicenseOut,
    LicenseUpgradeRequest,
)
from app.services.invoicing import (
    create_invoice_pay_link,
    invoice_to_dict,
    issue_invoice,
    mark_invoice_paid,
    promote_proforma_to_invoice,
    send_invoice_notification,
)
from app.services.licenses import admin_summary, create_license
from app.services.licenses import apply_license_replacement, set_license_grace_window
from app.services.download_access import get_client_download_policy, normalize_download_policy, resolve_allowed_download_codes
from app.services.system_control import SYSTEM_CONTROL_STATE, snapshot as _system_snapshot

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminInvoiceIssueRequest(BaseModel):
    client_id: str
    amount_cents: int = Field(ge=1)
    currency: str = "EUR"
    description: str = "SoftiBridge service invoice"
    send_now: bool = False
    invoice_channel: str = "ADMIN_MANUAL"
    payment_method: str = "BANK_TRANSFER"
    document_type: str = "PROFORMA"  # PROFORMA or INVOICE


class ManualPaymentReviewRequest(BaseModel):
    review_notes: str | None = None
    approve_amount_cents: int | None = None


class AdminSystemControlRequest(BaseModel):
    action: str
    reason: str | None = None


class AdminWLCreateRequest(BaseModel):
    email: str
    contact_name: str
    brand_name: str
    admin_plan_code: str
    fee_pct_l1: int = Field(default=70, ge=0, le=100)
    notes: str | None = None


class AdminWLUpdateRequest(BaseModel):
    contact_name: str | None = None
    brand_name: str | None = None
    status: str | None = None
    fee_pct_l1: int | None = Field(default=None, ge=0, le=100)
    notes: str | None = None
    admin_plan_code: str | None = None


class AdminLimitsUpdateRequest(BaseModel):
    limits_json: dict = Field(default_factory=dict)
    source: str = "OVERRIDE"


class AdminBrandingUpdateRequest(BaseModel):
    brand_name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    custom_domain: str | None = None
    sender_name: str | None = None
    sender_email: str | None = None
    config_json: dict | None = None


class AdminLifecycleRequest(BaseModel):
    reason: str | None = None
    grace_days: int | None = Field(default=None, ge=1, le=60)


class LicenseReplaceRequest(BaseModel):
    plan_code: str | None = None
    days: int = Field(default=30, ge=1, le=3650)
    grace_hours: int = Field(default=48, ge=0, le=24 * 90)
    reason: str | None = None


class LicenseGraceUpdateRequest(BaseModel):
    grace_hours: int = Field(default=48, ge=0, le=24 * 90)
    reason: str | None = None


class ClientDownloadPolicyRequest(BaseModel):
    mode: str = "AUTO"  # AUTO | MANUAL
    manual_codes: list[str] = Field(default_factory=list)
    allow_extra_codes: list[str] = Field(default_factory=list)
    deny_codes: list[str] = Field(default_factory=list)


class AdminBillingIssueRequest(BaseModel):
    admin_wl_id: str
    amount_cents: int = Field(ge=1)
    currency: str = "EUR"
    description: str = "SoftiBridge Admin monthly plan"
    document_type: str = "PROFORMA"
    payment_method: str = "BANK_TRANSFER"
    invoice_channel: str = "ADMIN_MANUAL"
    subscription_id: str | None = None


class AdminBillingManualSubmitRequest(BaseModel):
    billing_document_id: str
    method: str
    submitted_amount_cents: int | None = None
    submitted_currency: str | None = None
    reference_code: str | None = None
    notes: str | None = None
    proof_url: str | None = None
    payload: dict = Field(default_factory=dict)


class FeeRulesUpdateRequest(BaseModel):
    l0: int = Field(ge=0, le=100)
    l1: int = Field(ge=0, le=100)
    l2: int = Field(ge=0, le=100)


class PayoutRunRequest(BaseModel):
    period: str | None = None  # YYYY-MM


class PayoutMarkPaidRequest(BaseModel):
    note: str | None = None


class VpsProvisionRequest(BaseModel):
    provider: str = "Custom"
    location: str = "Auto"
    ip: str | None = None
    notes: str | None = None


def _invoice_with_client(db: Session, invoice: Invoice) -> dict:
    client = db.query(Client).filter(Client.id == invoice.client_id).one_or_none() if invoice.client_id else None
    return invoice_to_dict(invoice, client=client)


def _snapshot_system_state() -> dict:
    return _system_snapshot()


def _get_or_create_fee_rules(db: Session, user_id: str | None = None) -> SuperAdminFeeRule:
    row = db.query(SuperAdminFeeRule).filter(SuperAdminFeeRule.id == "default").one_or_none()
    if row:
        return row
    row = SuperAdminFeeRule(
        id="default",
        l0_pct=20,
        l1_pct_default=70,
        l2_pct=10,
        updated_by_user_id=user_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _admin_wl_to_dict(db: Session, row: AdminWL) -> dict:
    sub = db.query(AdminSubscription).filter(AdminSubscription.admin_wl_id == row.id).order_by(AdminSubscription.created_at.desc()).first()
    limits = db.query(AdminOperationalLimits).filter(AdminOperationalLimits.admin_wl_id == row.id).one_or_none()
    branding = db.query(AdminBranding).filter(AdminBranding.admin_wl_id == row.id).one_or_none()
    return {
        "id": row.id,
        "email": row.email,
        "contact_name": row.contact_name,
        "brand_name": row.brand_name,
        "status": row.status,
        "admin_plan_code": row.admin_plan_code,
        "fee_pct_l1": row.fee_pct_l1,
        "notes": row.notes,
        "subscription": {
            "id": sub.id,
            "status": sub.status,
            "billing_cycle": sub.billing_cycle,
            "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
            "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
            "grace_until": sub.grace_until.isoformat() if sub.grace_until else None,
            "auto_renew": sub.auto_renew,
        } if sub else None,
        "limits": (limits.limits_json if limits else {}),
        "limits_source": limits.source if limits else None,
        "branding": {
            "brand_name": branding.brand_name,
            "logo_url": branding.logo_url,
            "primary_color": branding.primary_color,
            "secondary_color": branding.secondary_color,
            "custom_domain": branding.custom_domain,
            "sender_name": branding.sender_name,
            "sender_email": branding.sender_email,
            "config_json": branding.config_json or {},
        } if branding else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _create_admin_status_history(db: Session, admin_wl_id: str, from_status: str | None, to_status: str, reason: str | None, actor_user_id: str | None):
    db.add(AdminStatusHistory(
        id=str(uuid.uuid4()),
        admin_wl_id=admin_wl_id,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        actor_user_id=actor_user_id,
    ))


def _resolve_admin_wl_for_user(db: Session, user, *, required: bool = False) -> AdminWL | None:
    if getattr(user, "role", None) != "ADMIN_WL":
        return None
    row = None
    if getattr(user, "id", None):
        row = db.query(AdminWL).filter(AdminWL.user_id == user.id).order_by(AdminWL.created_at.desc()).first()
    if not row and getattr(user, "email", None):
        row = db.query(AdminWL).filter(AdminWL.email == user.email).order_by(AdminWL.created_at.desc()).first()
        # Auto-link the first matching WL profile to the authenticated ADMIN_WL user.
        if row and (not row.user_id or row.user_id == getattr(user, "id", None)):
            row.user_id = getattr(user, "id", None)
            db.commit()
            db.refresh(row)
    if required and not row:
        raise HTTPException(status_code=403, detail="Profilo Admin WL non collegato al tuo utente")
    return row


def _enforce_client_scope(client: Client | None, admin_scope: AdminWL | None) -> None:
    if not client:
        raise HTTPException(status_code=404, detail="Cliente non trovato")
    if admin_scope and client.admin_wl_id != admin_scope.id:
        raise HTTPException(status_code=403, detail="Cliente fuori dal tuo perimetro Admin")


def _max_grace_hours_for_actor(db: Session, user, admin_scope: AdminWL | None) -> int | None:
    if getattr(user, "role", None) == "SUPER_ADMIN":
        return None
    if not admin_scope:
        return 48
    limits = db.query(AdminOperationalLimits).filter(AdminOperationalLimits.admin_wl_id == admin_scope.id).one_or_none()
    limits_json = (limits.limits_json or {}) if limits else {}
    raw = limits_json.get("license_replacement_max_grace_hours", 48)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 48
    return max(0, value)


def _get_scoped_license(db: Session, license_id: str, admin_scope: AdminWL | None) -> License:
    lic = db.query(License).filter(License.id == license_id).one_or_none()
    if not lic:
        raise HTTPException(status_code=404, detail="Licenza non trovata")
    if admin_scope:
        if not lic.client_id:
            raise HTTPException(status_code=403, detail="Licenza non associata a un tuo cliente")
        client = db.query(Client).filter(Client.id == lic.client_id).one_or_none()
        _enforce_client_scope(client, admin_scope)
    return lic


def _get_scoped_invoice(db: Session, invoice_number: str, admin_scope: AdminWL | None) -> Invoice:
    invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    if admin_scope:
        if not invoice.client_id:
            raise HTTPException(status_code=403, detail="Fattura non associata a un cliente del tuo perimetro")
        client = db.query(Client).filter(Client.id == invoice.client_id).one_or_none()
        _enforce_client_scope(client, admin_scope)
    return invoice


def _scoped_client_ids(db: Session, admin_scope: AdminWL | None) -> set[str]:
    if not admin_scope:
        return set()
    rows = db.query(Client.id).filter(Client.admin_wl_id == admin_scope.id).all()
    return {r[0] for r in rows if r and r[0]}


def _admin_billing_doc_to_dict(db: Session, doc: AdminBillingDocument) -> dict:
    admin_row = db.query(AdminWL).filter(AdminWL.id == doc.admin_wl_id).one_or_none()
    return {
        "id": doc.id,
        "invoice_number": doc.invoice_number,
        "document_type": doc.document_type,
        "status": doc.status,
        "payment_method": doc.payment_method,
        "invoice_channel": doc.invoice_channel,
        "amount_cents": doc.amount_cents,
        "currency": doc.currency,
        "description": doc.description,
        "issued_at": doc.issued_at.isoformat() if doc.issued_at else None,
        "paid_at": doc.paid_at.isoformat() if doc.paid_at else None,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "pdf_path": doc.pdf_path,
        "admin_wl": {
            "id": admin_row.id,
            "brand_name": admin_row.brand_name,
            "email": admin_row.email,
            "contact_name": admin_row.contact_name,
            "status": admin_row.status,
            "admin_plan_code": admin_row.admin_plan_code,
        } if admin_row else None,
        "payable": doc.status not in {"PAID", "CANCELLED"},
    }


@router.get("/dashboard/summary", response_model=AdminSummary)
def dashboard_summary(
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
) -> AdminSummary:
    if getattr(user, "role", None) == "SUPER_ADMIN":
        return AdminSummary(**admin_summary(db))

    admin_scope = _resolve_admin_wl_for_user(db, user, required=True)
    client_rows = db.query(Client).filter(Client.admin_wl_id == admin_scope.id).all()
    client_ids = [c.id for c in client_rows]
    clients_total = len(client_rows)

    licenses_q = db.query(License)
    if client_ids:
        licenses_q = licenses_q.filter(License.client_id.in_(client_ids))
        licenses = licenses_q.all()
    else:
        licenses = []
    licenses_total = len(licenses)
    licenses_active = sum(1 for l in licenses if l.status == "ACTIVE")

    plan_map = {p.code: p for p in db.query(Plan).filter(Plan.active.is_(True)).all()}
    active_licenses = [l for l in licenses if l.status in {"ACTIVE", "PAST_DUE", "GRACE_REPLACEMENT"}]
    mrr_cents = sum((plan_map.get(l.plan_code).monthly_price_cents or 0) for l in active_licenses if l.plan_code in plan_map)

    payments_q = db.query(Payment)
    if client_ids:
        payments_q = payments_q.filter(Payment.client_id.in_(client_ids))
        paid_rows = payments_q.all()
    else:
        paid_rows = []
    invoices_total_cents = sum(p.amount_cents for p in paid_rows if p.status in {"PAID", "SUCCEEDED"})

    return AdminSummary(
        clients_total=clients_total,
        licenses_total=licenses_total,
        licenses_active=licenses_active,
        mrr_cents=mrr_cents,
        invoices_total_cents=invoices_total_cents,
    )


@router.get("/system/status")
def admin_system_status(
    _user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
):
    return {"ok": True, "system": _snapshot_system_state()}


@router.post("/system/control")
def admin_system_control(
    req: AdminSystemControlRequest,
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    action = (req.action or "").strip().upper()
    if not action:
        raise HTTPException(status_code=400, detail="Azione richiesta")

    mode_before = SYSTEM_CONTROL_STATE.get("mode", "NORMAL")
    now_iso = datetime.now(timezone.utc).isoformat()

    if action == "ANALYZE":
        SYSTEM_CONTROL_STATE["last_action"] = "ANALYZE"
        SYSTEM_CONTROL_STATE["last_reason"] = req.reason or "Health analysis requested"
        SYSTEM_CONTROL_STATE["updated_at"] = now_iso
    elif action == "MAINTENANCE_ON":
        SYSTEM_CONTROL_STATE.update({
            "mode": "MAINTENANCE",
            "billing_enabled": False,
            "signals_enabled": True,
            "ea_bridge_enabled": True,
            "client_access_enabled": True,
            "last_action": action,
            "last_reason": req.reason or "Maintenance mode enabled",
            "updated_at": now_iso,
        })
    elif action == "MAINTENANCE_OFF":
        SYSTEM_CONTROL_STATE.update({
            "mode": "NORMAL",
            "billing_enabled": True,
            "signals_enabled": True,
            "ea_bridge_enabled": True,
            "client_access_enabled": True,
            "last_action": action,
            "last_reason": req.reason or "Maintenance mode disabled",
            "updated_at": now_iso,
        })
    elif action == "FREEZE_OPERATIONS":
        SYSTEM_CONTROL_STATE.update({
            "mode": "FROZEN",
            "billing_enabled": False,
            "signals_enabled": False,
            "ea_bridge_enabled": False,
            "client_access_enabled": True,
            "last_action": action,
            "last_reason": req.reason or "Operations frozen",
            "updated_at": now_iso,
        })
    elif action == "RESUME_ALL":
        SYSTEM_CONTROL_STATE.update({
            "mode": "NORMAL",
            "billing_enabled": True,
            "signals_enabled": True,
            "ea_bridge_enabled": True,
            "client_access_enabled": True,
            "last_action": action,
            "last_reason": req.reason or "All services resumed",
            "updated_at": now_iso,
        })
    elif action == "EMERGENCY_SHUTDOWN":
        SYSTEM_CONTROL_STATE.update({
            "mode": "SHUTDOWN",
            "billing_enabled": False,
            "signals_enabled": False,
            "ea_bridge_enabled": False,
            "client_access_enabled": False,
            "last_action": action,
            "last_reason": req.reason or "Emergency shutdown",
            "updated_at": now_iso,
        })
    else:
        raise HTTPException(status_code=400, detail=f"Azione non supportata: {action}")

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        action=f"SYSTEM_CONTROL_{action}",
        entity_type="SYSTEM",
        entity_id="softibridge-core",
        level="WARNING" if action in {"FREEZE_OPERATIONS", "EMERGENCY_SHUTDOWN"} else "INFO",
        details={
            "action": action,
            "mode_before": mode_before,
            "mode_after": SYSTEM_CONTROL_STATE.get("mode"),
            "reason": req.reason,
            "snapshot": _snapshot_system_state(),
        },
    ))
    db.commit()
    return {"ok": True, "system": _snapshot_system_state()}


@router.get("/clients", response_model=list[ClientOut])
def list_clients(
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
    admin_wl_id: str | None = None,
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    q = db.query(Client)
    if admin_scope:
        q = q.filter(Client.admin_wl_id == admin_scope.id)
    elif admin_wl_id:
        q = q.filter(Client.admin_wl_id == admin_wl_id)
    rows = q.order_by(Client.created_at.desc()).all()
    return [ClientOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/clients/grouped")
def list_clients_grouped_by_admin(
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    q = db.query(Client)
    if admin_scope:
        q = q.filter(Client.admin_wl_id == admin_scope.id)
    rows = q.order_by(Client.created_at.desc()).all()
    admin_ids = [r.admin_wl_id for r in rows if r.admin_wl_id]
    admins = {}
    if admin_ids:
        for a in db.query(AdminWL).filter(AdminWL.id.in_(admin_ids)).all():
            admins[a.id] = a
    grouped: dict[str, dict] = {}
    for c in rows:
        key = c.admin_wl_id or "UNASSIGNED"
        if key not in grouped:
            a = admins.get(c.admin_wl_id) if c.admin_wl_id else None
            grouped[key] = {
                "admin_wl_id": c.admin_wl_id,
                "admin_brand_name": a.brand_name if a else ("Senza Admin" if not c.admin_wl_id else "Admin sconosciuto"),
                "admin_email": a.email if a else None,
                "clients": [],
            }
        grouped[key]["clients"].append(ClientOut.model_validate(c, from_attributes=True).model_dump())
    return {"groups": list(grouped.values())}


@router.post("/clients", response_model=ClientOut)
def create_client_endpoint(
    req: ClientCreateRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    target_admin_wl_id = admin_scope.id if admin_scope else req.admin_wl_id
    if not admin_scope and req.admin_wl_id and not db.query(AdminWL).filter(AdminWL.id == req.admin_wl_id).one_or_none():
        raise HTTPException(status_code=404, detail="Admin WL assegnato non trovato")
    row = Client(
        id=str(uuid.uuid4()),
        admin_wl_id=target_admin_wl_id,
        full_name=req.full_name,
        email=req.email,
        telegram_username=req.telegram_username,
        phone=req.phone,
        country_code=(req.country_code or "").upper() or None,
        fiscal_profile=req.fiscal_profile,
        status="ACTIVE",
    )
    db.add(row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        action="CLIENT_CREATED",
        entity_type="CLIENT",
        entity_id=row.id,
        details={"email": req.email, "country_code": row.country_code},
    ))
    db.commit()
    db.refresh(row)
    return ClientOut.model_validate(row, from_attributes=True)


@router.get("/clients/{client_id}/download-policy")
def get_client_download_policy_endpoint(
    client_id: str,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    client = db.query(Client).filter(Client.id == client_id).one_or_none()
    _enforce_client_scope(client, admin_scope)
    assert client is not None
    policy = get_client_download_policy(client)
    active_downloads = db.query(Download).filter(Download.active.is_(True)).order_by(Download.code.asc()).all()
    allowed_codes = resolve_allowed_download_codes(db, client)
    return {
        "client_id": client.id,
        "policy": policy,
        "allowed_codes": sorted(allowed_codes),
        "available_codes": [str(d.code or "").upper() for d in active_downloads if d.code],
    }


@router.patch("/clients/{client_id}/download-policy")
def update_client_download_policy_endpoint(
    client_id: str,
    req: ClientDownloadPolicyRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    client = db.query(Client).filter(Client.id == client_id).one_or_none()
    _enforce_client_scope(client, admin_scope)
    assert client is not None

    policy = normalize_download_policy(req.model_dump())
    profile = client.fiscal_profile if isinstance(client.fiscal_profile, dict) else {}
    profile = dict(profile)
    profile["download_policy"] = policy
    client.fiscal_profile = profile
    db.add(client)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type=getattr(user, "role", None) or "ADMIN",
        actor_id=getattr(user, "id", None),
        action="CLIENT_DOWNLOAD_POLICY_UPDATED",
        entity_type="CLIENT",
        entity_id=client.id,
        details={"policy": policy},
    ))
    db.commit()

    allowed_codes = resolve_allowed_download_codes(db, client)
    active_downloads = db.query(Download).filter(Download.active.is_(True)).order_by(Download.code.asc()).all()
    return {
        "ok": True,
        "client_id": client.id,
        "policy": policy,
        "allowed_codes": sorted(allowed_codes),
        "available_codes": [str(d.code or "").upper() for d in active_downloads if d.code],
    }


@router.get("/payments/client")
def list_client_payments_archive(
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
    admin_wl_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    effective_admin_wl_id = admin_scope.id if admin_scope else admin_wl_id
    q = db.query(Payment).order_by(Payment.created_at.desc())
    if status:
        q = q.filter(Payment.status == status.upper())
    rows = q.limit(min(limit, 500)).all()
    client_ids = [r.client_id for r in rows if r.client_id]
    clients = {}
    if client_ids:
        for c in db.query(Client).filter(Client.id.in_(client_ids)).all():
            if effective_admin_wl_id and c.admin_wl_id != effective_admin_wl_id:
                continue
            clients[c.id] = c
    invoice_map: dict[str, Invoice] = {}
    if rows:
        pids = [r.id for r in rows]
        invs = db.query(Invoice).filter(Invoice.payment_id.in_(pids)).all() if pids else []
        invoice_map = {i.payment_id: i for i in invs if i.payment_id}
    manual_map: dict[str, ManualPaymentSubmission] = {}
    if rows:
        pids = [r.id for r in rows]
        subs = db.query(ManualPaymentSubmission).filter(ManualPaymentSubmission.payment_id.in_(pids)).all() if pids else []
        manual_map = {s.payment_id: s for s in subs if s.payment_id}

    result = []
    for p in rows:
        c = clients.get(p.client_id) if p.client_id else None
        if p.client_id and not c and effective_admin_wl_id:
            continue
        inv = invoice_map.get(p.id)
        manual = manual_map.get(p.id)
        result.append({
            "id": p.id,
            "client": {
                "id": c.id,
                "full_name": c.full_name,
                "email": c.email,
                "admin_wl_id": c.admin_wl_id,
            } if c else None,
            "invoice": {
                "id": inv.id,
                "invoice_number": inv.invoice_number,
                "status": inv.status,
                "document_type": (inv.fiscal_snapshot or {}).get("document_type") if inv else None,
            } if inv else None,
            "method": (p.metadata_json or {}).get("payment_method") or ("STRIPE" if p.stripe_payment_intent_id or p.stripe_checkout_session_id else "MANUAL"),
            "status": p.status,
            "amount_cents": p.amount_cents,
            "currency": p.currency,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "manual_submission": {
                "id": manual.id,
                "status": manual.status,
                "reference_code": manual.reference_code,
                "proof_url": manual.proof_url,
                "review_notes": manual.review_notes,
            } if manual else None,
        })
    return result


@router.get("/wl/fee-report")
def admin_wl_fee_report(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
    limit: int = 1000,
    paid_only: bool = True,
):
    fee_rules = _get_or_create_fee_rules(db)
    q = db.query(Payment).order_by(Payment.created_at.desc()).limit(min(limit, 5000))
    payments = q.all()

    paid_statuses = {"PAID", "SUCCEEDED"}
    if paid_only:
        payments = [p for p in payments if str(p.status or "").upper() in paid_statuses]

    client_ids = [p.client_id for p in payments if p.client_id]
    clients = {}
    if client_ids:
        for c in db.query(Client).filter(Client.id.in_(client_ids)).all():
            clients[c.id] = c

    admin_ids = {c.admin_wl_id for c in clients.values() if c.admin_wl_id}
    admins = {}
    if admin_ids:
        for a in db.query(AdminWL).filter(AdminWL.id.in_(admin_ids)).all():
            admins[a.id] = a

    grouped: dict[str, dict] = {}
    for p in payments:
        if not p.client_id:
            continue
        client = clients.get(p.client_id)
        if not client:
            continue
        admin_id = client.admin_wl_id or "UNASSIGNED"
        admin_row = admins.get(client.admin_wl_id) if client.admin_wl_id else None
        method = (p.metadata_json or {}).get("payment_method") or ("STRIPE" if p.stripe_payment_intent_id or p.stripe_checkout_session_id else "MANUAL")
        amount_cents = int(p.amount_cents or 0)
        if amount_cents <= 0:
            continue

        entry = grouped.get(admin_id)
        if not entry:
            fee_pct_l1 = int(admin_row.fee_pct_l1) if admin_row and admin_row.fee_pct_l1 is not None else int(fee_rules.l1_pct_default or 70)
            l2_pct = int(fee_rules.l2_pct or 10)
            l0_pct = int(fee_rules.l0_pct or 20)
            if fee_pct_l1 + l2_pct + l0_pct != 100:
                l0_pct = max(0, 100 - fee_pct_l1 - l2_pct)
            entry = {
                "admin_wl_id": None if admin_id == "UNASSIGNED" else admin_id,
                "admin_brand_name": admin_row.brand_name if admin_row else "Senza Admin",
                "admin_email": admin_row.email if admin_row else None,
                "admin_status": admin_row.status if admin_row else None,
                "admin_plan_code": admin_row.admin_plan_code if admin_row else None,
                "fee_pct_l1": fee_pct_l1,
                "fee_pct_l2": l2_pct,
                "fee_pct_l0": l0_pct,
                "payments_count": 0,
                "clients_count": 0,
                "total_amount_cents": 0,
                "l0_amount_cents": 0,
                "l1_amount_cents": 0,
                "l2_amount_cents": 0,
                "last_payment_at": None,
                "_client_ids": set(),
                "methods": {},
            }
            grouped[admin_id] = entry

        entry["payments_count"] += 1
        entry["total_amount_cents"] += amount_cents
        entry["l0_amount_cents"] += round(amount_cents * (entry["fee_pct_l0"] / 100))
        entry["l1_amount_cents"] += round(amount_cents * (entry["fee_pct_l1"] / 100))
        entry["l2_amount_cents"] += round(amount_cents * (entry["fee_pct_l2"] / 100))
        entry["_client_ids"].add(client.id)
        entry["methods"][method] = int(entry["methods"].get(method, 0)) + 1
        p_ts = p.paid_at or p.created_at
        if p_ts:
            current_last = entry.get("last_payment_at")
            if (not current_last) or (p_ts.isoformat() > current_last):
                entry["last_payment_at"] = p_ts.isoformat()

    rows = []
    for rec in grouped.values():
        rec["clients_count"] = len(rec.pop("_client_ids", set()))
        rows.append(rec)
    rows.sort(key=lambda r: r["total_amount_cents"], reverse=True)

    summary = {
        "admins_count": len(rows),
        "payments_count": sum(r["payments_count"] for r in rows),
        "clients_count": sum(r["clients_count"] for r in rows),
        "total_amount_cents": sum(r["total_amount_cents"] for r in rows),
        "l0_amount_cents": sum(r["l0_amount_cents"] for r in rows),
        "l1_amount_cents": sum(r["l1_amount_cents"] for r in rows),
        "l2_amount_cents": sum(r["l2_amount_cents"] for r in rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fee_rules": {
            "l0": int(fee_rules.l0_pct or 0),
            "l1": int(fee_rules.l1_pct_default or 0),
            "l2": int(fee_rules.l2_pct or 0),
        },
    }
    return {"summary": summary, "rows": rows}


@router.get("/wl/fee-rules")
def admin_wl_fee_rules_get(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    row = _get_or_create_fee_rules(db)
    return {
        "l0": int(row.l0_pct or 0),
        "l1": int(row.l1_pct_default or 0),
        "l2": int(row.l2_pct or 0),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/wl/fee-rules")
def admin_wl_fee_rules_save(
    req: FeeRulesUpdateRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    if req.l0 + req.l1 + req.l2 != 100:
        raise HTTPException(status_code=400, detail="La somma L0+L1+L2 deve essere 100")
    row = _get_or_create_fee_rules(db, user_id=getattr(user, "id", None))
    row.l0_pct = int(req.l0)
    row.l1_pct_default = int(req.l1)
    row.l2_pct = int(req.l2)
    row.updated_by_user_id = getattr(user, "id", None)
    db.add(row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="FEE_RULES_UPDATED",
        entity_type="FEE_RULES",
        entity_id=row.id,
        details={"l0": req.l0, "l1": req.l1, "l2": req.l2},
    ))
    db.commit()
    return {
        "ok": True,
        "l0": int(row.l0_pct or 0),
        "l1": int(row.l1_pct_default or 0),
        "l2": int(row.l2_pct or 0),
    }


@router.get("/wl/payouts")
def admin_wl_payouts_list(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
    period: str | None = None,
    status: str | None = None,
    limit: int = 500,
):
    q = db.query(SuperAdminPayout).order_by(SuperAdminPayout.updated_at.desc())
    if period:
        q = q.filter(SuperAdminPayout.period == period)
    if status and status.upper() != "ALL":
        q = q.filter(SuperAdminPayout.status == status.upper())
    rows = q.limit(min(limit, 2000)).all()
    return [{
        "id": r.id,
        "period": r.period,
        "beneficiary": r.beneficiary_name,
        "beneficiary_ref": r.beneficiary_ref,
        "beneficiary_type": r.beneficiary_type,
        "level": r.level,
        "amount_cents": int(r.amount_cents or 0),
        "currency": r.currency,
        "method": r.method,
        "status": r.status,
        "paid_at": r.paid_at.isoformat() if r.paid_at else None,
        "meta": r.meta_json or {},
    } for r in rows]


@router.post("/wl/payouts/run")
def admin_wl_payouts_run(
    req: PayoutRunRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    period = (req.period or datetime.now(timezone.utc).strftime("%Y-%m")).strip()
    report = admin_wl_fee_report(_user=user, db=db, limit=5000, paid_only=True)
    rows = report.get("rows", [])
    created_or_updated = 0
    for rec in rows:
        admin_id = rec.get("admin_wl_id") or "UNASSIGNED"
        brand = rec.get("admin_brand_name") or "Senza Admin"
        l1_amount = int(rec.get("l1_amount_cents") or 0)
        l2_amount = int(rec.get("l2_amount_cents") or 0)

        def upsert(level: str, beneficiary_type: str, beneficiary_ref: str, beneficiary_name: str, amount_cents: int, method: str):
            nonlocal created_or_updated
            if amount_cents <= 0:
                return
            row = db.query(SuperAdminPayout).filter(
                SuperAdminPayout.period == period,
                SuperAdminPayout.level == level,
                SuperAdminPayout.beneficiary_type == beneficiary_type,
                SuperAdminPayout.beneficiary_ref == beneficiary_ref,
            ).one_or_none()
            if not row:
                row = SuperAdminPayout(
                    id=str(uuid.uuid4()),
                    period=period,
                    beneficiary_type=beneficiary_type,
                    beneficiary_ref=beneficiary_ref,
                    beneficiary_name=beneficiary_name,
                    level=level,
                    amount_cents=amount_cents,
                    currency="EUR",
                    method=method,
                    status="PENDING",
                    meta_json={"source": "fee_report"},
                )
            else:
                row.amount_cents = amount_cents
                row.beneficiary_name = beneficiary_name
                row.method = method
                if row.status not in {"PAID", "ON_HOLD"}:
                    row.status = "PENDING"
            db.add(row)
            created_or_updated += 1

        upsert("L1", "ADMIN_WL", admin_id, f"{brand} ({admin_id})", l1_amount, "BANK")
        upsert("L2", "AFFILIATE_POOL", f"AFF_POOL::{admin_id}", f"Affiliates Pool ({brand})", l2_amount, "MANUAL")

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="PAYOUT_BATCH_RUN",
        entity_type="PAYOUT_BATCH",
        entity_id=period,
        details={"period": period, "rows": created_or_updated},
    ))
    db.commit()
    return {"ok": True, "period": period, "rows": created_or_updated}


@router.post("/wl/payouts/{payout_id}/mark-paid")
def admin_wl_payout_mark_paid(
    payout_id: str,
    req: PayoutMarkPaidRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    row = db.query(SuperAdminPayout).filter(SuperAdminPayout.id == payout_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Payout non trovato")
    row.status = "PAID"
    row.paid_at = datetime.now(timezone.utc)
    meta = dict(row.meta_json or {})
    if req.note:
        meta["note"] = req.note
    row.meta_json = meta
    db.add(row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="PAYOUT_MARKED_PAID",
        entity_type="PAYOUT",
        entity_id=row.id,
        details={"period": row.period, "beneficiary": row.beneficiary_name, "amount_cents": row.amount_cents, "note": req.note},
    ))
    db.commit()
    return {"ok": True, "id": row.id, "status": row.status, "paid_at": row.paid_at.isoformat() if row.paid_at else None}


def _seed_vps_nodes_if_empty(db: Session):
    if db.query(VpsNode).count() > 0:
        return
    seed = [
        ("S1-CONT-FRA", "Contabo", "173.20.12.88", "Frankfurt, DE", 18, 25, "ONLINE"),
        ("S2-AWS-LON", "AWS", "54.120.44.12", "London, UK", 34, 48, "ONLINE"),
        ("S3-HTS-NY", "Hostinger", "109.11.23.4", "New York, US", 6, 12, "ONLINE"),
        ("S4-DGO-AMS", "DigitalOcean", "188.166.50.2", "Amsterdam, NL", 0, 2, "PROVISIONING"),
    ]
    for i, provider, ip, loc, allocs, res, status in seed:
        db.add(VpsNode(
            id=i,
            provider=provider,
            ip=ip,
            location=loc,
            allocs=allocs,
            res_pct=res,
            status=status,
            notes="seed",
        ))
    db.commit()


@router.get("/vps/nodes")
def admin_vps_nodes(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    _seed_vps_nodes_if_empty(db)
    rows = db.query(VpsNode).order_by(VpsNode.created_at.asc()).all()
    return [{
        "id": r.id,
        "provider": r.provider,
        "ip": r.ip,
        "location": r.location,
        "allocs": int(r.allocs or 0),
        "res": f"{int(r.res_pct or 0)}%",
        "status": r.status,
        "notes": r.notes,
        "last_reboot_at": r.last_reboot_at.isoformat() if r.last_reboot_at else None,
    } for r in rows]


@router.post("/vps/nodes/provision")
def admin_vps_provision(
    req: VpsProvisionRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    _seed_vps_nodes_if_empty(db)
    node_id = f"S{db.query(VpsNode).count()+1}-NEW"
    row = VpsNode(
        id=node_id,
        provider=(req.provider or "Custom").strip() or "Custom",
        ip=(req.ip or "Pending..").strip() or "Pending..",
        location=(req.location or "Auto").strip() or "Auto",
        allocs=0,
        res_pct=0,
        status="PROVISIONING",
        notes=req.notes,
    )
    db.add(row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="VPS_PROVISION_REQUESTED",
        entity_type="VPS_NODE",
        entity_id=node_id,
        details={"provider": row.provider, "location": row.location},
    ))
    db.commit()
    return {"ok": True, "node": {"id": row.id, "provider": row.provider, "ip": row.ip, "location": row.location, "allocs": row.allocs, "res": f"{row.res_pct}%", "status": row.status}}


@router.post("/vps/nodes/{node_id}/reboot")
def admin_vps_reboot(
    node_id: str,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    row = db.query(VpsNode).filter(VpsNode.id == node_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="VPS node non trovato")
    row.status = "REBOOTING"
    row.last_reboot_at = datetime.now(timezone.utc)
    db.add(row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="VPS_REBOOT_REQUESTED",
        entity_type="VPS_NODE",
        entity_id=row.id,
        details={"provider": row.provider},
    ))
    db.commit()
    return {"ok": True, "id": row.id, "status": row.status, "last_reboot_at": row.last_reboot_at.isoformat() if row.last_reboot_at else None}


@router.get("/licenses", response_model=list[LicenseOut])
def list_licenses(
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    q = db.query(License)
    if admin_scope:
        q = q.join(Client, License.client_id == Client.id).filter(Client.admin_wl_id == admin_scope.id)
    rows = q.order_by(License.created_at.desc()).all()
    return [LicenseOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/licenses", response_model=LicenseOut)
def create_license_endpoint(
    req: LicenseCreateRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    if admin_scope:
        if not req.client_id:
            raise HTTPException(status_code=400, detail="Per Admin WL il client_id è obbligatorio")
        client = db.query(Client).filter(Client.id == req.client_id).one_or_none()
        _enforce_client_scope(client, admin_scope)
    lic = create_license(db, client_id=req.client_id, plan_code=req.plan_code, days=req.days)
    return LicenseOut.model_validate(lic, from_attributes=True)


@router.post("/licenses/{license_id}/replace", response_model=LicenseOut)
def replace_license_endpoint(
    license_id: str,
    req: LicenseReplaceRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    current = _get_scoped_license(db, license_id, admin_scope)
    max_hours = _max_grace_hours_for_actor(db, user, admin_scope)
    if max_hours is not None and req.grace_hours > max_hours:
        raise HTTPException(status_code=403, detail=f"Grace window massima consentita: {max_hours} ore")
    try:
        replacement = apply_license_replacement(
            db,
            source_license=current,
            plan_code=req.plan_code,
            days=req.days,
            grace_hours=req.grace_hours,
            reason=req.reason,
            actor_type=getattr(user, "role", None) or "ADMIN",
            actor_id=getattr(user, "id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LicenseOut.model_validate(replacement, from_attributes=True)


@router.patch("/licenses/{license_id}/grace", response_model=LicenseOut)
def update_license_grace_endpoint(
    license_id: str,
    req: LicenseGraceUpdateRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    license_row = _get_scoped_license(db, license_id, admin_scope)
    max_hours = _max_grace_hours_for_actor(db, user, admin_scope)
    if max_hours is not None and req.grace_hours > max_hours:
        raise HTTPException(status_code=403, detail=f"Grace window massima consentita: {max_hours} ore")
    try:
        updated = set_license_grace_window(
            db,
            license_row=license_row,
            grace_hours=req.grace_hours,
            reason=req.reason,
            actor_type=getattr(user, "role", None) or "ADMIN",
            actor_id=getattr(user, "id", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return LicenseOut.model_validate(updated, from_attributes=True)


@router.post("/licenses/{license_id}/revoke")
def revoke_license_endpoint(
    license_id: str,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    lic = _get_scoped_license(db, license_id, admin_scope)
    lic.status = "REVOKED"
    lic.updated_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        action="LICENSE_REVOKED",
        entity_type="LICENSE",
        entity_id=license_id,
        details={},
    ))
    db.commit()
    return {"ok": True, "license_id": license_id, "status": "REVOKED"}


@router.post("/licenses/{license_id}/upgrade")
def upgrade_license_endpoint(
    license_id: str,
    req: LicenseUpgradeRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    lic = _get_scoped_license(db, license_id, admin_scope)
    lic.plan_code = req.plan_code
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        action="LICENSE_UPGRADED",
        entity_type="LICENSE",
        entity_id=license_id,
        details={"new_plan": req.plan_code},
    ))
    db.commit()
    return {"ok": True, "license_id": license_id, "plan_code": req.plan_code}


@router.post("/licenses/{license_id}/remote-kill")
def remote_kill_license_endpoint(
    license_id: str,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    lic = _get_scoped_license(db, license_id, admin_scope)
    lic.status = "SUSPENDED"
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        action="REMOTE_KILL_EXECUTED",
        entity_type="LICENSE",
        entity_id=license_id,
        level="WARNING",
        details={"install_id": lic.install_id},
    ))
    db.commit()
    return {"ok": True, "license_id": license_id, "status": "SUSPENDED"}


@router.get("/kill-list/export")
def export_kill_list(
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    q = db.query(License).filter(License.status.in_(["SUSPENDED", "REVOKED"]))
    if admin_scope:
        q = q.join(Client, License.client_id == Client.id).filter(Client.admin_wl_id == admin_scope.id)
    disabled = q.all()
    return {
        "disabled_installs": [l.install_id for l in disabled if l.install_id],
        "licenses": [l.id for l in disabled],
        "count": len(disabled),
    }


@router.get("/logs", response_model=list[AuditLogOut])
def list_logs(
    _user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 500)).all()
    return [AuditLogOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/invoices")
def list_invoices(
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    q = db.query(Invoice)
    if admin_scope:
        q = q.join(Client, Invoice.client_id == Client.id).filter(Client.admin_wl_id == admin_scope.id)
    rows = q.order_by(Invoice.created_at.desc()).limit(min(limit, 500)).all()
    client_ids = [r.client_id for r in rows if r.client_id]
    clients = {}
    if client_ids:
        for c in db.query(Client).filter(Client.id.in_(client_ids)).all():
            clients[c.id] = c
    return [invoice_to_dict(r, client=clients.get(r.client_id)) for r in rows]


@router.get("/wl/self")
def get_admin_wl_self(
    user=Depends(require_roles("ADMIN_WL")),
    db: Session = Depends(get_db),
):
    row = _resolve_admin_wl_for_user(db, user, required=True)
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


@router.patch("/wl/self/branding")
def update_admin_wl_self_branding(
    req: AdminBrandingUpdateRequest,
    user=Depends(require_roles("ADMIN_WL")),
    db: Session = Depends(get_db),
):
    row = _resolve_admin_wl_for_user(db, user, required=True)
    branding = db.query(AdminBranding).filter(AdminBranding.admin_wl_id == row.id).one_or_none()
    if not branding:
        branding = AdminBranding(id=str(uuid.uuid4()), admin_wl_id=row.id, brand_name=row.brand_name, config_json={})
        db.add(branding)
    for field in ["brand_name", "logo_url", "primary_color", "secondary_color", "custom_domain", "sender_name", "sender_email"]:
        val = getattr(req, field)
        if val is not None:
            setattr(branding, field, val)
    if req.config_json is not None:
        branding.config_json = req.config_json
    if req.brand_name:
        row.brand_name = req.brand_name
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN_WL",
        actor_id=getattr(user, "id", None),
        action="ADMIN_WL_SELF_BRANDING_UPDATED",
        entity_type="ADMIN_WL",
        entity_id=row.id,
        details={"brand_name": branding.brand_name, "custom_domain": branding.custom_domain},
    ))
    db.commit()
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


# ============================
# ADMIN WL (L0 management)
# ============================


@router.get("/wl/plans")
def list_admin_wl_plans(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    rows = db.query(AdminPlan).order_by(AdminPlan.code.asc()).all()
    return [{
        "id": r.id,
        "code": r.code,
        "display_name": r.display_name,
        "monthly_price_cents": r.monthly_price_cents,
        "currency": r.currency,
        "grace_days_default": r.grace_days_default,
        "default_limits": r.default_limits or {},
        "active": r.active,
    } for r in rows]


@router.get("/wl/admins")
def list_admin_wl(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    rows = db.query(AdminWL).order_by(AdminWL.created_at.desc()).all()
    return [_admin_wl_to_dict(db, r) for r in rows]


@router.post("/wl/admins")
def create_admin_wl(
    req: AdminWLCreateRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    plan = db.query(AdminPlan).filter(AdminPlan.code == req.admin_plan_code, AdminPlan.active.is_(True)).one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Piano Admin non trovato")
    if db.query(AdminWL).filter(AdminWL.email == req.email).one_or_none():
        raise HTTPException(status_code=409, detail="Admin con questa email già esistente")

    now = datetime.now(timezone.utc)
    row = AdminWL(
        id=str(uuid.uuid4()),
        email=req.email,
        contact_name=req.contact_name,
        brand_name=req.brand_name,
        status="PENDING_PAYMENT",
        admin_plan_code=plan.code,
        fee_pct_l1=req.fee_pct_l1,
        notes=req.notes,
        parent_super_admin_user_id=getattr(user, "id", None),
    )
    db.add(row)
    sub = AdminSubscription(
        id=str(uuid.uuid4()),
        admin_wl_id=row.id,
        admin_plan_code=plan.code,
        status="PENDING_PAYMENT",
        billing_cycle="MONTHLY",
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
        auto_renew=True,
    )
    db.add(sub)
    db.add(AdminOperationalLimits(
        id=str(uuid.uuid4()),
        admin_wl_id=row.id,
        source="PLAN",
        limits_json=plan.default_limits or {},
    ))
    db.add(AdminBranding(
        id=str(uuid.uuid4()),
        admin_wl_id=row.id,
        brand_name=req.brand_name,
        sender_name=req.brand_name,
        sender_email=req.email,
        config_json={},
    ))
    _create_admin_status_history(db, row.id, None, "PENDING_PAYMENT", "Nuovo admin creato dal Super Admin", getattr(user, "id", None))
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="ADMIN_WL_CREATED",
        entity_type="ADMIN_WL",
        entity_id=row.id,
        details={"plan_code": plan.code, "brand_name": req.brand_name, "email": req.email},
    ))
    db.commit()
    db.refresh(row)
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


@router.get("/wl/admins/{admin_wl_id}")
def get_admin_wl(
    admin_wl_id: str,
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    row = db.query(AdminWL).filter(AdminWL.id == admin_wl_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    history = db.query(AdminStatusHistory).filter(AdminStatusHistory.admin_wl_id == admin_wl_id).order_by(AdminStatusHistory.created_at.desc()).limit(50).all()
    return {
        "ok": True,
        "admin_wl": _admin_wl_to_dict(db, row),
        "status_history": [{
            "id": h.id,
            "from_status": h.from_status,
            "to_status": h.to_status,
            "reason": h.reason,
            "created_at": h.created_at.isoformat() if h.created_at else None,
        } for h in history],
    }


@router.patch("/wl/admins/{admin_wl_id}")
def update_admin_wl(
    admin_wl_id: str,
    req: AdminWLUpdateRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    row = db.query(AdminWL).filter(AdminWL.id == admin_wl_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    prev_status = row.status
    if req.contact_name is not None:
        row.contact_name = req.contact_name
    if req.brand_name is not None:
        row.brand_name = req.brand_name
        branding = db.query(AdminBranding).filter(AdminBranding.admin_wl_id == row.id).one_or_none()
        if branding:
            branding.brand_name = req.brand_name
    if req.fee_pct_l1 is not None:
        row.fee_pct_l1 = req.fee_pct_l1
    if req.notes is not None:
        row.notes = req.notes
    if req.admin_plan_code is not None:
        plan = db.query(AdminPlan).filter(AdminPlan.code == req.admin_plan_code).one_or_none()
        if not plan:
            raise HTTPException(status_code=404, detail="Piano Admin non trovato")
        row.admin_plan_code = plan.code
        sub = db.query(AdminSubscription).filter(AdminSubscription.admin_wl_id == row.id).order_by(AdminSubscription.created_at.desc()).first()
        if sub:
            sub.admin_plan_code = plan.code
        limits = db.query(AdminOperationalLimits).filter(AdminOperationalLimits.admin_wl_id == row.id).one_or_none()
        if limits and limits.source == "PLAN":
            limits.limits_json = plan.default_limits or {}
    if req.status is not None:
        row.status = req.status.upper()
        if row.status != prev_status:
            _create_admin_status_history(db, row.id, prev_status, row.status, "Aggiornamento admin manuale", getattr(user, "id", None))
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="ADMIN_WL_UPDATED",
        entity_type="ADMIN_WL",
        entity_id=row.id,
        details={"status": row.status, "plan": row.admin_plan_code},
    ))
    db.commit()
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


@router.patch("/wl/admins/{admin_wl_id}/limits")
def update_admin_wl_limits(
    admin_wl_id: str,
    req: AdminLimitsUpdateRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    row = db.query(AdminWL).filter(AdminWL.id == admin_wl_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    limits = db.query(AdminOperationalLimits).filter(AdminOperationalLimits.admin_wl_id == admin_wl_id).one_or_none()
    if not limits:
        limits = AdminOperationalLimits(id=str(uuid.uuid4()), admin_wl_id=admin_wl_id, source=req.source.upper(), limits_json=req.limits_json or {})
        db.add(limits)
    else:
        limits.source = req.source.upper()
        limits.limits_json = req.limits_json or {}
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="ADMIN_WL_LIMITS_UPDATED",
        entity_type="ADMIN_WL",
        entity_id=admin_wl_id,
        details={"source": limits.source, "limits": limits.limits_json},
    ))
    db.commit()
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


@router.patch("/wl/admins/{admin_wl_id}/branding")
def update_admin_wl_branding(
    admin_wl_id: str,
    req: AdminBrandingUpdateRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    row = db.query(AdminWL).filter(AdminWL.id == admin_wl_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    branding = db.query(AdminBranding).filter(AdminBranding.admin_wl_id == admin_wl_id).one_or_none()
    if not branding:
        branding = AdminBranding(id=str(uuid.uuid4()), admin_wl_id=admin_wl_id, brand_name=row.brand_name, config_json={})
        db.add(branding)
    for field in ["brand_name", "logo_url", "primary_color", "secondary_color", "custom_domain", "sender_name", "sender_email"]:
        val = getattr(req, field)
        if val is not None:
            setattr(branding, field, val)
    if req.config_json is not None:
        branding.config_json = req.config_json
    if req.brand_name:
        row.brand_name = req.brand_name
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="ADMIN_WL_BRANDING_UPDATED",
        entity_type="ADMIN_WL",
        entity_id=admin_wl_id,
        details={"custom_domain": branding.custom_domain, "brand_name": branding.brand_name},
    ))
    db.commit()
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


@router.post("/wl/admins/{admin_wl_id}/activate")
def activate_admin_wl(admin_wl_id: str, req: AdminLifecycleRequest, user=Depends(require_roles("SUPER_ADMIN")), db: Session = Depends(get_db)):
    row = db.query(AdminWL).filter(AdminWL.id == admin_wl_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    prev = row.status
    row.status = "ACTIVE"
    sub = db.query(AdminSubscription).filter(AdminSubscription.admin_wl_id == row.id).order_by(AdminSubscription.created_at.desc()).first()
    if sub:
        sub.status = "ACTIVE"
        now = datetime.now(timezone.utc)
        sub.current_period_start = sub.current_period_start or now
        sub.current_period_end = sub.current_period_end or (now + timedelta(days=30))
        sub.grace_until = None
    _create_admin_status_history(db, row.id, prev, "ACTIVE", req.reason or "Attivazione admin", getattr(user, "id", None))
    db.commit()
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


@router.post("/wl/admins/{admin_wl_id}/suspend")
def suspend_admin_wl(admin_wl_id: str, req: AdminLifecycleRequest, user=Depends(require_roles("SUPER_ADMIN")), db: Session = Depends(get_db)):
    row = db.query(AdminWL).filter(AdminWL.id == admin_wl_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    prev = row.status
    row.status = "SUSPENDED"
    sub = db.query(AdminSubscription).filter(AdminSubscription.admin_wl_id == row.id).order_by(AdminSubscription.created_at.desc()).first()
    if sub:
        sub.status = "SUSPENDED"
    _create_admin_status_history(db, row.id, prev, "SUSPENDED", req.reason or "Sospensione admin", getattr(user, "id", None))
    db.commit()
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


@router.post("/wl/admins/{admin_wl_id}/revoke")
def revoke_admin_wl(admin_wl_id: str, req: AdminLifecycleRequest, user=Depends(require_roles("SUPER_ADMIN")), db: Session = Depends(get_db)):
    row = db.query(AdminWL).filter(AdminWL.id == admin_wl_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    prev = row.status
    row.status = "REVOKED"
    sub = db.query(AdminSubscription).filter(AdminSubscription.admin_wl_id == row.id).order_by(AdminSubscription.created_at.desc()).first()
    if sub:
        sub.status = "REVOKED"
    _create_admin_status_history(db, row.id, prev, "REVOKED", req.reason or "Revoca admin", getattr(user, "id", None))
    db.commit()
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


@router.post("/wl/admins/{admin_wl_id}/force-grace")
def force_grace_admin_wl(admin_wl_id: str, req: AdminLifecycleRequest, user=Depends(require_roles("SUPER_ADMIN")), db: Session = Depends(get_db)):
    row = db.query(AdminWL).filter(AdminWL.id == admin_wl_id).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    prev = row.status
    row.status = "GRACE_PERIOD"
    sub = db.query(AdminSubscription).filter(AdminSubscription.admin_wl_id == row.id).order_by(AdminSubscription.created_at.desc()).first()
    if sub:
        sub.status = "GRACE_PERIOD"
        grace_days = req.grace_days or 7
        sub.grace_until = datetime.now(timezone.utc) + timedelta(days=grace_days)
    _create_admin_status_history(db, row.id, prev, "GRACE_PERIOD", req.reason or "Grace period forzato", getattr(user, "id", None))
    db.commit()
    return {"ok": True, "admin_wl": _admin_wl_to_dict(db, row)}


# ============================
# BILLING ADMIN (separato)
# ============================


@router.get("/wl/billing/invoices")
def list_admin_billing_invoices(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    rows = db.query(AdminBillingDocument).order_by(AdminBillingDocument.created_at.desc()).limit(min(limit, 500)).all()
    return [_admin_billing_doc_to_dict(db, r) for r in rows]


@router.post("/wl/billing/invoices/issue")
def issue_admin_billing_invoice(
    req: AdminBillingIssueRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    admin_row = db.query(AdminWL).filter(AdminWL.id == req.admin_wl_id).one_or_none()
    if not admin_row:
        raise HTTPException(status_code=404, detail="Admin WL non trovato")
    number = f"ADM-{datetime.now(timezone.utc).year}-{uuid.uuid4().hex[:8].upper()}"
    doc = AdminBillingDocument(
        id=str(uuid.uuid4()),
        admin_wl_id=admin_row.id,
        subscription_id=req.subscription_id,
        invoice_number=number,
        document_type=req.document_type.upper(),
        status="ISSUED",
        payment_method=req.payment_method.upper(),
        invoice_channel=req.invoice_channel.upper(),
        amount_cents=req.amount_cents,
        currency=req.currency.upper(),
        description=req.description,
        fiscal_snapshot={"owner_type": "ADMIN", "brand_name": admin_row.brand_name, "email": admin_row.email},
        issued_at=datetime.now(timezone.utc),
    )
    db.add(doc)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="ADMIN_BILLING_ISSUED",
        entity_type="ADMIN_BILLING_DOCUMENT",
        entity_id=doc.id,
        details={"admin_wl_id": admin_row.id, "invoice_number": number, "amount_cents": req.amount_cents},
    ))
    db.commit()
    return {"ok": True, "invoice": _admin_billing_doc_to_dict(db, doc)}


@router.post("/wl/billing/invoices/{invoice_number}/mark-paid")
def mark_admin_billing_invoice_paid(
    invoice_number: str,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    doc = db.query(AdminBillingDocument).filter(AdminBillingDocument.invoice_number == invoice_number).one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Fattura Admin non trovata")
    doc.status = "PAID"
    doc.paid_at = datetime.now(timezone.utc)
    pay = AdminPayment(
        id=str(uuid.uuid4()),
        admin_wl_id=doc.admin_wl_id,
        billing_document_id=doc.id,
        subscription_id=doc.subscription_id,
        method=doc.payment_method,
        status="PAID",
        amount_cents=doc.amount_cents,
        currency=doc.currency,
        paid_at=doc.paid_at,
    )
    db.add(pay)
    admin_row = db.query(AdminWL).filter(AdminWL.id == doc.admin_wl_id).one_or_none()
    if admin_row:
        prev = admin_row.status
        admin_row.status = "ACTIVE"
        _create_admin_status_history(db, admin_row.id, prev, "ACTIVE", "Pagamento fattura admin confermato", getattr(user, "id", None))
    sub = db.query(AdminSubscription).filter(AdminSubscription.id == doc.subscription_id).one_or_none() if doc.subscription_id else None
    if sub:
        sub.status = "ACTIVE"
        now = datetime.now(timezone.utc)
        sub.current_period_start = now
        sub.current_period_end = now + timedelta(days=30)
        sub.grace_until = None
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="SUPER_ADMIN",
        actor_id=getattr(user, "id", None),
        action="ADMIN_BILLING_MARKED_PAID",
        entity_type="ADMIN_BILLING_DOCUMENT",
        entity_id=doc.id,
        details={"invoice_number": invoice_number},
    ))
    db.commit()
    return {"ok": True, "invoice": _admin_billing_doc_to_dict(db, doc)}


@router.get("/wl/billing/payments")
def list_admin_billing_payments(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
    limit: int = 200,
):
    rows = db.query(AdminPayment).order_by(AdminPayment.created_at.desc()).limit(min(limit, 500)).all()
    admins = {a.id: a for a in db.query(AdminWL).filter(AdminWL.id.in_([r.admin_wl_id for r in rows if r.admin_wl_id])).all()} if rows else {}
    docs = {d.id: d for d in db.query(AdminBillingDocument).filter(AdminBillingDocument.id.in_([r.billing_document_id for r in rows if r.billing_document_id])).all()} if rows else {}
    return [{
        "id": r.id,
        "admin_wl_id": r.admin_wl_id,
        "admin_brand_name": admins.get(r.admin_wl_id).brand_name if admins.get(r.admin_wl_id) else None,
        "billing_document_id": r.billing_document_id,
        "invoice_number": docs.get(r.billing_document_id).invoice_number if docs.get(r.billing_document_id) else None,
        "method": r.method,
        "status": r.status,
        "amount_cents": r.amount_cents,
        "currency": r.currency,
        "paid_at": r.paid_at.isoformat() if r.paid_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.post("/wl/billing/payments/manual/submit")
def submit_admin_manual_payment(
    req: AdminBillingManualSubmitRequest,
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    doc = db.query(AdminBillingDocument).filter(AdminBillingDocument.id == req.billing_document_id).one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Documento Admin non trovato")
    existing = db.query(AdminManualPaymentSubmission).filter(
        AdminManualPaymentSubmission.billing_document_id == doc.id,
        AdminManualPaymentSubmission.status == "PENDING",
    ).one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Esiste già una verifica manuale pendente")
    pay = AdminPayment(
        id=str(uuid.uuid4()),
        admin_wl_id=doc.admin_wl_id,
        billing_document_id=doc.id,
        subscription_id=doc.subscription_id,
        method=req.method.upper(),
        status="PENDING_VERIFICATION",
        amount_cents=req.submitted_amount_cents or doc.amount_cents,
        currency=(req.submitted_currency or doc.currency).upper(),
    )
    db.add(pay)
    db.flush()
    subm = AdminManualPaymentSubmission(
        id=str(uuid.uuid4()),
        admin_wl_id=doc.admin_wl_id,
        billing_document_id=doc.id,
        payment_id=pay.id,
        method=req.method.upper(),
        status="PENDING",
        submitted_amount_cents=req.submitted_amount_cents,
        submitted_currency=req.submitted_currency,
        reference_code=req.reference_code,
        proof_url=req.proof_url,
        notes=req.notes,
        payload=req.payload or {},
    )
    db.add(subm)
    doc.status = "PENDING_VERIFICATION"
    db.commit()
    return {"ok": True, "submission_id": subm.id}


@router.get("/wl/billing/payments/manual")
def list_admin_manual_payments(
    _user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
    status: str | None = None,
    limit: int = 100,
):
    q = db.query(AdminManualPaymentSubmission).order_by(AdminManualPaymentSubmission.submitted_at.desc())
    if status:
        q = q.filter(AdminManualPaymentSubmission.status == status.upper())
    rows = q.limit(min(limit, 500)).all()
    admins = {a.id: a for a in db.query(AdminWL).filter(AdminWL.id.in_([r.admin_wl_id for r in rows if r.admin_wl_id])).all()} if rows else {}
    docs = {d.id: d for d in db.query(AdminBillingDocument).filter(AdminBillingDocument.id.in_([r.billing_document_id for r in rows if r.billing_document_id])).all()} if rows else {}
    return [{
        "id": r.id,
        "admin_wl": {"id": r.admin_wl_id, "brand_name": admins.get(r.admin_wl_id).brand_name if admins.get(r.admin_wl_id) else None},
        "document": {"id": r.billing_document_id, "invoice_number": docs.get(r.billing_document_id).invoice_number if docs.get(r.billing_document_id) else None},
        "method": r.method,
        "status": r.status,
        "submitted_amount_cents": r.submitted_amount_cents,
        "submitted_currency": r.submitted_currency,
        "reference_code": r.reference_code,
        "proof_url": r.proof_url,
        "notes": r.notes,
        "payload": r.payload or {},
        "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
    } for r in rows]


@router.post("/wl/billing/payments/manual/{submission_id}/approve")
def approve_admin_manual_payment(
    submission_id: str,
    req: ManualPaymentReviewRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    subm = db.query(AdminManualPaymentSubmission).filter(AdminManualPaymentSubmission.id == submission_id).one_or_none()
    if not subm:
        raise HTTPException(status_code=404, detail="Pagamento manuale Admin non trovato")
    if subm.status != "PENDING":
        raise HTTPException(status_code=409, detail="Pagamento manuale Admin già processato")
    subm.status = "APPROVED"
    subm.review_notes = req.review_notes
    subm.reviewed_by_user_id = getattr(user, "id", None)
    subm.reviewed_at = datetime.now(timezone.utc)
    if subm.payment_id:
        pay = db.query(AdminPayment).filter(AdminPayment.id == subm.payment_id).one_or_none()
        if pay:
            pay.status = "PAID"
            if req.approve_amount_cents is not None:
                pay.amount_cents = req.approve_amount_cents
            pay.paid_at = datetime.now(timezone.utc)
    doc = db.query(AdminBillingDocument).filter(AdminBillingDocument.id == subm.billing_document_id).one_or_none()
    if doc:
        if doc.document_type == "PROFORMA":
            doc.document_type = "INVOICE"
        doc.status = "PAID"
        doc.paid_at = datetime.now(timezone.utc)
        admin_row = db.query(AdminWL).filter(AdminWL.id == doc.admin_wl_id).one_or_none()
        if admin_row:
            prev = admin_row.status
            admin_row.status = "ACTIVE"
            _create_admin_status_history(db, admin_row.id, prev, "ACTIVE", "Pagamento manuale admin approvato", getattr(user, "id", None))
    db.commit()
    return {"ok": True}


@router.post("/wl/billing/payments/manual/{submission_id}/reject")
def reject_admin_manual_payment(
    submission_id: str,
    req: ManualPaymentReviewRequest,
    user=Depends(require_roles("SUPER_ADMIN")),
    db: Session = Depends(get_db),
):
    subm = db.query(AdminManualPaymentSubmission).filter(AdminManualPaymentSubmission.id == submission_id).one_or_none()
    if not subm:
        raise HTTPException(status_code=404, detail="Pagamento manuale Admin non trovato")
    if subm.status != "PENDING":
        raise HTTPException(status_code=409, detail="Pagamento manuale Admin già processato")
    subm.status = "REJECTED"
    subm.review_notes = req.review_notes
    subm.reviewed_by_user_id = getattr(user, "id", None)
    subm.reviewed_at = datetime.now(timezone.utc)
    if subm.payment_id:
        pay = db.query(AdminPayment).filter(AdminPayment.id == subm.payment_id).one_or_none()
        if pay:
            pay.status = "REJECTED"
    doc = db.query(AdminBillingDocument).filter(AdminBillingDocument.id == subm.billing_document_id).one_or_none()
    if doc and doc.status == "PENDING_VERIFICATION":
        doc.status = "ISSUED"
    db.commit()
    return {"ok": True}


@router.post("/invoices/issue")
def issue_invoice_endpoint(
    req: AdminInvoiceIssueRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    client = db.query(Client).filter(Client.id == req.client_id).one_or_none()
    _enforce_client_scope(client, admin_scope)
    invoice = issue_invoice(
        db,
        client=client,
        amount_cents=req.amount_cents,
        currency=req.currency,
        description=req.description,
        initial_status="ISSUED",
        document_type=req.document_type,
        invoice_channel=req.invoice_channel,
        payment_method=req.payment_method,
    )
    send_result = None
    if req.send_now:
        send_result = send_invoice_notification(db, invoice=invoice, client=client, actor_type="ADMIN", actor_id=getattr(user, "id", None))
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        actor_id=getattr(user, "id", None),
        action="ADMIN_INVOICE_ISSUED",
        entity_type="INVOICE",
        entity_id=invoice.id,
        details={"client_id": client.id, "invoice_number": invoice.invoice_number, "send_now": req.send_now},
    ))
    db.commit()
    return {"ok": True, "invoice": _invoice_with_client(db, invoice), "send_result": send_result}


@router.post("/invoices/{invoice_number}/send")
def send_invoice_endpoint(
    invoice_number: str,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    invoice = _get_scoped_invoice(db, invoice_number, admin_scope)
    if not invoice.client_id:
        raise HTTPException(status_code=400, detail="Fattura senza cliente associato")
    client = db.query(Client).filter(Client.id == invoice.client_id).one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Cliente fattura non trovato")
    send_result = send_invoice_notification(db, invoice=invoice, client=client, actor_type="ADMIN", actor_id=getattr(user, "id", None))
    db.commit()
    return {"ok": True, "invoice": _invoice_with_client(db, invoice), "send_result": send_result}


@router.post("/invoices/{invoice_number}/mark-paid")
def mark_invoice_paid_endpoint(
    invoice_number: str,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    invoice = _get_scoped_invoice(db, invoice_number, admin_scope)
    if (invoice.status or "").upper() != "PAID":
        mark_invoice_paid(db, invoice=invoice, actor_type="ADMIN", actor_id=getattr(user, "id", None))
    db.commit()
    return {"ok": True, "invoice": _invoice_with_client(db, invoice)}


@router.post("/invoices/{invoice_number}/payment-link")
def invoice_payment_link_endpoint(
    invoice_number: str,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    invoice = _get_scoped_invoice(db, invoice_number, admin_scope)
    if not invoice.client_id:
        raise HTTPException(status_code=400, detail="Fattura senza cliente associato")
    client = db.query(Client).filter(Client.id == invoice.client_id).one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Cliente non trovato")
    result, payment = create_invoice_pay_link(db, invoice=invoice, client=client)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        actor_id=getattr(user, "id", None),
        action="ADMIN_INVOICE_PAYMENT_LINK_CREATED",
        entity_type="INVOICE",
        entity_id=invoice.id,
        details={"invoice_number": invoice.invoice_number, "payment_id": payment.id, "simulated": result.simulated},
    ))
    db.commit()
    return {
        "ok": True,
        "invoice": _invoice_with_client(db, invoice),
        "checkout_url": result.url,
        "simulated": result.simulated,
        "payment_id": payment.id,
    }


@router.get("/payments/manual")
def list_manual_payments(
    status: str | None = None,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    q = db.query(ManualPaymentSubmission).order_by(ManualPaymentSubmission.submitted_at.desc())
    if status:
        q = q.filter(ManualPaymentSubmission.status == status.upper())
    rows = q.limit(min(limit, 500)).all()
    invoice_ids = [r.invoice_id for r in rows]
    client_ids = [r.client_id for r in rows if r.client_id]
    invoices = {i.id: i for i in db.query(Invoice).filter(Invoice.id.in_(invoice_ids)).all()} if invoice_ids else {}
    clients = {c.id: c for c in db.query(Client).filter(Client.id.in_(client_ids)).all()} if client_ids else {}
    result = [
        {
            "id": r.id,
            "method": r.method,
            "status": r.status,
            "reference_code": r.reference_code,
            "submitted_amount_cents": r.submitted_amount_cents,
            "submitted_currency": r.submitted_currency,
            "proof_url": r.proof_url,
            "notes": r.notes,
            "payload": r.payload,
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "review_notes": r.review_notes,
            "invoice": _invoice_with_client(db, invoices[r.invoice_id]) if r.invoice_id in invoices else None,
            "client": {
                "id": clients[r.client_id].id,
                "full_name": clients[r.client_id].full_name,
                "email": clients[r.client_id].email,
            } if r.client_id and r.client_id in clients else None,
        }
        for r in rows
    ]
    if admin_scope:
        filtered = []
        for item in result:
            client = item.get("client")
            inv = item.get("invoice") or {}
            client_admin_id = ((inv.get("client") or {}).get("admin_wl_id")) if inv else None
            if client and client.get("id"):
                c = clients.get(client["id"])
                if not c or c.admin_wl_id != admin_scope.id:
                    continue
            elif client_admin_id and client_admin_id != admin_scope.id:
                continue
            filtered.append(item)
        return filtered
    return result


@router.post("/payments/manual/{submission_id}/approve")
def approve_manual_payment(
    submission_id: str,
    req: ManualPaymentReviewRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    sub = db.query(ManualPaymentSubmission).filter(ManualPaymentSubmission.id == submission_id).one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Manual payment submission non trovato")
    if sub.status == "APPROVED":
        invoice = db.query(Invoice).filter(Invoice.id == sub.invoice_id).one_or_none()
        return {"ok": True, "already_approved": True, "invoice": _invoice_with_client(db, invoice) if invoice else None}
    invoice = db.query(Invoice).filter(Invoice.id == sub.invoice_id).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura associata non trovata")
    if admin_scope:
        if not invoice.client_id:
            raise HTTPException(status_code=403, detail="Fattura fuori dal tuo perimetro Admin")
        client = db.query(Client).filter(Client.id == invoice.client_id).one_or_none()
        _enforce_client_scope(client, admin_scope)
    payment = db.query(Payment).filter(Payment.id == sub.payment_id).one_or_none() if sub.payment_id else None
    if payment:
        payment.status = "PAID"
        payment.paid_at = datetime.now(timezone.utc)
        if req.approve_amount_cents and req.approve_amount_cents > 0:
            payment.amount_cents = req.approve_amount_cents
    promote_proforma_to_invoice(db, invoice=invoice, invoice_channel="ADMIN_MANUAL", payment_method=sub.method)
    mark_invoice_paid(db, invoice=invoice, actor_type="ADMIN", actor_id=getattr(user, "id", None), payment=payment)
    sub.status = "APPROVED"
    sub.reviewed_at = datetime.now(timezone.utc)
    sub.reviewed_by_user_id = getattr(user, "id", None)
    sub.review_notes = req.review_notes
    # MVP: riattiva ultima licenza cliente se sospesa/scaduta per mancato pagamento
    if invoice.client_id:
        lic = db.query(License).filter(License.client_id == invoice.client_id).order_by(License.created_at.desc()).first()
        if lic and lic.status in {"SUSPENDED", "PAST_DUE", "EXPIRED"}:
            lic.status = "ACTIVE"
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        actor_id=getattr(user, "id", None),
        action="MANUAL_PAYMENT_APPROVED",
        entity_type="MANUAL_PAYMENT_SUBMISSION",
        entity_id=sub.id,
        details={"invoice_id": sub.invoice_id, "method": sub.method},
    ))
    db.commit()
    return {"ok": True, "submission_id": sub.id, "invoice": _invoice_with_client(db, invoice)}


@router.post("/payments/manual/{submission_id}/reject")
def reject_manual_payment(
    submission_id: str,
    req: ManualPaymentReviewRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    admin_scope = _resolve_admin_wl_for_user(db, user, required=True) if getattr(user, "role", None) == "ADMIN_WL" else None
    sub = db.query(ManualPaymentSubmission).filter(ManualPaymentSubmission.id == submission_id).one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Manual payment submission non trovato")
    invoice = db.query(Invoice).filter(Invoice.id == sub.invoice_id).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura associata non trovata")
    if admin_scope:
        if not invoice.client_id:
            raise HTTPException(status_code=403, detail="Fattura fuori dal tuo perimetro Admin")
        client = db.query(Client).filter(Client.id == invoice.client_id).one_or_none()
        _enforce_client_scope(client, admin_scope)
    sub.status = "REJECTED"
    sub.reviewed_at = datetime.now(timezone.utc)
    sub.reviewed_by_user_id = getattr(user, "id", None)
    sub.review_notes = req.review_notes
    if sub.payment_id:
        payment = db.query(Payment).filter(Payment.id == sub.payment_id).one_or_none()
        if payment and payment.status != "PAID":
            payment.status = "REJECTED"
    invoice.status = "SENT" if (invoice.status or "").upper() == "PENDING_VERIFICATION" else invoice.status
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        actor_id=getattr(user, "id", None),
        action="MANUAL_PAYMENT_REJECTED",
        entity_type="MANUAL_PAYMENT_SUBMISSION",
        entity_id=sub.id,
        details={"invoice_id": sub.invoice_id, "method": sub.method},
    ))
    db.commit()
    return {"ok": True, "submission_id": sub.id, "invoice": _invoice_with_client(db, invoice)}
