from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import AuditLog, Client, License, SignalFormat, SignalParseLog, SignalRoom
from app.services.bridge_files import enqueue_command
from app.services.bridge_queue import persist_command_for_licenses
from app.services.ea_security import verify_ea_signature
from app.services.licenses import hash_activation_code, is_license_runtime_valid, normalize_license_status
from app.services.signal_parser import canonical_to_bridge_payload, parse_signal
from app.services.system_control import ea_bridge_enabled, signals_enabled
from app.services.telegram_service import get_me, get_webhook_info, send_message


def _is_dev_env() -> bool:
    return get_settings().app_env.lower() in {"development", "dev", "local"}

router = APIRouter(prefix="/telegram", tags=["telegram"])

# ─────────────────────────────────────────────────────────────
# STATI CONVERSAZIONE BOT (in-memory, MVP senza Redis)
# Traccia utenti in attesa di inserire il codice licenza
# ─────────────────────────────────────────────────────────────
_AWAITING_LICENSE: set[str] = set()   # chat_id (str) che ha ricevuto la richiesta di codice


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
        errors={
            "warnings": result.get("warnings", []),
            "errors": result.get("errors", []),
            "validation": result.get("validation", {}),
        },
    )
    db.add(row)
    db.flush()
    return row


def _try_activate_license(db: Session, chat_id_s: str, chat_id: int | str, text: str) -> dict | None:
    """
    Prova ad attivare una licenza con il testo inviato (codice grezzo, senza comando).
    Ritorna un dict con action se gestito, None se il testo non è un codice licenza valido.
    """
    activation_code = text.strip().upper()

    # Filtro rapido: il codice deve avere almeno 6 caratteri alfanumerici/trattini
    if len(activation_code) < 6 or not any(c.isalnum() for c in activation_code):
        return None

    # Row lock per evitare double-spend del codice (no-op su SQLite)
    license_q = (
        db.query(License)
        .filter(License.activation_code_hash == hash_activation_code(activation_code))
        .order_by(License.created_at.desc())
    )
    try:
        license_row = license_q.with_for_update().first()
    except Exception:
        license_row = license_q.first()

    if not license_row:
        # Non è un codice licenza valido → non interferire con altri handler
        return None

    now = datetime.now(timezone.utc)
    normalize_license_status(license_row, now=now)

    if not license_row.activation_code_expires_at or license_row.activation_code_expires_at < now:
        db.commit()
        _AWAITING_LICENSE.discard(chat_id_s)
        send_message(
            chat_id,
            "❌ Il codice è scaduto.\n\nTorna nella tua dashboard → sezione Licenza → clicca 'Genera nuovo codice' e reinviamelo qui."
        )
        return {"ok": True, "action": "ACTIVATE_EXPIRED"}

    # Re-check sotto lock: se altra richiesta ha già usato il codice, abort
    if license_row.activation_code_used_at:
        db.commit()
        _AWAITING_LICENSE.discard(chat_id_s)
        send_message(
            chat_id,
            "❌ Questo codice è già stato usato.\n\nTorna nella dashboard → Licenza → 'Genera nuovo codice' e reinviamelo."
        )
        return {"ok": True, "action": "ACTIVATE_USED"}

    valid, reason = is_license_runtime_valid(license_row, now=now)
    if not valid:
        db.commit()
        _AWAITING_LICENSE.discard(chat_id_s)
        send_message(chat_id, f"❌ Licenza non attivabile: {reason}\n\nContatta il supporto se credi si tratti di un errore.")
        return {"ok": True, "action": "ACTIVATE_LICENSE_INVALID"}

    if not license_row.client_id:
        send_message(chat_id, "❌ Licenza non collegata a un profilo. Contatta il supporto.")
        return {"ok": True, "action": "ACTIVATE_NO_CLIENT"}

    client = db.query(Client).filter(Client.id == license_row.client_id).first()
    if not client:
        send_message(chat_id, "❌ Profilo cliente non trovato. Contatta il supporto.")
        return {"ok": True, "action": "ACTIVATE_NO_CLIENT"}

    # Attivazione riuscita
    client.telegram_chat_id = chat_id_s
    license_row.activation_code_used_at = now
    license_row.activation_code_hash = None
    db.add(client)
    db.add(license_row)
    db.add(AuditLog(
        id=str(uuid.uuid4()),
        actor_type="TELEGRAM",
        actor_id=chat_id_s,
        action="LICENSE_TELEGRAM_ACTIVATED",
        entity_type="LICENSE",
        entity_id=license_row.id,
        details={"client_id": client.id},
        created_at=now,
    ))
    db.commit()
    _AWAITING_LICENSE.discard(chat_id_s)

    send_message(
        chat_id,
        f"✅ Perfetto, {client.full_name or 'ciao'}! La tua licenza è attivata con successo.\n\n"
        "Il tuo account Telegram è ora collegato al sistema SoftiBridge.\n"
        "Riceverai qui le notifiche per ogni ordine eseguito, Stop Loss, TP1 e TP2. 📊\n\n"
        "Se non lo hai ancora fatto, completa la configurazione MT4 dalla tua dashboard e installa l'EA sulla VPS. 🚀"
    )
    return {
        "ok": True,
        "action": "ACTIVATE_OK",
        "license_id": license_row.id,
        "client_id": client.id,
    }


