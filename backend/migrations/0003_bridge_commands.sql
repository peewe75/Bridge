-- Fase 1: DB-backed bridge queue for multi-tenant routing.
-- Each command targets a specific license_id; EA/sidecar pulls only its own.

CREATE TABLE IF NOT EXISTS bridge_commands (
    id BIGSERIAL PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    cmd_uid TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL DEFAULT 'BOTH',          -- MT4 / MT5 / BOTH
    cmd_kind TEXT NOT NULL DEFAULT 'SIGNAL',        -- SIGNAL / CONTROL
    payload_kv TEXT NOT NULL,                       -- serialized 'k=v;...' line ready for EA
    status TEXT NOT NULL DEFAULT 'PENDING',         -- PENDING/CONSUMED/FAILED/EXPIRED
    source_chat_id TEXT,
    room_id TEXT REFERENCES signal_rooms(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_bridge_commands_lic_status_id
    ON bridge_commands (license_id, status, id);

CREATE INDEX IF NOT EXISTS idx_bridge_commands_created_at
    ON bridge_commands (created_at DESC);


CREATE TABLE IF NOT EXISTS bridge_results (
    id BIGSERIAL PRIMARY KEY,
    license_id TEXT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    cmd_id BIGINT REFERENCES bridge_commands(id) ON DELETE SET NULL,
    cmd_uid TEXT,
    status TEXT NOT NULL,                           -- OK / ERROR
    msg TEXT,
    ticket TEXT,
    extra JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bridge_results_lic_created
    ON bridge_results (license_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bridge_results_cmd_uid
    ON bridge_results (cmd_uid);
