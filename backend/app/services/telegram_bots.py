"""
BYO Telegram bot lifecycle.
- Encryption Fernet dei token a riposo (chiave TELEGRAM_TOKEN_ENC_KEY).
- Verifica token via Telegram getMe.
- Registrazione/cancellazione webhook per token.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass

from app.config import get_settings


class TokenEncryptionError(RuntimeError):
    pass


class BotApiError(RuntimeError):
    pass


@dataclass
class BotIdentity:
    bot_username: str
    bot_id: int | None
    raw_get_me: dict


def _fernet():
    try:
        from cryptography.fernet import Fernet, InvalidToken  # type: ignore
    except ImportError as exc:
        raise TokenEncryptionError(
            "Pacchetto 'cryptography' mancante (pip install cryptography)"
        ) from exc
    key = (get_settings().telegram_token_enc_key or "").strip()
    if not key:
        raise TokenEncryptionError("TELEGRAM_TOKEN_ENC_KEY non configurato")
    try:
        return Fernet(key.encode("utf-8")), InvalidToken
    except Exception as exc:
        raise TokenEncryptionError(f"TELEGRAM_TOKEN_ENC_KEY non valido: {exc}") from exc


def encrypt_token(plaintext: str) -> str:
    f, _ = _fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str) -> str:
    f, InvalidToken = _fernet()
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise TokenEncryptionError("Token decrypt failed (chiave cambiata?)") from exc


def generate_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def _http_json(url: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise BotApiError(f"Telegram HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise BotApiError(f"Telegram network error: {e}") from e


def verify_token(token: str) -> BotIdentity:
    """Chiama getMe per validare token. Raise BotApiError se invalido."""
    data = _http_json(f"https://api.telegram.org/bot{token}/getMe")
    if not data.get("ok"):
        raise BotApiError(f"getMe ok=false: {data}")
    result = data.get("result") or {}
    username = result.get("username") or ""
    bot_id = result.get("id")
    return BotIdentity(bot_username=username, bot_id=bot_id, raw_get_me=result)


def set_webhook(token: str, webhook_url: str, secret_token: str) -> dict:
    """Registra webhook con secret_token per quel bot."""
    return _http_json(
        f"https://api.telegram.org/bot{token}/setWebhook",
        payload={
            "url": webhook_url,
            "secret_token": secret_token,
            "drop_pending_updates": True,
        },
    )


def delete_webhook(token: str) -> dict:
    """Rimuove webhook (usato quando il client elimina il bot)."""
    return _http_json(
        f"https://api.telegram.org/bot{token}/deleteWebhook",
        payload={"drop_pending_updates": True},
    )


def build_webhook_url(bot_id: str) -> str:
    base = (get_settings().telegram_webhook_base_url or "").rstrip("/")
    if not base:
        raise BotApiError("TELEGRAM_WEBHOOK_BASE_URL non configurato")
    return f"{base}/api/telegram/webhook/{bot_id}"
