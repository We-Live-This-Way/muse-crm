# Lead Outreach CRM

A small self-hosted CRM for text-message lead outreach. AI sales bots on OpenMausBot or Rakazo — or any agent that can make an HTTP call or use MCP — can find and import leads with their own API key (`POST /api/v1/importLeadsCsv` and `GET /api/v1/listLeads`). Reps then work that list from the phone with [WLTW Dialer](https://github.com/We-Live-This-Way/muse-dialer).

One Python file, one SQLite database, no packages to install.

## Features

- **Pipeline board:** New → Texted → Replied → Taken over → Closed / Not qualified / Do not contact.
- **One inbox** for inbound replies, with STOP handling (auto opt-out) and human takeover, which blocks automated sends.
- **CSV import** with flexible headers and dedupe by phone; an **address** field on each lead (CSV, web UI, search, and manual add); campaigns; templates with `{name}`, `{business}` and `{city}`; bulk sends; call dispositions; a daily send cap (default 100); notifications.
- **Texting providers:** Twilio and VoIP.ms, set up in the web UI under Settings. With no provider configured, sends are recorded as *queued* and never faked as sent.
- **Bot API** with per-bot keys (`Authorization: Bearer <key>`), for OpenMausBot (MCP), Rakazo, or any agent that can make an HTTP call: import leads (`POST /api/v1/importLeadsCsv`), list leads, update a lead's business / contact name / address (`POST /api/v1/updateLead`), send texts, read the inbox, record calls. Every call is logged.
- **Webhooks:** `POST /webhooks/twilio` (with signature validation) and `POST /webhooks/voipms`.
- `attention.py`: a read-only daily digest of replies waiting, leads gone quiet and leads never texted.

## Quick start

```bash
cp .env.example .env      # set ADMIN_PASSWORD
python3 crm.py            # open http://127.0.0.1:8080
python3 test_crm.py       # optional: run the test suite
```

Bot API keys from the command line:

```bash
python3 crm.py --create-key "MyBot"   # prints the key once
python3 crm.py --list-keys
python3 crm.py --revoke-key crm_live_abc
```

For a server install (systemd or Docker, HTTPS tunnel, webhooks, backups), see [DEPLOY.md](DEPLOY.md).

## Security notes

- Binds to `127.0.0.1` by default. Put an HTTPS tunnel or reverse proxy in front.
- Provider credentials and API key hashes are stored in `crm.db`. Keep it `chmod 600` and back it up.
- Never commit `.env` or the database.

## License

MIT, see [LICENSE](LICENSE).
