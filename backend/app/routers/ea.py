from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AuditLog, BridgeCommand, BridgeResult, EaInstallation, License
from app.schemas import (
    EaCommandAckRequest,
    EaCommandItem,
    EaCommandPullRequest,
    EaCommandPullResponse,
    EaHeartbeatRequest,
    EaValidateRequest,
    EaValidateResponse,
)
from app.services.ea_security import verify_ea_signature
from app.services.licenses import is_license_runtime_valid, normalize_license_status
from app.services.system_control import ea_bridge_enabled

router = APIRouter(prefix="/ea", tags=["ea"])


def _validate_common(db: Session, payload) -> tuple[bool, str | None, License | None]:
    if not verify_ea_signature(
        license_id=payload.license_id,
        install_id=payload.install_id,
        account_number=payload.account_number,
        platform=payload.platform,
        timestamp=payload.timestamp,
        signature=payload.signature,
    ):
        return False, "Invalid signature or timestamp", None

    lic = db.query(License).filter(License.id == payload.license_id).one_or_none()
    if not lic:
        return False, "License not found", None

    prev_status = lic.status
    now = datetime.now(timezone.utc)
    normalize_license_status(lic, now=now)
    changed = prev_status != lic.status
    valid, reason = is_license_runtime_valid(lic, now=now)
    if changed:
        db.add(lic)
        db.commit()
    if not valid:
        return False, reason, lic

    accounts = lic.mt_accounts or {"MT4": [], "MT5": []}
    allowed = set(accounts.get(payload.platform, []))
    if str(payload.account_number) not in allowed:
        return False, "Account not authorized for license", lic

    if lic.install_id and lic.install_id != payload.install_id:
        return False, "Install ID mismatch", lic
    return True, None, lic


@router.post("/validate", response_model=EaValidateResponse)
def ea_validate(req: EaValidateRequest, db: Session = Depends(get_db)) -> EaValidateResponse:
    valid, reason, lic = _validate_common(db, req)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="EA",
        action="EA_VALIDATE",
        entity_type="LICENSE",
        entity_id=req.license_id,
        level="INFO" if valid else "WARNING",
        details={"platform": req.platform, "account_number": req.account_number, "valid": valid, "reason": reason},
    ))
    db.commit()
    return EaValidateResponse(valid=valid, reason=reason, license_status=(lic.status if lic else None), expiry_at=(lic.expiry_at if lic else None))


@router.post("/heartbeat")
def ea_heartbeat(req: EaHeartbeatRequest, db: Session = Depends(get_db)):
    valid, reason, lic = _validate_common(db, req)
    if not valid or not lic:
        return {"ok": False, "reason": reason}

    if not lic.install_id:
        lic.install_id = req.install_id

    row = (
        db.query(EaInstallation)
        .filter(EaInstallation.license_id == lic.id, EaInstallation.install_id == req.install_id, EaInstallation.account_number == req.account_number)
        .one_or_none()
    )
    if not row:
        row = EaInstallation(
            id=str(uuid.uuid4()),
            license_id=lic.id,
            install_id=req.install_id,
            platform=req.platform,
            account_number=req.account_number,
            status="ACTIVE",
        )
        db.add(row)
    row.last_heartbeat_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="EA",
        action="EA_HEARTBEAT",
        entity_type="LICENSE",
        entity_id=lic.id,
        details={"install_id": req.install_id, "platform": req.platform, "account_number": req.account_number},
    ))
    db.commit()
    return {"ok": True, "license_id": lic.id, "install_id": req.install_id}


# ─────────────────────────────────────────────────────────────
# Pull/Ack — DB-backed queue per VPS remoti (sidecar agent)
# ─────────────────────────────────────────────────────────────

@router.post("/commands/pull", response_model=EaCommandPullResponse)
def ea_commands_pull(req: EaCommandPullRequest, db: Session = Depends(get_db)) -> EaCommandPullResponse:
    valid, reason, lic = _validate_common(db, req)
    if not valid or not lic:
        raise HTTPException(status_code=403, detail=reason or "EA non autorizzata")
    if not ea_bridge_enabled():
        return EaCommandPullResponse(license_status=lic.status, commands=[], server_ts=int(time.time()))

    now = datetime.now(timezone.utc)
    q = (
        db.query(BridgeCommand)
        .filter(BridgeCommand.license_id == lic.id)
        .filter(BridgeCommand.status == "PENDING")
        .filter(BridgeCommand.id > int(req.since_id or 0))
        .filter(
            (BridgeCommand.platform == req.platform)
            | (BridgeCommand.platform == "BOTH")
        )
    )
    rows = q.order_by(BridgeCommand.id.asc()).limit(int(req.limit or 50)).all()

    # Expire scaduti senza spedirli
    out: list[EaCommandItem] = []
    for r in rows:
        if r.expires_at and r.expires_at < now:
            r.status = "EXPIRED"
            db.add(r)
            continue
        out.append(EaCommandItem(
            id=r.id,
            cmd_uid=r.cmd_uid,
            cmd_kind=r.cmd_kind,
            platform=r.platform,
            payload_kv=r.payload_kv,
            created_at=r.created_at,
        ))
    if rows:
        db.commit()

    return EaCommandPullResponse(
        license_status=lic.status,
        commands=out,
        server_ts=int(time.time()),
    )


@router.post("/commands/ack")
def ea_commands_ack(req: EaCommandAckRequest, db: Session = Depends(get_db)):
    valid, reason, lic = _validate_common(db, req)
    if not valid or not lic:
        raise HTTPException(status_code=403, detail=reason or "EA non autorizzata")

    cmd: BridgeCommand | None = None
    if req.cmd_id:
        cmd = db.query(BridgeCommand).filter(BridgeCommand.id == int(req.cmd_id)).one_or_none()
    if not cmd and req.cmd_uid:
        cmd = db.query(BridgeCommand).filter(BridgeCommand.cmd_uid == req.cmd_uid).one_or_none()

    if cmd and cmd.license_id != lic.id:
        raise HTTPException(status_code=403, detail="cmd non appartiene a questa licenza")

    if cmd:
        cmd.status = "CONSUMED" if req.status == "OK" else "FAILED"
        cmd.consumed_at = datetime.now(timezone.utc)
        db.add(cmd)

    db.add(BridgeResult(
        license_id=lic.id,
        cmd_id=cmd.id if cmd else None,
        cmd_uid=req.cmd_uid or (cmd.cmd_uid if cmd else None),
        status=req.status,
        msg=req.msg,
        ticket=req.ticket,
        extra=req.extra or {},
    ))
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="EA",
        action="EA_CMD_ACK",
        entity_type="BRIDGE_COMMAND",
        entity_id=str(cmd.id) if cmd else (req.cmd_uid or None),
        level="INFO" if req.status == "OK" else "WARNING",
        details={"status": req.status, "msg": req.msg, "ticket": req.ticket, "platform": req.platform},
    ))
    db.commit()
    return {"ok": True, "cmd_id": cmd.id if cmd else None, "status": cmd.status if cmd else None}
