from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
import time

from app.models import AuditLog, Client, Download, Invoice, License, ManualPaymentSubmission, Payment, Plan, SignalRoom, Subscription, TelegramBot, User
from app.schemas import (
    BillingPortalResponse,
    ClientDashboardResponse,
    ClientEaConfigRequest,
    ClientEaConfigResponse,
    ClientOut,
    DownloadOut,
    LicenseOut,
)
from app.services.files import build_download_url
from app.services.invoicing import (
    MANUAL_METHODS,
    create_invoice_pay_link,
    ensure_invoice_payment_record,
    get_manual_payment_instructions,
    invoice_to_dict,
    mark_invoice_paid,
)
from app.services.stripe_service import create_billing_portal_session
from app.services.bridge_files import enqueue_control_command, read_recent_events, read_recent_results, read_state_snapshot
from app.services.licenses import issue_activation_code, normalize_license_status
from app.services.download_access import is_download_allowed_for_client, resolve_allowed_download_codes
from app.services.telegram_bots import (
    BotApiError,
    TokenEncryptionError,
    build_webhook_url,
    delete_webhook,
    encrypt_token,
    generate_webhook_secret,
    set_webhook,
    verify_token,
)

router = APIRouter(prefix="/client", tags=["client"])


class ClientTradeControlRequest(BaseModel):
    action: str
    ticket: int | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    move_sl_pips: int | None = None
    symbol: str | None = "CURRENT"
    write_mt4: bool = True
    write_mt5: bool = True


class ManualBankTransferSubmitRequest(BaseModel):
    reference_code: str
    amount_cents: int | None = None
    notes: str | None = None
    proof_url: str | None = None


class ManualUsdtSubmitRequest(BaseModel):
    txid: str
    amount_usdt: float | None = None
    notes: str | None = None
    proof_url: str | None = None


class LicenseActivationCodeRequest(BaseModel):
    ttl_minutes: int = 20


class TelegramBotRegisterRequest(BaseModel):
    bot_token: str


class TelegramBotOut(BaseModel):
    id: str
    bot_username: str | None = None
    status: str
    last_check_at: datetime | None = None
    last_error: str | None = None
    webhook_url: str | None = None
    created_at: datetime | None = None


def _resolve_client(db: Session, user: User) -> Client:
    client = db.query(Client).filter(Client.user_id == user.id).one_or_none()
    if not client:
        client = db.query(Client).filter(Client.email == user.email).one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Profilo cliente non trovato")
    return client


