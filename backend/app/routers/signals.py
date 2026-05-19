from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_roles
from app.models import AuditLog, SignalFormat, SignalParseLog, SignalRoom, User
from app.schemas import (
    SignalFormatCreateRequest,
    SignalFormatOut,
    SignalIngestRequest,
    SignalParseLogOut,
    SignalParseResult,
    SignalParseTestRequest,
    SignalRoomCreateRequest,
    SignalRoomOut,
)
from app.services.bridge_files import enqueue_command
from app.services.signal_parser import canonical_to_bridge_payload, parse_signal
from app.services.system_control import ea_bridge_enabled, signals_enabled

router = APIRouter(prefix="/signals", tags=["signals"])


def _load_formats_for_room(db: Session, room_id: str | None) -> list[SignalFormat]:
    q = db.query(SignalFormat).filter(SignalFormat.enabled.is_(True))
    if room_id:
        q = q.filter((SignalFormat.room_id == room_id) | (SignalFormat.room_id.is_(None)))
    else:
        q = q.filter(SignalFormat.room_id.is_(None))
    return q.order_by(SignalFormat.priority.asc()).all()


def _save_parse_log(db: Session, *, room_id: str | None, source_chat_id: str | None, raw_text: str, result: dict) -> SignalParseLog:
    row = SignalParseLog(
        id=str(uuid.uuid4()),
        room_id=room_id,
        source_chat_id=source_chat_id,
        raw_text=raw_text,
        normalized_text=result.get("normalized_text") or "",
        parser_used=result.get("parser_used"),
        result_mode=result.get("mode"),
        confidence=int(result.get("confidence") or 0),
        valid=bool(result.get("validation", {}).get("valid_logic")) and bool(result.get("matched")),
        parsed_payload=result.get("canonical") or {},
        errors={"warnings": result.get("warnings", []), "errors": result.get("errors", []), "validation": result.get("validation", {})},
    )
    db.add(row)
    db.flush()
    return row


