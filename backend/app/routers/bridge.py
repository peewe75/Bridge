from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_roles
from app.models import AuditLog
from app.services.bridge_files import (
    append_event_line,
    bridge_status,
    enqueue_command,
    enqueue_control_command,
    read_state_snapshot,
    read_recent_events,
    read_recent_results,
    write_result_file,
)
from app.services.system_control import ea_bridge_enabled


def _require_bridge_enabled() -> None:
    if not ea_bridge_enabled():
        raise HTTPException(status_code=503, detail="EA bridge disabilitato (system control)")

router = APIRouter(prefix="/bridge", tags=["bridge"])


class BridgeCommandRequest(BaseModel):
    mode: Literal["PIPS", "PRICE", "SHORTHAND"]
    symbol: str = "XAUUSD"
    side: Literal["BUY", "SELL"]
    format: str | None = None
    src_chat: int = 0
    exec: str = "AUTO"
    threshold_pips: int = 15
    comment: str = "SoftiBridge"
    # PIPS
    entry: float | None = None
    sl_pips: int | None = None
    tp1_pips: int | None = None
    tp2_pips: int | None = None
    tp3_pips: int | None = None
    # PRICE
    entry_lo: float | None = None
    entry_hi: float | None = None
    sl_price: float | None = None
    tp1_price: float | None = None
    tp2_price: float | None = None
    tp3_price: float | None = None
    tp4_price: float | None = None
    tp_open: str | None = None
    open: int | None = None
    # SHORTHAND
    entry1: int | None = None
    entry2: int | None = None
    sl: int | None = None
    tp1: int | None = None
    tp2: int | None = None
    tp3: int | None = None
    write_mt4: bool = True
    write_mt5: bool = True


class BridgeSimEventRequest(BaseModel):
    event: str = "TP1"
    cmd_id: str = Field(default_factory=lambda: "SIM-" + uuid.uuid4().hex[:8].upper())
    symbol: str = "XAUUSD"
    side: Literal["BUY", "SELL"] = "BUY"
    extra: dict[str, Any] = Field(default_factory=dict)


class BridgeSimResultRequest(BaseModel):
    cmd_id: str = Field(default_factory=lambda: "SIM-" + uuid.uuid4().hex[:8].upper())
    status: str = "OK"
    msg: str = "SIMULATED_RESULT"


class BridgeControlRequest(BaseModel):
    action: Literal[
        "CLOSE_ALL",
        "CLOSE_BUY",
        "CLOSE_SELL",
        "CLOSE_TICKET",
        "CANCEL_ALL",
        "CANCEL_BUY",
        "CANCEL_SELL",
        "CANCEL_TICKET",
        "SET_SLTP",
        "SET_SL",
        "SET_TP",
        "MOVE_SL",
        "MOVE_BE",
    ]
    symbol: str | None = "CURRENT"
    ticket: int | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    move_sl_pips: int | None = None
    write_mt4: bool = True
    write_mt5: bool = True