@router.get("/dashboard", response_model=ClientDashboardResponse)
def client_dashboard(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    lic = db.query(License).filter(License.client_id == client.id).order_by(License.created_at.desc()).first()
    invoices = db.query(Invoice).filter(Invoice.client_id == client.id).order_by(Invoice.created_at.desc()).limit(20).all()
    invoice_rows = [invoice_to_dict(i, client=client) for i in invoices]
    return ClientDashboardResponse(
        client=ClientOut.model_validate(client, from_attributes=True),
        license=LicenseOut.model_validate(lic, from_attributes=True) if lic else None,
        invoices=invoice_rows,
    )


@router.get("/license", response_model=LicenseOut)
def client_license(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    lic = db.query(License).filter(License.client_id == client.id).order_by(License.created_at.desc()).first()
    if not lic:
        raise HTTPException(status_code=404, detail="Licenza non trovata")
    normalize_license_status(lic)
    db.commit()
    return LicenseOut.model_validate(lic, from_attributes=True)


@router.post("/license/activation-code")
def client_license_activation_code(
    req: LicenseActivationCodeRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    lic = db.query(License).filter(License.client_id == client.id).order_by(License.created_at.desc()).first()
    if not lic:
        raise HTTPException(status_code=404, detail="Licenza non trovata")
    normalize_license_status(lic)
    if lic.status not in {"ACTIVE", "PAST_DUE", "GRACE_REPLACEMENT"}:
        raise HTTPException(status_code=400, detail=f"Licenza non attivabile via bot: {lic.status}")
    ttl_minutes = max(1, min(int(req.ttl_minutes or 20), 60 * 48))
    activation_code = issue_activation_code(lic, ttl_minutes=ttl_minutes)
    db.add(lic)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="CLIENT",
        actor_id=getattr(user, "id", None),
        action="LICENSE_ACTIVATION_CODE_ISSUED",
        entity_type="LICENSE",
        entity_id=lic.id,
        details={"ttl_minutes": ttl_minutes},
    ))
    db.commit()
    return {
        "ok": True,
        "license_id": lic.id,
        "activation_code": activation_code,
        "expires_at": lic.activation_code_expires_at.isoformat() if lic.activation_code_expires_at else None,
    }


@router.get("/ea/config", response_model=ClientEaConfigResponse)
def client_ea_config_get(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    lic = db.query(License).filter(License.client_id == client.id).order_by(License.created_at.desc()).first()

    profile = client.fiscal_profile or {}
    ea_cfg = profile.get("ea_config") if isinstance(profile, dict) else {}
    if not isinstance(ea_cfg, dict):
        ea_cfg = {}

    mt4_from_license = None
    mt5_from_license = None
    if lic and isinstance(lic.mt_accounts, dict):
        mt4_list = lic.mt_accounts.get("MT4") or []
        mt5_list = lic.mt_accounts.get("MT5") or []
        mt4_from_license = mt4_list[0] if mt4_list else None
        mt5_from_license = mt5_list[0] if mt5_list else None

    return ClientEaConfigResponse(
        mt4_account=ea_cfg.get("mt4_account") or mt4_from_license,
        mt5_account=ea_cfg.get("mt5_account") or mt5_from_license,
        default_lots=float(ea_cfg.get("default_lots", 0.1) or 0.1),
        max_daily_dd_pct=float(ea_cfg.get("max_daily_dd_pct", 5.0) or 5.0),
        source="profile" if ea_cfg else ("license" if lic else "default"),
    )


@router.post("/ea/config", response_model=ClientEaConfigResponse)
def client_ea_config_save(
    req: ClientEaConfigRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    lic = db.query(License).filter(License.client_id == client.id).order_by(License.created_at.desc()).first()

    mt4 = (req.mt4_account or "").strip() or None
    mt5 = (req.mt5_account or "").strip() or None

    profile = dict(client.fiscal_profile or {})
    profile["ea_config"] = {
        "mt4_account": mt4,
        "mt5_account": mt5,
        "default_lots": float(req.default_lots),
        "max_daily_dd_pct": float(req.max_daily_dd_pct),
    }
    client.fiscal_profile = profile

    if lic:
        mt_accounts = dict(lic.mt_accounts or {})
        mt_accounts["MT4"] = [mt4] if mt4 else []
        mt_accounts["MT5"] = [mt5] if mt5 else []
        lic.mt_accounts = mt_accounts

    db.add(client)
    if lic:
        db.add(lic)
    db.commit()

    return ClientEaConfigResponse(
        mt4_account=mt4,
        mt5_account=mt5,
        default_lots=float(req.default_lots),
        max_daily_dd_pct=float(req.max_daily_dd_pct),
        source="saved",
    )


@router.get("/invoices")
def client_invoices(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    rows = db.query(Invoice).filter(Invoice.client_id == client.id).order_by(Invoice.created_at.desc()).all()
    return [invoice_to_dict(i, client=client) for i in rows]


@router.get("/payments/manual")
def client_manual_payment_archive(
    status: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    q = (
        db.query(ManualPaymentSubmission)
        .filter(ManualPaymentSubmission.client_id == client.id)
        .order_by(ManualPaymentSubmission.submitted_at.desc())
    )
    status_u = (status or "").strip().upper()
    if status_u and status_u != "ALL":
        q = q.filter(ManualPaymentSubmission.status == status_u)
    rows = q.limit(200).all()
    invoice_ids = [r.invoice_id for r in rows if r.invoice_id]
    invoice_map: dict[str, Invoice] = {}
    if invoice_ids:
        invoices = db.query(Invoice).filter(Invoice.id.in_(invoice_ids)).all()
        invoice_map = {i.id: i for i in invoices}

    result = []
    for r in rows:
        inv = invoice_map.get(r.invoice_id)
        inv_dict = invoice_to_dict(inv, client=client) if inv else None
        result.append({
            "id": r.id,
            "method": r.method,
            "status": r.status,
            "reference_code": r.reference_code,
            "submitted_amount_cents": r.submitted_amount_cents,
            "submitted_currency": r.submitted_currency,
            "proof_url": r.proof_url,
            "notes": r.notes,
            "review_notes": r.review_notes,
            "payload": r.payload or {},
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "invoice": inv_dict,
        })
    return result


@router.get("/payments")
def client_payments_archive(
    status: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    q = (
        db.query(Payment)
        .filter(Payment.client_id == client.id)
        .order_by(Payment.created_at.desc())
    )
    status_u = (status or "").strip().upper()
    if status_u and status_u != "ALL":
        q = q.filter(Payment.status == status_u)
    rows = q.limit(200).all()
    payment_ids = [r.id for r in rows]
    invoices = db.query(Invoice).filter(Invoice.payment_id.in_(payment_ids)).all() if payment_ids else []
    invoice_map = {i.payment_id: i for i in invoices if i.payment_id}
    manuals = db.query(ManualPaymentSubmission).filter(ManualPaymentSubmission.payment_id.in_(payment_ids)).all() if payment_ids else []
    manual_map = {m.payment_id: m for m in manuals if m.payment_id}
    result = []
    for p in rows:
        inv = invoice_map.get(p.id)
        m = manual_map.get(p.id)
        result.append({
            "id": p.id,
            "status": p.status,
            "method": (p.metadata_json or {}).get("payment_method") or ("STRIPE" if p.stripe_payment_intent_id or p.stripe_checkout_session_id else "MANUAL"),
            "amount_cents": p.amount_cents,
            "currency": p.currency,
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "invoice": invoice_to_dict(inv, client=client) if inv else None,
            "manual_submission": {
                "id": m.id,
                "status": m.status,
                "reference_code": m.reference_code,
                "proof_url": m.proof_url,
                "review_notes": m.review_notes,
                "method": m.method,
            } if m else None,
        })
    return result


@router.post("/invoices/{invoice_number}/pay")
def client_invoice_pay(
    invoice_number: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number, Invoice.client_id == client.id).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    if (invoice.status or "").upper() == "PAID":
        return {"ok": True, "already_paid": True, "invoice": invoice_to_dict(invoice, client=client)}
    result, payment = create_invoice_pay_link(db, invoice=invoice, client=client)
    db.commit()
    return {
        "ok": True,
        "invoice": invoice_to_dict(invoice, client=client),
        "checkout_url": result.url,
        "simulated": result.simulated,
        "payment_id": payment.id,
    }


@router.post("/invoices/{invoice_number}/confirm-demo-payment")
def client_invoice_confirm_demo_payment(
    invoice_number: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number, Invoice.client_id == client.id).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    mark_invoice_paid(db, invoice=invoice, actor_type="CLIENT", actor_id=user.id)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="CLIENT",
        actor_id=user.id,
        action="INVOICE_DEMO_PAYMENT_CONFIRMED",
        entity_type="INVOICE",
        entity_id=invoice.id,
        details={"invoice_number": invoice.invoice_number, "timestamp": datetime.now(timezone.utc).isoformat()},
    ))
    db.commit()
    return {"ok": True, "simulated": True, "invoice": invoice_to_dict(invoice, client=client)}


@router.get("/invoices/{invoice_number}/manual-methods")
def client_invoice_manual_methods(
    invoice_number: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number, Invoice.client_id == client.id).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    return {
        "invoice": invoice_to_dict(invoice, client=client),
        "methods": [
            get_manual_payment_instructions(method="BANK_TRANSFER", invoice=invoice),
            get_manual_payment_instructions(method="USDT_TRC20", invoice=invoice),
        ],
    }


@router.post("/invoices/{invoice_number}/upload-proof")
async def client_upload_manual_payment_proof(
    invoice_number: str,
    method: str = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number, Invoice.client_id == client.id).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    method_u = method.upper().strip()
    if method_u not in MANUAL_METHODS:
        raise HTTPException(status_code=400, detail="Metodo non supportato")
    allowed_ext = {".png", ".jpg", ".jpeg", ".pdf", ".webp"}
    original = file.filename or "proof.bin"
    ext = os.path.splitext(original)[1].lower()
    if ext not in allowed_ext:
        raise HTTPException(status_code=400, detail="Formato file non consentito")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="File vuoto")
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File troppo grande (max 8MB)")
    os.makedirs(get_settings().manual_payment_proofs_dir, exist_ok=True)
    safe_inv = re.sub(r"[^A-Za-z0-9._-]", "_", invoice_number)
    safe_base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(original)[0])[:32] or "proof"
    name = f"{safe_inv}_{method_u}_{uuid.uuid4().hex[:8]}_{safe_base}{ext}"
    path = os.path.join(get_settings().manual_payment_proofs_dir, name)
    with open(path, "wb") as f:
        f.write(content)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="CLIENT",
        actor_id=user.id,
        action="MANUAL_PAYMENT_PROOF_UPLOADED",
        entity_type="INVOICE",
        entity_id=invoice.id,
        details={"invoice_number": invoice.invoice_number, "method": method_u, "proof_name": name, "size": len(content)},
    ))
    db.commit()
    return {"ok": True, "proof_url": f"/api/files/proof/{name}", "proof_name": name}


