from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings


def _ensure_utf8(path: Path) -> None:
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


def _safe_append_line(path: Path, line: str, retries: int = 25) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_utf8(path)
    payload = line if line.endswith("\n") else line + "\n"
    last_err: Exception | None = None
    for i in range(retries):
        try:
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            return
        except Exception as e:
            last_err = e
            time.sleep(0.03 + 0.01 * i)
    raise RuntimeError(f"Queue append failed: {last_err}")


def safe_write_queue_replace0(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_utf8(path)
    line = line.strip()
    if not line:
        return
    if not path.exists():
        path.write_text(line + "\n", encoding="utf-8")
        return
    txt = path.read_text(encoding="utf-8", errors="ignore").strip()
    if txt in {"", "0"}:
        path.write_text(line + "\n", encoding="utf-8")
        return
    try:
        with path.open("a", encoding="utf-8") as f:
            if not txt.endswith("\n"):
                f.write("\n")
            f.write(line + "\n")
    except Exception:
        _safe_append_line(path, line)


def format_cmd_line(payload: dict[str, Any]) -> str:
    keys = [
        "id","ts","src_chat","mode","format","symbol","side",
        "entry","sl_pips","tp1_pips","tp2_pips","tp3_pips",
        "entry1","entry2","sl","tp1","tp2","tp3","open",
        "entry_lo","entry_hi","sl_price","tp1_price","tp2_price","tp3_price","tp4_price","tp_open",
        "exec","threshold_pips","comment"
    ]
    parts: list[str] = []
    for k in keys:
        if k not in payload or payload[k] is None:
            continue
        parts.append(f"{k}={payload[k]}")
    for k, v in payload.items():
        if k in keys or v is None:
            continue
        parts.append(f"{k}={v}")
    return ";".join(parts)


def parse_kv(content: str) -> dict[str, str]:
    out: dict[str, str] = {}
    s = content.strip()
    if ";" in s:
        for c in [c.strip() for c in s.split(";") if c.strip()]:
            if "=" in c:
                k, v = c.split("=", 1)
                out[k.strip()] = v.strip()
    else:
        for line in s.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@dataclass
class SoftiBridgeFilePaths:
    base: Path
    inbox: Path
    outbox: Path
    state: Path
    queue_mt4: Path
    queue_mt5: Path
    events: Path


def resolve_bridge_paths() -> SoftiBridgeFilePaths:
    settings = get_settings()
    if not settings.softibridge_file_bridge_base:
        # default local folder inside backend for testing
        base = Path.cwd() / "softibridge_runtime"
    else:
        base = Path(settings.softibridge_file_bridge_base).expanduser()
    inbox = base / "inbox"
    outbox = base / "outbox"
    state = base / "state"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    queue_mt4 = inbox / "cmd_queue.txt"
    queue_mt5 = inbox / "cmd_queue_mt5.txt"
    events = outbox / "events.txt"
    for p in (queue_mt4, queue_mt5, events):
        _ensure_utf8(p)
    return SoftiBridgeFilePaths(base, inbox, outbox, state, queue_mt4, queue_mt5, events)


def enqueue_command(payload: dict[str, Any], *, write_mt4: bool = True, write_mt5: bool = True) -> dict[str, Any]:
    # Ensure id and ts are always present — the EA skips lines without a valid id
    payload = dict(payload)
    if not payload.get("id"):
        payload["id"] = f"SIG-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    if not payload.get("ts"):
        payload["ts"] = int(time.time())
    line = format_cmd_line(payload)
    # In multi-tenant prod il file è scritto dal sidecar sul VPS client. Il backend
    # scrive il file solo se SOFTIBRIDGE_BRIDGE_FILE_LEGACY=true (dev/single-tenant).
    if not get_settings().softibridge_bridge_file_legacy:
        return {"line": line, "queue_mt4": None, "queue_mt5": None, "file_legacy": False}
    paths = resolve_bridge_paths()
    if write_mt4:
        safe_write_queue_replace0(paths.queue_mt4, line)
    if write_mt5:
        safe_write_queue_replace0(paths.queue_mt5, line)
    return {"line": line, "queue_mt4": str(paths.queue_mt4), "queue_mt5": str(paths.queue_mt5), "file_legacy": True}


def enqueue_control_command(
    *,
    action: str,
    symbol: str | None = None,
    side_filter: str | None = None,
    ticket: int | None = None,
    sl_price: float | None = None,
    tp_price: float | None = None,
    move_sl_pips: int | None = None,
    comment: str = "SoftiBridge-Web",
    write_mt4: bool = True,
    write_mt5: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, str]:
    payload: dict[str, Any] = {
        "id": f"CTRL-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}",
        "ts": int(time.time()),
        "mode": "CTRL",
        "action": (action or "").upper(),
        "symbol": symbol or "CURRENT",
        "comment": comment,
    }
    if side_filter:
        payload["filter"] = side_filter.upper()
    if ticket is not None:
        payload["ticket"] = int(ticket)
    if sl_price is not None:
        payload["sl_price"] = sl_price
    if tp_price is not None:
        payload["tp_price"] = tp_price
    if move_sl_pips is not None:
        payload["move_sl_pips"] = int(move_sl_pips)
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    return enqueue_command(payload, write_mt4=write_mt4, write_mt5=write_mt5)


def read_recent_events(limit: int = 100) -> list[dict[str, str]]:
    paths = resolve_bridge_paths()
    if not paths.events.exists():
        return []
    try:
        txt = paths.events.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    return [parse_kv(ln) for ln in lines[-limit:]]


def read_recent_results(limit: int = 100) -> list[dict[str, str]]:
    paths = resolve_bridge_paths()
    files = sorted(paths.outbox.glob("res_*.txt"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    out: list[dict[str, str]] = []
    for fp in files[:limit]:
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        kv = parse_kv(content)
        kv["_file"] = fp.name
        out.append(kv)
    return out


def _read_kv_lines_file(path: Path, limit: int = 500) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    return [parse_kv(ln) for ln in lines[-limit:]]


def read_state_snapshot() -> dict[str, Any]:
    paths = resolve_bridge_paths()
    files = {
        "mt4_positions": paths.state / "positions_mt4.txt",
        "mt4_pending": paths.state / "pending_mt4.txt",
        "mt5_positions": paths.state / "positions_mt5.txt",
        "mt5_pending": paths.state / "pending_mt5.txt",
        "summary": paths.state / "bridge_state_summary.txt",
    }
    out = {
        "positions": {
            "mt4": _read_kv_lines_file(files["mt4_positions"]),
            "mt5": _read_kv_lines_file(files["mt5_positions"]),
        },
        "pending": {
            "mt4": _read_kv_lines_file(files["mt4_pending"]),
            "mt5": _read_kv_lines_file(files["mt5_pending"]),
        },
        "summary": parse_kv(files["summary"].read_text(encoding="utf-8", errors="replace")) if files["summary"].exists() else {},
        "files": {k: {"path": str(v), "exists": v.exists(), "size": (v.stat().st_size if v.exists() else 0)} for k, v in files.items()},
    }
    return out


def write_result_file(*, cmd_id: str, status: str, msg: str) -> str:
    paths = resolve_bridge_paths()
    fname = paths.outbox / f"res_{cmd_id}.txt"
    fname.write_text(f"id={cmd_id}\nstatus={status}\nmsg={msg}\n", encoding="utf-8")
    return str(fname)


def append_event_line(kv: dict[str, Any]) -> str:
    paths = resolve_bridge_paths()
    base = {"ts": int(time.time())}
    base.update({k: v for k, v in kv.items() if v is not None})
    line = ";".join([f"{k}={v}" for k, v in base.items()])
    with paths.events.open("a", encoding="utf-8") as f:
        f.write(("" if paths.events.stat().st_size == 0 else "\n") + line)
    return line


def bridge_status() -> dict[str, Any]:
    paths = resolve_bridge_paths()
    state = read_state_snapshot()
    return {
        "base": str(paths.base),
        "inbox": str(paths.inbox),
        "outbox": str(paths.outbox),
        "queue_mt4": {"path": str(paths.queue_mt4), "exists": paths.queue_mt4.exists(), "size": paths.queue_mt4.stat().st_size if paths.queue_mt4.exists() else 0},
        "queue_mt5": {"path": str(paths.queue_mt5), "exists": paths.queue_mt5.exists(), "size": paths.queue_mt5.stat().st_size if paths.queue_mt5.exists() else 0},
        "events": {"path": str(paths.events), "exists": paths.events.exists(), "size": paths.events.stat().st_size if paths.events.exists() else 0},
        "recent_events": read_recent_events(limit=10),
        "recent_results": read_recent_results(limit=10),
        "state_files": state.get("files", {}),
    }
