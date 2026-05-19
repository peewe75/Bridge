# SoftiBridge Sidecar Agent

Daemon leggero che gira sul VPS del client **accanto a MT4/MT5**.
Polla il backend SoftiBridge via HTTPS (con firma HMAC EA), scrive
`cmd_queue.txt` locale per l'EA, poi conferma (ack).

L'EA esistente non viene toccato: continua a leggere il file come oggi.

## Architettura

```
[Backend SoftiBridge cloud]
  │  scrive BridgeCommand in DB (per license_id)
  ▼
[Sidecar.exe sul VPS client]
  │  ogni 2s: POST /api/ea/commands/pull (HMAC firmato)
  │  scrive payload_kv su MQL4/Files/SoftiBridge/inbox/cmd_queue.txt
  │  POST /api/ea/commands/ack
  ▼
[EA su MT4/MT5]
  legge cmd_queue.txt come sempre → esegue ordine
```

## Setup (one-time per VPS client)

1. Copia `SoftiBridge_Sidecar.exe` sul VPS del client.
2. Copia `sidecar_config.json.example` → `sidecar_config.json` accanto all'exe.
3. Compila i campi:
   - `api_base`: URL pubblico del backend (es. `https://api.softibridge.com`)
   - `license_id`: codice licenza del client (es. `SB-A1B2C3D4`)
   - `install_id`: UUID univoco di questo VPS (uguale a quello configurato nell'EA)
   - `account_number`: numero conto MT4/MT5
   - `platform`: `MT4` o `MT5`
   - `ea_hmac_secret`: stesso segreto configurato nell'EA (`EA_HMAC_SECRET` lato backend)
   - `bridge_files_dir`: path della cartella `Files` dell'istanza MetaTrader (es. `C:\Users\<u>\AppData\Roaming\MetaQuotes\Terminal\<id>\MQL4\Files\SoftiBridge`)
4. Avvia: `SoftiBridge_Sidecar.exe`
5. (Opzionale) Schedula come Windows Service o avvio automatico.

## Build .exe

Sul tuo PC sviluppo:

```bat
pip install pyinstaller
cd sidecar_agent
build_sidecar_exe.bat
```

Output: `sidecar_agent\dist\SoftiBridge_Sidecar.exe`

## Test in dry-run (Python)

```bash
cd sidecar_agent
cp sidecar_config.json.example sidecar_config.json
# edita config
python sidecar.py
```

## Note operative

- **Stato**: il sidecar mantiene `.sidecar_since.json` accanto al config per evitare di rileggere comandi già consumati.
- **Backoff**: in caso di errore HTTP/rete, raddoppia il polling fino a 30s.
- **Sicurezza**: tutti i call al backend sono firmati con HMAC-SHA256 sul timestamp (anti-replay max 5min skew).
- **Multi-istanza**: ogni MT (MT4 + MT5 sullo stesso VPS) richiede un'istanza sidecar separata con `platform` diverso e cartella `bridge_files_dir` diversa.