def _submit_manual_payment(
    *,
    db: Session,
    invoice: Invoice,
    client: Client,
    user: User,
    method: str,
    reference_code: str,
    submitted_amount_cents: int | None,
    submitted_currency: str,
    notes: str | None,
    proof_url: str | None,
    payload: dict,
) -> dict:
    method_u = method.upper()
    if method_u not in MANUAL_METHODS:
        raise HTTPException(status_code=400, detail="Metodo manuale non supportato")
    existing_pending = (
        db.query(ManualPaymentSubmission)
        .filter(
            ManualPaymentSubmission.invoice_id == invoice.id,
            ManualPaymentSubmission.method == method_u,
            ManualPaymentSubmission.status == "PENDING",
        )
        .order_by(ManualPaymentSubmission.submitted_at.desc())
        .first()
    )
    if existing_pending:
        raise HTTPException(status_code=409, detail="Esiste già una segnalazione pagamento in verifica per questa fattura")
    payment = ensure_invoice_payment_record(db, invoice=invoice, client=client)
    payment.status = "PENDING_VERIFICATION"
    meta = dict(payment.metadata_json or {})
    meta.update({
        "invoice_number": invoice.invoice_number,
        "billing_kind": "MANUAL_INVOICE_PAYMENT",
        "payment_method": method_u,
        "submitted_by_client_id": client.id,
    })
    payment.metadata_json = meta
    submission = ManualPaymentSubmission(
        id=str(uuid.uuid4()),
        invoice_id=invoice.id,
        client_id=client.id,
        payment_id=payment.id,
        method=method_u,
        status="PENDING",
        submitted_amount_cents=submitted_amount_cents,
        submitted_currency=submitted_currency,
        reference_code=reference_code,
        proof_url=proof_url,
        notes=notes,
        payload=payload,
    )
    db.add(submission)
    invoice.status = "PENDING_VERIFICATION"
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="CLIENT",
        actor_id=user.id,
        action="MANUAL_PAYMENT_SUBMITTED",
        entity_type="INVOICE",
        entity_id=invoice.id,
        details={"invoice_number": invoice.invoice_number, "method": method_u, "submission_id": submission.id},
    ))
    db.commit()
    return {
        "ok": True,
        "invoice": invoice_to_dict(invoice, client=client),
        "submission": {
            "id": submission.id,
            "method": submission.method,
            "status": submission.status,
            "reference_code": submission.reference_code,
            "submitted_at": submission.submitted_at.isoformat() if submission.submitted_at else None,
        },
    }


