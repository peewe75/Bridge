from __future__ import annotations

from typing import Any


SYSTEM_CONTROL_STATE: dict[str, Any] = {
    "mode": "NORMAL",  # NORMAL | MAINTENANCE | FROZEN | SHUTDOWN
    "billing_enabled": True,
    "signals_enabled": True,
    "ea_bridge_enabled": True,
    "client_access_enabled": True,
    "updated_at": None,
    "last_action": None,
    "last_reason": None,
}


def snapshot() -> dict[str, Any]:
    return {
        "mode": SYSTEM_CONTROL_STATE.get("mode", "NORMAL"),
        "billing_enabled": bool(SYSTEM_CONTROL_STATE.get("billing_enabled", True)),
        "signals_enabled": bool(SYSTEM_CONTROL_STATE.get("signals_enabled", True)),
        "ea_bridge_enabled": bool(SYSTEM_CONTROL_STATE.get("ea_bridge_enabled", True)),
        "client_access_enabled": bool(SYSTEM_CONTROL_STATE.get("client_access_enabled", True)),
        "updated_at": SYSTEM_CONTROL_STATE.get("updated_at"),
        "last_action": SYSTEM_CONTROL_STATE.get("last_action"),
        "last_reason": SYSTEM_CONTROL_STATE.get("last_reason"),
    }


def signals_enabled() -> bool:
    return bool(SYSTEM_CONTROL_STATE.get("signals_enabled", True))


def ea_bridge_enabled() -> bool:
    return bool(SYSTEM_CONTROL_STATE.get("ea_bridge_enabled", True))


def billing_enabled() -> bool:
    return bool(SYSTEM_CONTROL_STATE.get("billing_enabled", True))


def client_access_enabled() -> bool:
    return bool(SYSTEM_CONTROL_STATE.get("client_access_enabled", True))
