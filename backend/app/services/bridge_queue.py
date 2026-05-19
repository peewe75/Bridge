from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models import BridgeCommand
from app.services.bridge_files import format_cmd_line


DEFAULT_TTL_HOURS = 12


def _ensure_id_ts(payload: dict[str, Any], *, prefix: str = "SIG") -> dict[str, Any]:
    payload = dict(payload)
    if not payload.get("id"):
        payload["id"] = f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    if not payload.get("ts"):
        payload["ts"] = int(time.time())
    return payload


def _platform_label(*, write_mt4: bool, write_mt5: bool) -> str:
    if write_mt4 and write_mt5:
        return "BOTH"
    if write_mt4:
        return "MT4"
    if write_mt5:
        return "MT5"
    return "BOTH"


def persist_command_for_licenses(
    db: Session,
    *,
    license_ids: Iterable[str],
    payload: dict[str, Any],
    cmd_kind: str = "SIGNAL",
    write_mt4: bool = True,
    write_mt5: bool = True,
    room_id: str | None = None,
    source_chat_id: str | None = None,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> list[BridgeCommand]:
    """
    Inserisce un BridgeCommand per ogni license_id target.
    Tutti i record condividono la stessa payload_kv ma hanno cmd_uid univoco
    (così l'EA/sidecar di ogni licenza vede solo il SUO record).
    Ritorna le righe inserite (id/cmd_uid leggibili dopo db.flush()).
    """
    base_prefix = "CTRL" if cmd_kind == "CONTROL" else "SIG"
    canonical = _ensure_id_ts(payload, prefix=base_prefix)
    base_uid = canonical["id"]
    platform = _platform_label(write_mt4=write_mt4, write_mt5=write_mt5)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=max(1, int(ttl_hours)))

    rows: list[BridgeCommand] = []
    license_ids = [lid for lid in license_ids if lid]
    if not license_ids:
        return rows

    for i, lic_id in enumerate(license_ids):
        # Variant per licenza per garantire unicità cmd_uid e tracciabilità
        cmd_uid = f"{base_uid}-L{i+1}" if len(license_ids) > 1 else base_uid
        per_payload = dict(canonical)
        per_payload["id"] = cmd_uid
        payload_kv = format_cmd_line(per_payload)
        row = BridgeCommand(
            license_id=lic_id,
            cmd_uid=cmd_uid,
            platform=platform,
            cmd_kind=cmd_kind,
            payload_kv=payload_kv,
            status="PENDING",
            source_chat_id=source_chat_id,
            room_id=room_id,
            expires_at=expires,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows
