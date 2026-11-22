# Deploy — Lead Outreach CRM on the CRM server

Target: any small Linux box with Python 3 (Docker optional), with a
Cloudflare tunnel (or another HTTPS reverse proxy) in front. Deploy as the `leadcrm` user below,
or any account you prefer — just keep the paths consistent.

## 1. Install the app

```bash
sudo useradd -r -s /usr/sbin/nologin leadcrm   # skip if the user exists
sudo mkdir -p /opt/lead-crm
sudo cp crm.py .env.example crm.service backup.sh /opt/lead-crm/
sudo cp -r static /opt/lead-crm/
sudo chown -R leadcrm:leadcrm /opt/lead-crm
sudo chmod 600 /opt/lead-crm/.env           # after step 2
```

No packages to install: Python 3 standard library only.

## 2. Configure

```bash
cd /opt/lead-crm
cp .env.example .env
nano .env
```

Set at minimum:

```
ADMIN_PASSWORD=<a strong password — this is the operator's login>
PUBLIC_BASE_URL=https://crm.example.com
```

Leave `HOST=127.0.0.1` and `PORT=8080` as they are — the
Cloudflare tunnel is the front door. Optional:
`VOIPMS_WEBHOOK_SECRET=<random string>` — if set, the VoIP.ms
callback URL must end in `?secret=<same string>`.

## 3. Start as a systemd service (preferred)

```bash
sudo cp crm.service /etc/systemd/system/crm.service
sudo systemctl daemon-reload
sudo systemctl enable --now crm
sudo systemctl status crm
sudo journalctl -u crm -f
```

Check it answers locally (from the box):

```bash
curl -s http://127.0.0.1:8080/healthz
# {"ok": true, "version": "1.0.0"}
```

### Container alternative

```bash
cd /opt/lead-crm
docker compose up -d --build
docker compose logs -f
```

The database lives in the `crm-data` volume. The systemd service
is preferred — one less moving part, and the box already runs
services this way.

## 4. Cloudflare tunnel — add crm.example.com

In the tunnel's `config.yml` , add an ingress entry **above** the
catch-all:

```yaml
ingress:
  - hostname: crm.example.com
    service: http://127.0.0.1:8080
  # ... existing entries ...
  - service: http_status:404
```

Then create the DNS record and reload the tunnel:

```bash
cloudflared tunnel route dns <tunnel-name> crm.example.com
sudo systemctl restart cloudflared   # or: cloudflared tunnel reload
curl -s https://crm.example.com/healthz
```

## 5. WAF skip rule — let Twilio's webhook posts through

Cloudflare's managed rules can challenge Twilio's webhook POSTs.
Add a WAF custom rule (Dashboard → the zone → Security → WAF →
Custom rules → Create rule):

- **Name:** Allow Twilio CRM webhooks
- **Expression:**
  `(http.host eq "crm.example.com" and http.request.uri.path eq "/webhooks/twilio")`
- **Action:** Skip — all remaining custom rules + all managed rules.
  Place it above any rule that would challenge or block.

If Cloudflare ever challenges the VoIP.ms callback too, add the
same rule for path `/webhooks/voipms`.

## 6. First sign-in and provider credentials (the operator does this)

1. Open https://crm.example.com and sign in with
   `ADMIN_PASSWORD`.
2. Settings → pick the provider and enter its credentials:
   - **Twilio** (the working path right now): Account SID, auth
     token, and the number +1 555 555 0100. Then **Test
     connection**.
   - **VoIP.ms:** email, API password, DID. The server's public IP
     must be whitelisted in the VoIP.ms portal (Main Menu → SOAP
     and REST/JSON API → whitelist), and the API must be enabled
     with an API password set there.
3. Send one real test text from the Leads tab and confirm it lands.

## 7. Point the webhooks at the CRM

- **Twilio Console → Phone Numbers → +1 555 555 0100 →
  Messaging → "A message comes in":** Webhook,
  `https://crm.example.com/webhooks/twilio`, HTTP POST.
  The CRM validates Twilio's request signature against the auth
  token in Settings, so set `PUBLIC_BASE_URL` in `.env` (step 2)
  or every inbound message will be rejected with 403.
- **VoIP.ms portal → DID Numbers → Manage DIDs → the DID → SMS
  URL Callback:** `https://crm.example.com/webhooks/voipms`
  (append `?secret=<value>` if you set `VOIPMS_WEBHOOK_SECRET`).

## 8. First bot API key

Either in Settings → Bot API keys (enter the bot's name, copy the
key once — it is shown only once), or from the box:

```bash
sudo -u leadcrm python3 /opt/lead-crm/crm.py --create-key "MyBot"
```

Hand the key to the bot's machine directly (never through
the hub or chat). Bots call the CRM with
`Authorization: Bearer <key>`:

- `POST /api/v1/importLeadsCsv` — `{"csv": "...", "importedBy": "mybot"}`
- `GET  /api/v1/listLeads?status=new`
- `POST /api/v1/sendSms` — `{"lead_id": 12, "template_id": 3}` or `{"phone": "...", "body": "..."}`
- `GET  /api/v1/listInbox` — optional `?lead_id=12` for one thread
- `POST /api/v1/recordInboundReply` — `{"phone": "...", "body": "..."}`

Takeover blocks bot sends; the daily cap applies to bot sends
exactly as to UI sends. Every call is logged (Settings → API call
log).

## 9. Backups

The whole CRM is one file, `crm.db`. Back it up nightly:

```bash
sudo mkdir -p /var/backups/lead-crm
sudo cp backup.sh /opt/lead-crm/
sudo chmod +x /opt/lead-crm/backup.sh
# cron (as root): 0 3 * * * /opt/lead-crm/backup.sh
```

The script uses SQLite's hot-backup API (safe while running) and
keeps 14 days. A backup can also be made from Settings → Backup.
**Restore:** stop the service, copy a backup over `crm.db`,
start the service. The file also holds provider credentials —
keep backups somewhere safe, not world-readable.

## 10. Optional: scheduled CSV import

`import_csv_cli.py <file.csv>` imports a CSV straight into the
database (same rules as a UI/API import), so any scheduler that can
drop a file on the server and run one command can feed the CRM.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| Service won't start: "ADMIN_PASSWORD is not set" | Set it in `/opt/lead-crm/.env`, then `systemctl restart crm`. |
| `curl` to 127.0.0.1:8080 works, public URL doesn't | Tunnel ingress entry missing/wrong, or DNS route not created (step 4). |
| Twilio inbound messages get 403 | `PUBLIC_BASE_URL` in `.env` doesn't match the public URL, or the Twilio auth token in Settings is wrong. Check `journalctl -u crm`. |
| Test connection fails (Twilio) | Wrong SID/token, or the account is still on trial with the number not verified. |
| Test connection fails (VoIP.ms) | Account closed, wrong API password, or the server IP isn't whitelisted in the VoIP.ms portal. |
| Sends stay "queued" | No provider configured, or credentials incomplete — fill in Settings. Queued means *not sent*, by design. |
| Bot API returns 401 | Wrong/revoked key, or the header isn't `Authorization: Bearer <key>`. |
| Bot API send returns 409 | Conversation is taken over by the operator, or the lead opted out. |
| Bot API send returns 429 | Daily cap reached. |