@router.post("/invoices/{invoice_number}/submit-bank-transfer")
def client_submit_bank_transfer(
    invoice_number: str,
    req: ManualBankTransferSubmitRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number, Invoice.client_id == client.id).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    return _submit_manual_payment(
        db=db,
        invoice=invoice,
        client=client,
        user=user,
        method="BANK_TRANSFER",
        reference_code=req.reference_code,
        submitted_amount_cents=req.amount_cents or invoice.total_cents,
        submitted_currency=invoice.currency,
        notes=req.notes,
        proof_url=req.proof_url,
        payload={"type": "BANK_TRANSFER", "reference_code": req.reference_code},
    )


@router.post("/invoices/{invoice_number}/submit-usdt")
def client_submit_usdt(
    invoice_number: str,
    req: ManualUsdtSubmitRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number, Invoice.client_id == client.id).one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Fattura non trovata")
    return _submit_manual_payment(
        db=db,
        invoice=invoice,
        client=client,
        user=user,
        method="USDT_TRC20",
        reference_code=req.txid,
        submitted_amount_cents=None,
        submitted_currency="USDT",
        notes=req.notes,
        proof_url=req.proof_url,
        payload={"type": "USDT_TRC20", "txid": req.txid, "amount_usdt": req.amount_usdt},
    )