@router.get("/rooms", response_model=list[SignalRoomOut])
def list_rooms(_user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT")), db: Session = Depends(get_db)):
    rows = db.query(SignalRoom).order_by(SignalRoom.created_at.desc()).all()
    return [SignalRoomOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/rooms", response_model=SignalRoomOut)
def create_room(req: SignalRoomCreateRequest, user: User = Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT")), db: Session = Depends(get_db)):
    row = SignalRoom(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        name=req.name,
        source_type=req.source_type,
        source_chat_id=req.source_chat_id,
        symbol_defaults=req.symbol_defaults,
        parser_policy=req.parser_policy,
        active=True,
    )
    db.add(row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="USER",
        actor_id=user.id,
        action="SIGNAL_ROOM_CREATED",
        entity_type="SIGNAL_ROOM",
        entity_id=row.id,
        details={"name": req.name, "source_type": req.source_type, "source_chat_id": req.source_chat_id},
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.refresh(row)
    return SignalRoomOut.model_validate(row, from_attributes=True)


@router.get("/formats", response_model=list[SignalFormatOut])
def list_formats(_user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT")), db: Session = Depends(get_db), room_id: str | None = None):
    rows = _load_formats_for_room(db, room_id)
    return [SignalFormatOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/formats", response_model=SignalFormatOut)
def create_format(req: SignalFormatCreateRequest, user: User = Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT")), db: Session = Depends(get_db)):
    if req.parser_kind == "REGEX_TEMPLATE":
        if not req.regex_pattern:
            raise HTTPException(status_code=400, detail="regex_pattern obbligatoria per REGEX_TEMPLATE")
        try:
            re.compile(req.regex_pattern, re.IGNORECASE | re.MULTILINE)
        except re.error as exc:
            raise HTTPException(status_code=400, detail=f"Regex non valida: {exc}") from exc
    row = SignalFormat(
        id=str(uuid.uuid4()),
        room_id=req.room_id,
        name=req.name,
        parser_kind=req.parser_kind,
        mode_hint=req.mode_hint,
        regex_pattern=req.regex_pattern,
        field_map=req.field_map,
        priority=req.priority,
        enabled=req.enabled,
        created_by_user_id=user.id,
    )
    db.add(row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="USER",
        actor_id=user.id,
        action="SIGNAL_FORMAT_CREATED",
        entity_type="SIGNAL_FORMAT",
        entity_id=row.id,
        details={"room_id": req.room_id, "name": req.name, "parser_kind": req.parser_kind, "priority": req.priority},
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    db.refresh(row)
    return SignalFormatOut.model_validate(row, from_attributes=True)


@router.post("/parse/test", response_model=SignalParseResult)
def parse_test(req: SignalParseTestRequest, _user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT")), db: Session = Depends(get_db)):
    formats = _load_formats_for_room(db, req.room_id)
    out = parse_signal(req.text, template_formats=formats)
    data = out.to_dict()
    if req.save_log:
        log = _save_parse_log(db, room_id=req.room_id, source_chat_id=req.source_chat_id, raw_text=req.text, result=data)
        db.commit()
        data["log_id"] = log.id
    return SignalParseResult(
        matched=data["matched"],
        parser_used=data.get("parser_used"),
        confidence=data.get("confidence", 0),
        mode=data.get("mode"),
        canonical=data.get("canonical", {}),
        warnings=data.get("warnings", []),
        errors=data.get("errors", []),
        validation=data.get("validation", {}),
    )


@router.post("/ingest")
def signal_ingest(req: SignalIngestRequest, user: User = Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT")), db: Session = Depends(get_db)):
    formats = _load_formats_for_room(db, req.room_id)
    out = parse_signal(req.text, template_formats=formats)
    data = out.to_dict()
    log = _save_parse_log(db, room_id=req.room_id, source_chat_id=req.source_chat_id, raw_text=req.text, result=data)

    enqueue_info = None
    should_enqueue = out.matched and out.confidence >= req.auto_enqueue_threshold
    if req.require_valid_logic and not out.validation.get("valid_logic"):
        should_enqueue = False
    if should_enqueue and not (signals_enabled() and ea_bridge_enabled()):
        should_enqueue = False
        out.warnings.append("system_control_blocked")
    if should_enqueue:
        payload = canonical_to_bridge_payload(out.canonical, source_chat_id=req.source_chat_id)
        enqueue_info = enqueue_command(payload, write_mt4=req.write_mt4, write_mt5=req.write_mt5)
        db.add(AuditLog(
            id=str(uuid.uuid4()),
            actor_type="USER",
            actor_id=user.id,
            action="SIGNAL_INGEST_ENQUEUED",
            entity_type="SIGNAL_PARSE_LOG",
            entity_id=log.id,
            details={"confidence": out.confidence, "payload": payload},
            created_at=datetime.now(timezone.utc),
        ))
    else:
        db.add(AuditLog(
            id=str(uuid.uuid4()),
            actor_type="USER",
            actor_id=user.id,
            action="SIGNAL_INGEST_REVIEW_REQUIRED",
            entity_type="SIGNAL_PARSE_LOG",
            entity_id=log.id,
            details={"confidence": out.confidence, "errors": out.errors, "warnings": out.warnings},
            created_at=datetime.now(timezone.utc),
        ))
    db.commit()
    return {
        "ok": True,
        "log_id": log.id,
        "parsed": {
            "matched": out.matched,
            "parser_used": out.parser_used,
            "confidence": out.confidence,
            "mode": out.mode,
            "canonical": out.canonical,
            "warnings": out.warnings,
            "errors": out.errors,
            "validation": out.validation,
        },
        "enqueued": bool(enqueue_info),
        "enqueue": enqueue_info,
    }


@router.get("/parse-logs", response_model=list[SignalParseLogOut])
def list_parse_logs(
    _user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT")),
    db: Session = Depends(get_db),
    room_id: str | None = None,
    limit: int = 100,
):
    q = db.query(SignalParseLog)
    if room_id:
        q = q.filter(SignalParseLog.room_id == room_id)
    rows = q.order_by(SignalParseLog.created_at.desc()).limit(min(limit, 500)).all()
    return [SignalParseLogOut.model_validate(r, from_attributes=True) for r in rows]

