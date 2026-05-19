"""
SoftiBridge Sidecar Agent
─────────────────────────
Tiny daemon che gira sul VPS del client accanto a MT4/MT5.
Polla il backend SoftiBridge via HTTP (HMAC EA), scrive cmd_queue.txt
locale per l'EA esistente (intoccato), poi acka.

Config: sidecar_config.json (vedi sidecar_config.json.example).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_POLL_SECONDS = 2.0
DEFAULT_BACKOFF_MAX_SECONDS = 30.0
SINCE_STATE_FILENAME = ".sidecar_since.json"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_config(path: Path) -> dict:
    if not path.exists():
        log(f"[ERROR] Config file non trovato: {path}")
        sys.exit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"[ERROR] Config invalido: {exc}")
        sys.exit(2)


def sign_ea_message(cfg: dict, timestamp: int) -> str:
    msg = f"{cfg['license_id']}|{cfg['install_id']}|{cfg['account_number']}|{cfg['platform']}|{timestamp}".encode("utf-8")
    secret = cfg["ea_hmac_secret"].encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def http_post_json(url: str, payload: dict, timeout: float = 10.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def load_since(state_dir: Path) -> int:
    p = state_dir / SINCE_STATE_FILENAME
    if not p.exists():
        return 0
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return int(d.get("since_id", 0))
    except Exception:
        return 0


def save_since(state_dir: Path, since_id: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / SINCE_STATE_FILENAME
    p.write_text(json.dumps({"since_id": int(since_id)}), encoding="utf-8")


def ensure_utf8(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return
    try:
        b = path.read_bytes()
        if b.startswith(b"\xef\xbb\xbf"):
            path.write_text(path.read_text(encoding="utf-8-sig", errors="ignore"), encoding="utf-8")
    except Exception:
        pass


def write_cmd_to_queue(queue_path: Path, line: str) -> None:
    """Replica della logica safe_write_queue_replace0 del backend."""
    ensure_utf8(queue_path)
    line = line.strip()
    if not line:
        return
    if not queue_path.exists():
        queue_path.write_text(line + "\n", encoding="utf-8")
        return
    txt = queue_path.read_text(encoding="utf-8", errors="ignore").strip()
    if txt in {"", "0"}:
        queue_path.write_text(line + "\n", encoding="utf-8")
        return
    with queue_path.open("a", encoding="utf-8") as f:
        if not txt.endswith("\n"):
            f.write("\n")
        f.write(line + "\n")


def resolve_queue_path(cfg: dict, platform: str) -> Path:
    base = Path(cfg["bridge_files_dir"]).expanduser()
    inbox = base / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    fname = "cmd_queue.txt" if platform == "MT4" else "cmd_queue_mt5.txt"
    return inbox / fname


def pull_and_apply(cfg: dict, since_id: int) -> tuple[int, int]:
    """Pull → write file → ack. Ritorna (new_since_id, n_applied)."""
    ts = int(time.time())
    sig = sign_ea_message(cfg, ts)
    pull_url = cfg["api_base"].rstrip("/") + "/api/ea/commands/pull"
    ack_url = cfg["api_base"].rstrip("/") + "/api/ea/commands/ack"

    pull_payload = {
        "license_id": cfg["license_id"],
        "install_id": cfg["install_id"],
        "account_number": cfg["account_number"],
        "platform": cfg["platform"],
        "timestamp": ts,
        "signature": sig,
        "since_id": int(since_id),
        "limit": int(cfg.get("pull_limit", 50)),
    }
    resp = http_post_json(pull_url, pull_payload, timeout=float(cfg.get("http_timeout_sec", 10)))
    commands = resp.get("commands") or []
    if not commands:
        return since_id, 0

    applied = 0
    max_id = since_id
    queue_path = resolve_queue_path(cfg, cfg["platform"])
    for cmd in commands:
        try:
            write_cmd_to_queue(queue_path, cmd["payload_kv"])
            applied += 1
            ack_ts = int(time.time())
            ack_sig = sign_ea_message(cfg, ack_ts)
            ack_payload = {
                "license_id": cfg["license_id"],
                "install_id": cfg["install_id"],
                "account_number": cfg["account_number"],
                "platform": cfg["platform"],
                "timestamp": ack_ts,
                "signature": ack_sig,
                "cmd_id": cmd["id"],
                "cmd_uid": cmd.get("cmd_uid"),
                "status": "OK",
            }
            try:
                http_post_json(ack_url, ack_payload, timeout=float(cfg.get("http_timeout_sec", 10)))
            except Exception as ack_err:
                log(f"[WARN] ACK failed for cmd {cmd['id']}: {ack_err}")
        except Exception as werr:
            log(f"[ERROR] write/ack failed for cmd {cmd.get('id')}: {werr}")
            ack_ts = int(time.time())
            ack_sig = sign_ea_message(cfg, ack_ts)
            try:
                http_post_json(ack_url, {
                    "license_id": cfg["license_id"],
                    "install_id": cfg["install_id"],
                    "account_number": cfg["account_number"],
                    "platform": cfg["platform"],
                    "timestamp": ack_ts,
                    "signature": ack_sig,
                    "cmd_id": cmd.get("id"),
                    "cmd_uid": cmd.get("cmd_uid"),
                    "status": "ERROR",
                    "msg": str(werr)[:200],
                }, timeout=float(cfg.get("http_timeout_sec", 10)))
            except Exception:
                pass
        finally:
            if int(cmd.get("id", 0)) > max_id:
                max_id = int(cmd["id"])
    return max_id, applied


def main() -> None:
    cfg_path = Path(os.environ.get("SOFTIBRIDGE_SIDECAR_CONFIG") or "sidecar_config.json")
    cfg = load_config(cfg_path)
    required = ["api_base", "license_id", "install_id", "account_number", "platform", "ea_hmac_secret", "bridge_files_dir"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        log(f"[ERROR] Config mancante: {missing}")
        sys.exit(2)

    state_dir = Path(cfg.get("state_dir") or cfg_path.parent)
    poll_sec = float(cfg.get("poll_interval_sec", DEFAULT_POLL_SECONDS))
    since_id = load_since(state_dir)
    log(f"[INFO] SoftiBridge Sidecar avviato")
    log(f"[INFO] license_id={cfg['license_id']} platform={cfg['platform']} since_id={since_id}")
    log(f"[INFO] api_base={cfg['api_base']} bridge_files_dir={cfg['bridge_files_dir']}")

    backoff = poll_sec
    while True:
        try:
            new_since, applied = pull_and_apply(cfg, since_id)
            if applied:
                log(f"[INFO] applied={applied} new_since_id={new_since}")
                since_id = new_since
                save_since(state_dir, since_id)
            backoff = poll_sec
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:200]
            log(f"[ERROR] HTTP {e.code}: {body}")
            backoff = min(backoff * 2, DEFAULT_BACKOFF_MAX_SECONDS)
        except urllib.error.URLError as e:
            log(f"[ERROR] Network: {e}")
            backoff = min(backoff * 2, DEFAULT_BACKOFF_MAX_SECONDS)
        except Exception as e:
            log(f"[ERROR] Unexpected: {e}")
            backoff = min(backoff * 2, DEFAULT_BACKOFF_MAX_SECONDS)
        time.sleep(backoff)


if __name__ == "__main__":
    main()