def _send_order_notification(client: Client, canonical: dict, event_type: str = "OPENED") -> None:
    """
    Invia notifica Telegram all'utente per ogni evento sul suo ordine.
    event_type: OPENED | SL_HIT | TP1_HIT | TP2_HIT | CLOSED
    """
    if not client or not client.telegram_chat_id:
        return

    symbol = canonical.get("symbol", "?")
    side = canonical.get("side", "?")
    entry = canonical.get("entry_lo") or canonical.get("entry") or "?"
    sl = canonical.get("sl_price") or canonical.get("sl") or "?"
    tp1 = canonical.get("tp1_price") or canonical.get("tp1") or "?"
    tp2 = canonical.get("tp2_price") or canonical.get("tp2") or "?"
    pnl = canonical.get("pnl")
    pnl_str = f" — P&L: {'+'if float(pnl)>=0 else ''}${float(pnl):.2f}" if pnl is not None else ""

    if event_type == "OPENED":
        msg = (
            f"✅ Ordine Aperto\n"
            f"{'📈' if side == 'BUY' else '📉'} {side} {symbol} @ {entry}\n"
            f"🛑 SL: {sl}  |  🎯 TP1: {tp1}  |  🎯 TP2: {tp2}"
        )
    elif event_type == "SL_HIT":
        msg = f"🛑 Stop Loss raggiunto su {symbol}\nPrezzo: {sl}{pnl_str}"
    elif event_type == "TP1_HIT":
        msg = f"🎯 TP1 raggiunto su {symbol}\nPrezzo: {tp1}{pnl_str}"
    elif event_type == "TP2_HIT":
        msg = f"🎯🎯 TP2 raggiunto su {symbol}\nPrezzo: {tp2}{pnl_str}"
    elif event_type == "CLOSED":
        msg = f"📊 Posizione {symbol} chiusa\nPrezzo: {entry}{pnl_str}"
    else:
        return

    try:
        send_message(client.telegram_chat_id, msg)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────────────────────

@router.get("/health")
def telegram_health():
    settings = get_settings()
    return {
        "ok": True,
        "bot_username": settings.telegram_bot_username,
        "mode": settings.telegram_mode,
        "token_configured": bool(settings.telegram_bot_token),
        "admin_super_chat_id_configured": bool(settings.telegram_admin_super_chat_id),
    }