@router.get("/status")
def bridge_file_status(_user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL"))):
    return bridge_status()


@router.get("/events")
def bridge_events(_user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")), limit: int = 100):
    return {"events": read_recent_events(limit=min(limit, 500))}


@router.get("/results")
def bridge_results(_user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")), limit: int = 100):
    return {"results": read_recent_results(limit=min(limit, 500))}


@router.get("/state")
def bridge_state(_user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT"))):
    return read_state_snapshot()


@router.post("/commands")
def bridge_enqueue(
    req: BridgeCommandRequest,
    _user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
    db: Session = Depends(get_db),
):
    _require_bridge_enabled()
    payload: dict[str, Any] = {
        "id": datetime.utcnow().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "ts": int(time.time()),
        "src_chat": req.src_chat,
        "mode": req.mode,
        "format": req.format or req.mode,
        "symbol": req.symbol,
        "side": req.side,
        "exec": req.exec,
        "threshold_pips": req.threshold_pips,
        "comment": req.comment,
    }
    if req.mode == "PIPS":
        required = [req.entry, req.sl_pips, req.tp1_pips]
        if any(v is None for v in required):
            raise HTTPException(status_code=400, detail="PIPS richiede entry, sl_pips, tp1_pips")
        payload.update({
            "entry": req.entry, "sl_pips": req.sl_pips,
            "tp1_pips": req.tp1_pips,
            "tp2_pips": req.tp2_pips or req.tp1_pips,
            "tp3_pips": req.tp3_pips or req.tp1_pips,
        })
    elif req.mode == "PRICE":
        required = [req.entry_lo, req.entry_hi, req.sl_price, req.tp1_price]
        if any(v is None for v in required):
            raise HTTPException(status_code=400, detail="PRICE richiede entry_lo, entry_hi, sl_price, tp1_price")
        payload.update({
            "entry_lo": req.entry_lo, "entry_hi": req.entry_hi, "sl_price": req.sl_price,
            "tp1_price": req.tp1_price, "tp2_price": req.tp2_price, "tp3_price": req.tp3_price,
            "tp4_price": req.tp4_price, "tp_open": req.tp_open, "open": req.open or 0,
        })
    else:
        required = [req.entry1, req.entry2, req.sl, req.tp1, req.tp2]
        if any(v is None for v in required):
            raise HTTPException(status_code=400, detail="SHORTHAND richiede entry1, entry2, sl, tp1, tp2")
        payload.update({
            "entry1": req.entry1, "entry2": req.entry2, "sl": req.sl,
            "tp1": req.tp1, "tp2": req.tp2, "tp3": req.tp3 or req.tp2, "open": req.open or 0,
        })
    out = enqueue_command(payload, write_mt4=req.write_mt4, write_mt5=req.write_mt5)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="ADMIN",
        action="BRIDGE_COMMAND_ENQUEUED",
        entity_type="EA_QUEUE",
        entity_id=payload["id"],
        details={"payload": payload, "queues": out},
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return {"ok": True, "id": payload["id"], **out}


@router.post("/control")
def bridge_control(
    req: BridgeControlRequest,
    user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL", "CLIENT")),
    db: Session = Depends(get_db),
):
    _require_bridge_enabled()
    action = req.action.upper()
    side_filter = None
    if action.endswith("_BUY"):
        side_filter = "BUY"
    elif action.endswith("_SELL"):
        side_filter = "SELL"
    elif action.endswith("_ALL"):
        side_filter = "ALL"
    out = enqueue_control_command(
        action=action,
        symbol=req.symbol,
        side_filter=side_filter,
        ticket=req.ticket,
        sl_price=req.sl_price,
        tp_price=req.tp_price,
        move_sl_pips=req.move_sl_pips,
        write_mt4=req.write_mt4,
        write_mt5=req.write_mt5,
    )
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="USER",
        actor_id=getattr(user, "id", None),
        action="BRIDGE_CONTROL_ENQUEUED",
        entity_type="EA_QUEUE",
        entity_id=None,
        details={"request": req.model_dump(), "queues": out},
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return {"ok": True, **out}


@router.post("/simulate/event")
def bridge_simulate_event(
    req: BridgeSimEventRequest,
    _user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
):
    if get_settings().app_env.lower() not in {"development", "dev", "local"}:
        raise HTTPException(status_code=403, detail="Solo development")
    data = {"id": req.cmd_id, "event": req.event, "symbol": req.symbol, "side": req.side}
    data.update(req.extra or {})
    line = append_event_line(data)
    return {"ok": True, "line": line}


@router.post("/simulate/result")
def bridge_simulate_result(
    req: BridgeSimResultRequest,
    _user=Depends(require_roles("SUPER_ADMIN", "ADMIN_WL")),
):
    if get_settings().app_env.lower() not in {"development", "dev", "local"}:
        raise HTTPException(status_code=403, detail="Solo development")
    path = write_result_file(cmd_id=req.cmd_id, status=req.status, msg=req.msg)
    return {"ok": True, "path": path}
