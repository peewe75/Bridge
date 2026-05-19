# SoftiBridge — Architettura Multi-Tenant + BYO Bot

Riferimento operativo dopo i refactor Fasi 1-6.

## Componenti

```
┌──────────────────────────────────────────────────────────────┐
│                    BACKEND (1 deploy su tua VPS)             │
│                                                              │
│  Postgres ──┬── BridgeCommand (per license_id)               │
│             ├── BridgeResult                                 │
│             ├── TelegramBot (BYO)                            │
│             └── SignalRoom (telegram_bot_id FK)              │
│                                                              │
│  FastAPI                                                     │
│   ├ POST /api/telegram/webhook          ← bot globale (C)    │
│   ├ POST /api/telegram/webhook/{bot_id} ← bot BYO (B)        │
│   ├ POST /api/ea/commands/pull          ← sidecar HMAC       │
│   └ POST /api/ea/commands/ack           ← sidecar HMAC       │
└────────────┬───────────────────────────────┬─────────────────┘
             │                               │
             │ HTTP poll 2s                  │
             ▼                               ▼
   ┌──────────────────┐             ┌──────────────────┐
   │  Sidecar VPS A   │             │  Sidecar VPS B   │
   │  (license SB-xx) │             │  (license SB-yy) │
   └────────┬─────────┘             └────────┬─────────┘
            │ write cmd_queue.txt            │
            ▼                                ▼
   ┌──────────────────┐             ┌──────────────────┐
   │   EA su MT4/5    │             │   EA su MT4/5    │
   └──────────────────┘             └──────────────────┘
```

## Variabili `.env` chiave

```
# Bridge file-write lato backend (false in prod multi-tenant)
SOFTIBRIDGE_BRIDGE_FILE_LEGACY=false

# BYO bot
TELEGRAM_WEBHOOK_BASE_URL=https://api.softibridge.com
TELEGRAM_TOKEN_ENC_KEY=<Fernet key 32-byte b64>

# Bot globale piattaforma (modello C)
TELEGRAM_BOT_TOKEN=<token bot SoftiBridge>
TELEGRAM_WEBHOOK_SECRET=<random 32 chars>

# EA HMAC condiviso con sidecar/EA esistenti
EA_HMAC_SECRET=<random 64 chars in prod>
```

Genera `TELEGRAM_TOKEN_ENC_KEY`:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Setup nuovo client (procedura tipica)

1. **Onboarding**: Stripe checkout → backend crea `Client + License`
2. **Attivazione Telegram**: dashboard → genera `SB-ACT-xxxx` → client lo manda al bot globale (modello C)
3. **Configurazione MT account**: dashboard → salva `mt4_account`/`mt5_account`
4. **Setup VPS**:
   - Client installa MT4/MT5 sulla sua VPS
   - Installa l'EA SoftiBridge (file `.ex4`/`.ex5`) con `license_id`, `install_id` UUID, `EA_HMAC_SECRET` come oggi
   - Installa `SoftiBridge_Sidecar.exe` accanto a MT
   - Compila `sidecar_config.json` (vedi `sidecar_agent/README.md`)
   - Avvia sidecar (manuale o servizio Windows)
5. **Signal room**:
   - Modello C: SuperAdmin/AdminWL aggiunge bot globale ai gruppi segnali, crea `SignalRoom { source_chat_id, telegram_bot_id=NULL }`, lega clients via `Client.signal_room_id`
   - Modello B: client crea proprio bot via @BotFather, `POST /api/client/telegram-bot {bot_token}`, backend verifica + setWebhook su URL `/api/telegram/webhook/{bot_id}`, crea `SignalRoom { source_chat_id, telegram_bot_id=<bot.id> }`

## Flusso runtime segnale (multi-tenant)

```
1. Bot (C globale o B BYO) riceve update Telegram da gruppo segnali
2. /api/telegram/webhook[/{bot_id}] → _process_update(payload, bot)
3. Lookup SignalRoom per source_chat_id E (telegram_bot_id IS NULL | == bot.id)
4. parse_signal → ParseOutcome
5. Se confidence ok + valid_logic + signals_enabled (system control):
     valid_clients = client della room con License runtime-valid
     Per ogni valid client:
       INSERT bridge_commands (license_id, payload_kv, status=PENDING)
6. Sidecar sul VPS di ogni client polla /api/ea/commands/pull (HMAC EA)
7. Sidecar scrive payload_kv in cmd_queue.txt (formato già supportato dall'EA esistente)
8. EA legge file → apre ordine su MT
9. Sidecar ACK → BridgeCommand.status=CONSUMED + BridgeResult
```

## Limiti BYO bot per piano

| Piano | Bot BYO max |
|---|---|
| BASIC | 1 |
| PRO | 3 |
| ENTERPRISE | 10 |

Override via `Plan.feature_flags.telegram_bots` (intero).

## Migrations da applicare

```
0001_initial.sql               (esistente)
0002_clerk_license_replacement.sql (esistente)
0003_bridge_commands.sql        (Fase 1)
0004_byo_telegram_bots.sql      (Fase 5)
```

In dev `Base.metadata.create_all` su startup gestisce tutto. In prod applicare le SQL manualmente o via tool migrations (Alembic non ancora wired).

## Smoke test end-to-end

```bash
# 1. Backend up
uvicorn app.main:app --reload

# 2. Crea licenza dev
# (via /api/admin/licenses o seed)

# 3. Test pull (sostituisci LIC/INSTALL/HMAC/TS)
python -c "
import hashlib, hmac, json, time, urllib.request
LIC='SB-TEST0001'; INST='vps-dev'; ACC='12345'; PLAT='MT4'
SECRET='change-me-ea'; ts=int(time.time())
msg=f'{LIC}|{INST}|{ACC}|{PLAT}|{ts}'.encode()
sig=hmac.new(SECRET.encode(), msg, hashlib.sha256).hexdigest()
body=json.dumps({'license_id':LIC,'install_id':INST,'account_number':ACC,'platform':PLAT,'timestamp':ts,'signature':sig,'since_id':0,'limit':50}).encode()
req=urllib.request.Request('http://localhost:8000/api/ea/commands/pull', data=body, method='POST')
req.add_header('Content-Type','application/json')
print(urllib.request.urlopen(req).read().decode())
"

# 4. Trigger un signal ingest manuale
# POST /api/signals/ingest {text: 'BUY GOLD 2645-2650 SL 2630 TP 2670', license_ids: ['SB-TEST0001']}

# 5. Re-run pull → vedi command nel response
# 6. Ack con POST /api/ea/commands/ack
```

## Note sicurezza

- **HMAC EA secret**: stesso tra backend (`EA_HMAC_SECRET`), EA installato, e sidecar (`ea_hmac_secret` in `sidecar_config.json`).
- **Token bot BYO**: cifrati Fernet at-rest. Decifrati solo per chiamare `setWebhook`/`deleteWebhook`. Non loggati mai.
- **Webhook secret per bot BYO**: generato random 32 byte per ogni bot, salvato in `TelegramBot.webhook_secret`, validato in header `X-Telegram-Bot-Api-Secret-Token`.
- **Replay attack**: signature EA ha skew max 5 min sul timestamp.
- **Cmd isolation**: pull filtra per `license_id` da signature verificata; sidecar non può vedere comandi di altri.