@router.get("/downloads", response_model=list[DownloadOut])
def client_downloads(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Presenza cliente richiesta per proteggere file
    client = db.query(Client).filter((Client.user_id == user.id) | (Client.email == user.email)).first()
    if not client:
        raise HTTPException(status_code=404, detail="Cliente non trovato")
    rows = db.query(Download).filter(Download.active.is_(True)).order_by(Download.code.asc()).all()
    allowed_codes = resolve_allowed_download_codes(db, client)
    filtered = [r for r in rows if str(r.code or "").upper() in allowed_codes]
    return [DownloadOut.model_validate(r, from_attributes=True) for r in filtered]


@router.post("/downloads/{download_id}/token")
def client_download_token(
    download_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    row = db.query(Download).filter(Download.id == download_id, Download.active.is_(True)).one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Download non trovato")
    if not is_download_allowed_for_client(db, client, row):
        raise HTTPException(status_code=403, detail="Download non abilitato per il tuo piano")
    exp = int(time.time()) + 600
    return {"url": build_download_url(download_id, client.id, exp), "expires_at": exp}


@router.post("/billing-portal/session", response_model=BillingPortalResponse)
def client_billing_portal(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    sub = db.query(Subscription).filter(Subscription.client_id == client.id).order_by(Subscription.created_at.desc()).first()
    if not sub or not sub.stripe_customer_id:
        # fallback demo
        result = create_billing_portal_session(stripe_customer_id=f"demo_{client.id}")
    else:
        result = create_billing_portal_session(stripe_customer_id=sub.stripe_customer_id)
    return BillingPortalResponse(url=result.url, simulated=result.simulated)


@router.get("/ea/events")
def client_ea_events(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
):
    # auth presence check only; events are global in current file bridge MVP
    _ = db.query(Client).filter((Client.user_id == user.id) | (Client.email == user.email)).first()
    return {"events": read_recent_events(limit=min(limit, 200)), "results": read_recent_results(limit=min(limit, 50))}


@router.get("/trading/state")
def client_trading_state(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ = db.query(Client).filter((Client.user_id == user.id) | (Client.email == user.email)).first()
    state = read_state_snapshot()
    return {
        "positions": state.get("positions", {}),
        "pending": state.get("pending", {}),
        "summary": state.get("summary", {}),
        "events": read_recent_events(limit=50),
        "results": read_recent_results(limit=50),
    }


@router.post("/trading/control")
def client_trading_control(
    req: ClientTradeControlRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    action = (req.action or "").upper()
    allowed = {
        "CLOSE_ALL", "CLOSE_BUY", "CLOSE_SELL",
        "CLOSE_TICKET",
        "CANCEL_ALL", "CANCEL_BUY", "CANCEL_SELL", "CANCEL_TICKET",
        "SET_SLTP", "SET_SL", "SET_TP", "MOVE_SL", "MOVE_BE",
    }
    if action not in allowed:
        raise HTTPException(status_code=400, detail="Azione non supportata")
    side_filter = None
    if action.endswith("_BUY"):
        side_filter = "BUY"
    elif action.endswith("_SELL"):
        side_filter = "SELL"
    elif action.endswith("_ALL"):
        side_filter = "ALL"
    result = enqueue_control_command(
        action=action,
        symbol=req.symbol,
        side_filter=side_filter,
        ticket=req.ticket,
        sl_price=req.sl_price,
        tp_price=req.tp_price,
        move_sl_pips=req.move_sl_pips,
        comment=f"SoftiBridge-Web|CLIENT|{client.id}",
        write_mt4=req.write_mt4,
        write_mt5=req.write_mt5,
    )
    return {"ok": True, "queued": result}


@router.post("/telegram/link")
def link_telegram_chat(
    req: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Salva il Telegram ID personale dell'utente (chat_id privata con il bot)."""
    chat_id = req.get("chat_id")
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id richiesto")

    client = _resolve_client(db, user)
    client.telegram_chat_id = str(chat_id)
    db.add(client)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="CLIENT",
        actor_id=user.id,
        action="CLIENT_TELEGRAM_ID_SAVED",
        entity_type="CLIENT",
        entity_id=client.id,
        details={"telegram_chat_id": str(chat_id)},
    ))
    db.commit()
    return {"ok": True, "chat_id": chat_id, "client_id": client.id}


@router.post("/signal-room/link")
def link_signal_room(
    req: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Collega l'utente al canale segnali del suo Admin di riferimento.
    Accetta sia l'ID interno della SignalRoom che il Telegram source_chat_id del canale.
    """
    room_id = (req.get("room_id") or "").strip()
    source_chat_id = (req.get("source_chat_id") or "").strip()

    if not room_id and not source_chat_id:
        raise HTTPException(status_code=400, detail="room_id oppure source_chat_id richiesto")

    client = _resolve_client(db, user)

    # Cerca la SignalRoom per ID interno o per Telegram source_chat_id
    room: SignalRoom | None = None
    if room_id:
        room = db.query(SignalRoom).filter(SignalRoom.id == room_id, SignalRoom.active.is_(True)).first()
    if not room and source_chat_id:
        room = db.query(SignalRoom).filter(
            SignalRoom.source_chat_id == source_chat_id,
            SignalRoom.active.is_(True)
        ).first()

    if not room:
        raise HTTPException(
            status_code=404,
            detail="Canale segnali non trovato. Verifica l'ID comunicato dal tuo provider."
        )

    client.signal_room_id = room.id
    # Se la room ha un admin_wl associato, collega anche quello
    if room.owner_user_id and not client.admin_wl_id:
        # cerca admin_wl collegato all'owner_user_id della room
        from app.models import AdminWL
        admin_wl = db.query(AdminWL).filter(AdminWL.user_id == room.owner_user_id).first()
        if admin_wl:
            client.admin_wl_id = admin_wl.id

    db.add(client)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="CLIENT",
        actor_id=user.id,
        action="CLIENT_SIGNAL_ROOM_LINKED",
        entity_type="CLIENT",
        entity_id=client.id,
        details={
            "signal_room_id": room.id,
            "room_name": room.name,
            "source_chat_id": room.source_chat_id,
        },
    ))
    db.commit()
    return {
        "ok": True,
        "room_id": room.id,
        "room_name": room.name,
        "source_chat_id": room.source_chat_id,
        "client_id": client.id,
    }


@router.get("/onboarding/status")
def client_onboarding_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Ritorna lo stato del wizard onboarding dell'utente.
    Indica quali step sono completati e quali mancano.
    """
    client = _resolve_client(db, user)
    lic = db.query(License).filter(License.client_id == client.id).order_by(License.created_at.desc()).first()

    profile = client.fiscal_profile or {}
    ea_cfg = profile.get("ea_config") if isinstance(profile, dict) else {}
    if not isinstance(ea_cfg, dict):
        ea_cfg = {}

    has_mt4 = bool(
        ea_cfg.get("mt4_account") or
        (lic and lic.mt_accounts and lic.mt_accounts.get("MT4"))
    )
    has_license_active = bool(lic and lic.status in {"ACTIVE", "PAST_DUE", "GRACE_REPLACEMENT"})
    license_telegram_activated = bool(lic and lic.activation_code_hash is None and lic.activation_code_used_at)

    steps = {
        "registered": True,  # se arriva qui, è già registrato
        "telegram_id_saved": bool(client.telegram_chat_id),
        "signal_room_linked": bool(client.signal_room_id),
        "mt4_configured": has_mt4,
        "license_generated": bool(lic),
        "license_telegram_activated": license_telegram_activated,
        "license_active": has_license_active,
    }
    completed = sum(1 for v in steps.values() if v)
    total = len(steps)
    next_step = next((k for k, v in steps.items() if not v), None)

    return {
        "ok": True,
        "steps": steps,
        "completed": completed,
        "total": total,
        "progress_pct": int(completed / total * 100),
        "onboarding_complete": completed == total,
        "next_step": next_step,
        "client": {
            "id": client.id,
            "full_name": client.full_name,
            "telegram_chat_id": client.telegram_chat_id,
            "signal_room_id": client.signal_room_id,
        },
        "license": {
            "id": lic.id if lic else None,
            "status": lic.status if lic else None,
            "expiry_at": lic.expiry_at.isoformat() if lic and lic.expiry_at else None,
        } if lic else None,
    }


# ─────────────────────────────────────────────────────────────
# BYO Telegram bot — il client porta il proprio bot
# ─────────────────────────────────────────────────────────────

_PLAN_BOT_LIMIT_DEFAULT = {"BASIC": 1, "PRO": 3, "ENTERPRISE": 10}


def _bot_limit_for_client(db: Session, client: Client) -> int:
    """Limite bot BYO per piano. Override via Plan.feature_flags.telegram_bots."""
    lic = db.query(License).filter(License.client_id == client.id).order_by(License.created_at.desc()).first()
    if not lic or not lic.plan_code:
        return 1
    plan = db.query(Plan).filter(Plan.code == lic.plan_code).one_or_none()
    if plan and isinstance(plan.feature_flags, dict):
        raw = plan.feature_flags.get("telegram_bots")
        try:
            if raw is not None:
                return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return _PLAN_BOT_LIMIT_DEFAULT.get(lic.plan_code.upper(), 1)


def _bot_to_out(row: TelegramBot) -> TelegramBotOut:
    return TelegramBotOut(
        id=row.id,
        bot_username=row.bot_username,
        status=row.status,
        last_check_at=row.last_check_at,
        last_error=row.last_error,
        webhook_url=row.webhook_url,
        created_at=row.created_at,
    )


@router.get("/telegram-bot", response_model=list[TelegramBotOut])
def list_telegram_bots(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    rows = (
        db.query(TelegramBot)
        .filter(TelegramBot.owner_type == "CLIENT", TelegramBot.owner_ref_id == client.id)
        .filter(TelegramBot.status != "REVOKED")
        .order_by(TelegramBot.created_at.desc())
        .all()
    )
    return [_bot_to_out(r) for r in rows]


@router.post("/telegram-bot", response_model=TelegramBotOut)
def register_telegram_bot(
    req: TelegramBotRegisterRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)

    # Enforce plan limit
    limit = _bot_limit_for_client(db, client)
    current = (
        db.query(TelegramBot)
        .filter(TelegramBot.owner_type == "CLIENT", TelegramBot.owner_ref_id == client.id)
        .filter(TelegramBot.status == "ACTIVE")
        .count()
    )
    if current >= limit:
        raise HTTPException(status_code=403, detail=f"Limite bot del piano raggiunto ({limit})")

    token = (req.bot_token or "").strip()
    if not token or ":" not in token:
        raise HTTPException(status_code=400, detail="Token bot non valido")

    # Verify via getMe
    try:
        identity = verify_token(token)
    except BotApiError as exc:
        raise HTTPException(status_code=400, detail=f"Telegram getMe fallito: {exc}") from exc

    # Encrypt + persist
    try:
        token_enc = encrypt_token(token)
    except TokenEncryptionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    bot_id = str(uuid.uuid4())
    webhook_secret = generate_webhook_secret()
    try:
        webhook_url = build_webhook_url(bot_id)
    except BotApiError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    row = TelegramBot(
        id=bot_id,
        owner_type="CLIENT",
        owner_ref_id=client.id,
        bot_username=identity.bot_username,
        bot_token_enc=token_enc,
        webhook_secret=webhook_secret,
        webhook_url=webhook_url,
        status="ACTIVE",
        last_check_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()

    # Registra webhook su Telegram
    try:
        set_webhook(token, webhook_url, webhook_secret)
    except BotApiError as exc:
        row.status = "SUSPENDED"
        row.last_error = str(exc)[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=f"setWebhook fallito: {exc}") from exc

    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="CLIENT",
        actor_id=getattr(user, "id", None),
        action="TELEGRAM_BOT_REGISTERED",
        entity_type="TELEGRAM_BOT",
        entity_id=bot_id,
        details={"bot_username": identity.bot_username, "client_id": client.id},
    ))
    db.commit()
    db.refresh(row)
    return _bot_to_out(row)


@router.delete("/telegram-bot/{bot_id}")
def delete_telegram_bot(
    bot_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = _resolve_client(db, user)
    row = db.query(TelegramBot).filter(TelegramBot.id == bot_id).one_or_none()
    if not row or row.owner_type != "CLIENT" or row.owner_ref_id != client.id:
        raise HTTPException(status_code=404, detail="Bot non trovato")

    # Best-effort deleteWebhook su Telegram
    try:
        from app.services.telegram_bots import decrypt_token as _dt
        token = _dt(row.bot_token_enc)
        delete_webhook(token)
    except (BotApiError, TokenEncryptionError) as exc:
        # Non blocco il soft-delete; logghiamo motivo
        row.last_error = f"deleteWebhook: {exc}"[:500]

    row.status = "REVOKED"
    db.add(row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="CLIENT",
        actor_id=getattr(user, "id", None),
        action="TELEGRAM_BOT_REVOKED",
        entity_type="TELEGRAM_BOT",
        entity_id=bot_id,
        details={"bot_username": row.bot_username, "client_id": client.id},
    ))
    db.commit()
    return {"ok": True, "bot_id": bot_id, "status": row.status}
