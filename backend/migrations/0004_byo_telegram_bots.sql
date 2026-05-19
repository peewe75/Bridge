-- Fase 5: BYO Telegram bot — client può portare il proprio bot.

CREATE TABLE IF NOT EXISTS telegram_bots (
    id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL DEFAULT 'CLIENT',         -- PLATFORM | ADMIN_WL | CLIENT
    owner_ref_id TEXT,
    bot_username TEXT,
    bot_token_enc TEXT NOT NULL,                       -- Fernet-encrypted
    webhook_secret TEXT,
    webhook_url TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',             -- ACTIVE/SUSPENDED/REVOKED
    last_check_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telegram_bots_owner
    ON telegram_bots (owner_type, owner_ref_id);

CREATE INDEX IF NOT EXISTS idx_telegram_bots_status
    ON telegram_bots (status);

CREATE INDEX IF NOT EXISTS idx_telegram_bots_username
    ON telegram_bots (bot_username);


-- Link opzionale: una signal_room può essere alimentata da un bot BYO.
-- Se NULL la room è gestita dal bot globale di piattaforma.
ALTER TABLE signal_rooms
    ADD COLUMN IF NOT EXISTS telegram_bot_id TEXT REFERENCES telegram_bots(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_signal_rooms_telegram_bot
    ON signal_rooms (telegram_bot_id);