@router.get("/info")
def telegram_info():
    me = get_me()
    hook = get_webhook_info()
    return {"get_me": me.data, "webhook_info": hook.data, "simulated": (me.simulated or hook.simulated)}


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
):
    settings = get_settings()
    if not settings.telegram_webhook_secret:
        if not _is_dev_env():
            raise HTTPException(
                status_code=500,
                detail="TELEGRAM_WEBHOOK_SECRET non configurato: webhook chiuso in produzione",
            )
    elif x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=403, detail="Telegram webhook secret non valido")

    payload = await request.json()
    chat_id = None
    text = None
    chat_type = None
    msg = payload.get("message") or payload.get("channel_post") or {}
    if msg:
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = chat.get("type")  # private / group / supergroup / channel
        text = msg.get("text") or msg.get("caption")

    db: Session = SessionLocal()
    try:
        db.add(AuditLog(
            id=str(uuid.uuid4()),
            actor_type="TELEGRAM",
            action="WEBHOOK_UPDATE_RECEIVED",
            entity_type="UPDATE",
            entity_id=str(payload.get("update_id")) if payload.get("update_id") is not None else None,
            details={"chat_id": chat_id, "chat_type": chat_type, "text": text, "update_keys": list(payload.keys())},
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()

        processed_signals: list[dict] = []
        chat_id_s = str(chat_id) if chat_id is not None else None

        # ─────────────────────────────────────────────────────
        # CASO 1: Messaggio PRIVATO con l'utente
        # ─────────────────────────────────────────────────────
        if chat_id_s and text and chat_type == "private":

            # /start → saluto + richiesta codice licenza
            if text.strip() == "/start":
                _AWAITING_LICENSE.add(chat_id_s)
                send_message(
                    chat_id,
                    "👋 Benvenuto su SoftiBridge!\n\n"
                    "Per attivare il tuo account e iniziare a ricevere i segnali di trading, "
                    "accedi alla tua dashboard → sezione *La mia Licenza* → clicca *Genera Codice Attivazione* "
                    "e incolla qui il codice che ti viene mostrato.\n\n"
                    "📋 Inserisci il codice qui sotto:"
                )
                return {"ok": True, "chat_id": chat_id, "text": text, "signals": [], "action": "START_COMMAND"}

            # Testo nel canale privato: prova attivazione licenza prima
            # (funziona sia se bot in stato awaiting che per chi manda direttamente il codice)
            if not text.strip().startswith("/"):
                result = _try_activate_license(db, chat_id_s, chat_id, text)
                if result:
                    return {**result, "chat_id": chat_id, "text": text, "signals": []}

                # Se era in attesa di codice ma ha scritto qualcosa di non riconoscibile
                if chat_id_s in _AWAITING_LICENSE:
                    send_message(
                        chat_id,
                        "🤔 Non ho riconosciuto il codice licenza.\n\n"
                        "Assicurati di copiare esattamente il codice dalla dashboard (es. SB-XXXXXXXX) e incollarlo qui.\n"
                        "Nessun testo extra, solo il codice."
                    )
                    return {"ok": True, "chat_id": chat_id, "text": text, "signals": [], "action": "INVALID_CODE_FORMAT"}

        # ─────────────────────────────────────────────────────
        # CASO 2: Messaggio da canale/gruppo — parsing segnali
        # (il bot è admin del canale, legge silenziosa)
        # ─────────────────────────────────────────────────────
        # H1: rispetta system control. Se segnali disabilitati (MAINTENANCE/FROZEN/SHUTDOWN),
        # salta enqueue ma non blocca audit del messaggio.
        signals_active = signals_enabled() and ea_bridge_enabled()
        if chat_id_s and text and chat_type in ("group", "supergroup", "channel"):
            rooms = (
                db.query(SignalRoom)
                .filter(SignalRoom.active.is_(True))
                .filter(SignalRoom.source_chat_id == chat_id_s)
                .all()
            )
            for room in rooms:
                policy = room.parser_policy or {}
                if policy.get("auto_ingest_enabled", True) is False:
                    processed_signals.append({"room_id": room.id, "status": "SKIPPED_POLICY_DISABLED"})
                    continue
                formats = _load_formats_for_room(db, room.id)
                parsed = parse_signal(text, template_formats=formats)
                parsed_data = parsed.to_dict()
                log = _save_parse_log(
                    db,
                    room_id=room.id,
                    source_chat_id=chat_id_s,
                    raw_text=text,
                    result=parsed_data,
                )
                threshold = int(policy.get("auto_enqueue_threshold", 85) or 85)
                require_valid_logic = bool(policy.get("require_valid_logic", True))
                write_mt4 = bool(policy.get("write_mt4", True))
                write_mt5 = bool(policy.get("write_mt5", True))
                should_enqueue = parsed.matched and parsed.confidence >= threshold
                if require_valid_logic and not parsed.validation.get("valid_logic"):
                    should_enqueue = False
                if should_enqueue and not signals_active:
                    should_enqueue = False
                    parsed.warnings.append("system_control_blocked")
                enqueue_meta = None
                bridge_payload = None
                if should_enqueue:
                    # ── Client della room (con telegram per notifica) ──
                    clients_in_room = (
                        db.query(Client)
                        .filter(Client.signal_room_id == room.id)
                        .filter(Client.telegram_chat_id.isnot(None))
                        .all()
                    )
                    # Fallback: vecchio comportamento (room.client_id singolo)
                    if not clients_in_room and room.client_id:
                        single_client = db.query(Client).filter(Client.id == room.client_id).first()
                        if single_client:
                            clients_in_room = [single_client]

                    # H11: enqueue solo se almeno un client ha licenza runtime-valid
                    now_dt = datetime.now(timezone.utc)
                    valid_clients: list[Client] = []
                    valid_license_ids: list[str] = []
                    for cl in clients_in_room:
                        lic = (
                            db.query(License)
                            .filter(License.client_id == cl.id)
                            .order_by(License.created_at.desc())
                            .first()
                        )
                        if not lic:
                            continue
                        ok, _ = is_license_runtime_valid(lic, now=now_dt)
                        if ok:
                            valid_clients.append(cl)
                            valid_license_ids.append(lic.id)

                    if not valid_clients:
                        should_enqueue = False
                        parsed.warnings.append("no_valid_license_in_room")

                db_cmd_rows: list = []
                if should_enqueue:
                    bridge_payload = canonical_to_bridge_payload(parsed.canonical, source_chat_id=chat_id_s)
                    # File legacy (back-compat dev/single-tenant)
                    enqueue_meta = enqueue_command(bridge_payload, write_mt4=write_mt4, write_mt5=write_mt5)
                    # DB per-license (multi-tenant, sidecar polls /api/ea/commands/pull)
                    db_cmd_rows = persist_command_for_licenses(
                        db,
                        license_ids=valid_license_ids,
                        payload=bridge_payload,
                        cmd_kind="SIGNAL",
                        write_mt4=write_mt4,
                        write_mt5=write_mt5,
                        room_id=room.id,
                        source_chat_id=chat_id_s,
                    )

                    for cl in valid_clients:
                        _send_order_notification(cl, parsed.canonical, event_type="OPENED")

                    db.add(AuditLog(
                        id=str(uuid.uuid4()),
                        actor_type="TELEGRAM",
                        actor_id=chat_id_s,
                        action="WEBHOOK_SIGNAL_ENQUEUED",
                        entity_type="SIGNAL_PARSE_LOG",
                        entity_id=log.id,
                        details={
                            "room_id": room.id,
                            "confidence": parsed.confidence,
                            "parser_used": parsed.parser_used,
                            "payload": bridge_payload,
                            "notified_clients": len(valid_clients),
                            "db_cmd_ids": [r.id for r in db_cmd_rows],
                            "db_cmd_count": len(db_cmd_rows),
                        },
                        created_at=datetime.now(timezone.utc),
                    ))
                else:
                    db.add(AuditLog(
                        id=str(uuid.uuid4()),
                        actor_type="TELEGRAM",
                        actor_id=chat_id_s,
                        action="WEBHOOK_SIGNAL_REVIEW_REQUIRED",
                        entity_type="SIGNAL_PARSE_LOG",
                        entity_id=log.id,
                        details={
                            "room_id": room.id,
                            "confidence": parsed.confidence,
                            "parser_used": parsed.parser_used,
                            "warnings": parsed.warnings,
                            "errors": parsed.errors,
                            "validation": parsed.validation,
                        },
                        created_at=datetime.now(timezone.utc),
                    ))
                processed_signals.append({
                    "room_id": room.id,
                    "room_name": room.name,
                    "matched": parsed.matched,
                    "parser_used": parsed.parser_used,
                    "confidence": parsed.confidence,
                    "valid_logic": bool(parsed.validation.get("valid_logic")),
                    "enqueued": bool(enqueue_meta),
                    "log_id": log.id,
                })

        db.commit()
    finally:
        db.close()

    return {"ok": True, "chat_id": chat_id, "text": text, "signals": processed_signals}


@router.post("/notify/trade-event")
async def notify_trade_event(request: Request):
    """
    Endpoint chiamato dall'EA per notificare eventi sul trade:
    SL preso, TP1, TP2 raggiunti, posizione chiusa.
    Richiede firma HMAC EA (stessa di /ea/validate).
    Payload atteso: {
        license_id, install_id, account_number, platform, timestamp, signature,  # auth EA
        event_type, symbol, side, price, pnl, ticket,                            # evento
    }
    """
    payload = await request.json()

    # C1: verifica firma EA prima di qualsiasi azione
    license_id = payload.get("license_id")
    install_id = payload.get("install_id")
    account_number = payload.get("account_number")
    platform = payload.get("platform")
    timestamp = payload.get("timestamp")
    signature = payload.get("signature")
    if not all([license_id, install_id, account_number, platform, timestamp, signature]):
        raise HTTPException(status_code=401, detail="Firma EA mancante")
    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Timestamp non valido")
    if not verify_ea_signature(
        license_id=license_id,
        install_id=install_id,
        account_number=str(account_number),
        platform=str(platform),
        timestamp=ts_int,
        signature=str(signature),
    ):
        raise HTTPException(status_code=403, detail="Firma EA non valida")

    event_type = (payload.get("event_type") or "").upper()  # SL_HIT | TP1_HIT | TP2_HIT | CLOSED
    symbol = payload.get("symbol", "?")
    side = payload.get("side", "")
    price = payload.get("price")
    pnl = payload.get("pnl")
    ticket = payload.get("ticket")

    if not event_type:
        raise HTTPException(status_code=400, detail="event_type richiesto")

    db: Session = SessionLocal()
    try:
        # Risolvi client tramite licenza autenticata, non da client_id arbitrario
        lic = db.query(License).filter(License.id == license_id).first()
        if not lic or not lic.client_id:
            return {"ok": False, "reason": "licenza non trovata o senza client"}
        client = db.query(Client).filter(Client.id == lic.client_id).first()
        if not client or not client.telegram_chat_id:
            return {"ok": False, "reason": "cliente non trovato o Telegram non collegato"}
        client_id = client.id

        pnl_str = ""
        if pnl is not None:
            try:
                pnl_f = float(pnl)
                pnl_str = f"\nP&L: {'+'if pnl_f >= 0 else ''}${pnl_f:.2f}"
            except Exception:
                pass

        ticket_str = f" — Ticket #{ticket}" if ticket else ""

        if event_type == "SL_HIT":
            msg = f"🛑 Stop Loss raggiunto{ticket_str}\n{symbol} {side} @ {price}{pnl_str}"
        elif event_type == "TP1_HIT":
            msg = f"🎯 TP1 raggiunto{ticket_str}\n{symbol} {side} @ {price}{pnl_str}"
        elif event_type == "TP2_HIT":
            msg = f"🎯🎯 TP2 raggiunto{ticket_str}\n{symbol} {side} @ {price}{pnl_str}"
        elif event_type == "CLOSED":
            msg = f"📊 Posizione chiusa{ticket_str}\n{symbol} {side} @ {price}{pnl_str}"
        elif event_type == "OPENED":
            msg = f"✅ Ordine eseguito{ticket_str}\n{symbol} {side} @ {price}"
        else:
            msg = f"📬 Evento {event_type} su {symbol}{ticket_str}{pnl_str}"

        send_message(client.telegram_chat_id, msg)

        db.add(AuditLog(
            id=str(uuid.uuid4()),
            actor_type="EA",
            actor_id=str(ticket) if ticket else None,
            action=f"TRADE_EVENT_{event_type}",
            entity_type="CLIENT",
            entity_id=client_id,
            details={"symbol": symbol, "side": side, "price": price, "pnl": pnl, "event_type": event_type},
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
        return {"ok": True, "notified": True, "client_id": client_id}
    finally:
        db.close()
