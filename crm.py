#!/usr/bin/env python3
"""
Lead Outreach CRM — self-hosted edition (We Live This Way).

A standalone port of the Muse-hosted Lead Outreach CRM, with no
Muse-platform dependencies.  Python 3 standard library only.

  python3 crm.py [--create-key NAME] [--list-keys] [--revoke-key PREFIX]

Config comes from the environment and an optional .env file next to
this script (see .env.example):

  ADMIN_PASSWORD      required — password for the web UI (the operator's login)
  HOST                bind address, default 127.0.0.1
  PORT                default 8080
  CRM_DB              SQLite file, default ./crm.db
  PUBLIC_BASE_URL     e.g. https://crm.example.com — used to validate
                      Twilio webhook signatures behind the tunnel
  VOIPMS_WEBHOOK_SECRET  optional shared secret for /webhooks/voipms

Features: CSV import (flexible headers, dedupe by phone), pipeline
New → Texted → Replied → Taken over → Closed / Not qualified /
Do not contact,
provider-agnostic texting (VoIP.ms first, Twilio second), one inbox
for inbound replies with STOP handling and takeover, templates,
bulk sends, call dispositions, daily cap (default 100), notifications,
a keyed bot API for your bots, and real public webhooks for Twilio
and VoIP.ms.

Honesty rule, built in: with no provider configured, a send is
recorded as QUEUED and the lead is NOT advanced — never faked
as sent.

The app never logs secrets, tokens, or message bodies.  Provider
credentials live in the SQLite file; protect it (chmod 600) and
back it up (see DEPLOY.md).
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"

# ---------------------------------------------------------------- config

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    path = os.path.join(APP_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("CRM_DB", os.path.join(APP_DIR, "crm.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
VOIPMS_WEBHOOK_SECRET = os.environ.get("VOIPMS_WEBHOOK_SECRET", "")

STATUSES = ["new", "texted", "replied", "taken_over", "closed", "not_qualified", "do_not_contact"]
STATUS_LABELS = {
    "new": "New",
    "texted": "Texted",
    "replied": "Replied",
    "taken_over": "Taken over",
    "closed": "Closed",
    "not_qualified": "Not qualified",
    "do_not_contact": "Do not contact",
}

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}

# ---------------------------------------------------------------- db

_db_lock = threading.RLock()
_db = None


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA foreign_keys=ON")
        init_schema(_db)
    return _db


def init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS leads (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT DEFAULT '',
      business TEXT DEFAULT '',
      phone TEXT DEFAULT '',
      email TEXT DEFAULT '',
      website TEXT DEFAULT '',
      address TEXT DEFAULT '',
      city TEXT DEFAULT '',
      campaign TEXT DEFAULT '',
      source TEXT DEFAULT '',
      notes TEXT DEFAULT '',
      status TEXT DEFAULT 'new',
      prev_status TEXT,
      opted_out INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
      updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
      last_texted_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
    CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);

    CREATE TABLE IF NOT EXISTS messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      lead_id INTEGER REFERENCES leads(id),
      direction TEXT,
      body TEXT,
      status TEXT,
      provider TEXT,
      provider_sid TEXT,
      error TEXT,
      created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );
    CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);

    CREATE TABLE IF NOT EXISTS calls (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      lead_id INTEGER REFERENCES leads(id),
      outcome TEXT,
      note TEXT DEFAULT '',
      called_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
      actor TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(lead_id);

    CREATE TABLE IF NOT EXISTS templates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE,
      body TEXT,
      created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );

    CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value TEXT
    );

    CREATE TABLE IF NOT EXISTS notifications (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT,
      text TEXT,
      lead_id INTEGER,
      is_read INTEGER DEFAULT 0,
      created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );

    CREATE TABLE IF NOT EXISTS import_batches (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      filename TEXT,
      imported_by TEXT,
      total_rows INTEGER,
      imported INTEGER,
      skipped_dupes INTEGER,
      invalid INTEGER,
      created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );

    CREATE TABLE IF NOT EXISTS api_keys (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT,
      key_hash TEXT UNIQUE,
      key_prefix TEXT,
      created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
      last_used_at TEXT
    );

    CREATE TABLE IF NOT EXISTS api_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      key_id INTEGER,
      endpoint TEXT,
      method TEXT,
      status INTEGER,
      created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );

    CREATE TABLE IF NOT EXISTS sessions (
      token TEXT PRIMARY KEY,
      created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
      expires_at TEXT
    );
    """)
    # Migration: databases created before the website column existed gain
    # it on startup, so existing installs keep working.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(leads)").fetchall()]
    if "website" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN website TEXT DEFAULT ''")
    if "address" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN address TEXT DEFAULT ''")
    if "campaign" not in cols:
        conn.execute("ALTER TABLE leads ADD COLUMN campaign TEXT DEFAULT ''")
    conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('provider','voipms')")
    conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES('daily_cap','100')")
    conn.commit()


def get_setting(key, default=""):
    with _db_lock:
        row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with _db_lock:
        db().execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        db().commit()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- utils

def normalize_phone(raw):
    """Normalize to 10 US digits, or '' when unusable."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def format_phone(digits):
    if len(digits) == 10:
        return "(%s) %s-%s" % (digits[0:3], digits[3:6], digits[6:10])
    return digits


def is_stop_message(body):
    if not body:
        return False
    first = re.split(r"[\s,.!]+", body.strip().upper(), 1)[0]
    return first in STOP_WORDS


def render_template(body, lead):
    name = (lead["name"] or "").strip()
    repl = {
        "{name}": name,
        "{first_name}": name.split()[0] if name else "",
        "{business}": lead["business"] or "",
        "{city}": lead["city"] or "",
    }
    out = body or ""
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def lead_dict(row):
    d = dict(row)
    d["phone_display"] = format_phone(d.get("phone") or "")
    d["status_label"] = STATUS_LABELS.get(d.get("status"), d.get("status"))
    return d


def notify(kind, text, lead_id=None):
    with _db_lock:
        db().execute(
            "INSERT INTO notifications(kind, text, lead_id) VALUES(?,?,?)",
            (kind, text, lead_id),
        )
        db().commit()

# ---------------------------------------------------------------- providers

class ProviderError(Exception):
    pass


def _http_get_json(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", "WLTW-CRM/%s" % VERSION)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise ProviderError("HTTP %s: %s" % (e.code, body[:300]))
    except Exception as e:
        raise ProviderError(str(e))


def _http_post_form(url, fields, auth=None, timeout=30):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "WLTW-CRM/%s" % VERSION)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(
            auth.encode("utf-8")).decode("ascii"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        msg = body[:300]
        try:
            msg = json.loads(body).get("message", msg)
        except Exception:
            pass
        raise ProviderError("HTTP %s: %s" % (e.code, msg))
    except Exception as e:
        raise ProviderError(str(e))


def voipms_call(method, extra):
    """Call the VoIP.ms REST API. Raises ProviderError on failure."""
    email = get_setting("voipms_email")
    password = get_setting("voipms_api_password")
    if not email or not password:
        raise ProviderError("VoIP.ms is not configured (email / API password missing).")
    params = {
        "api_username": email,
        "api_password": password,
        "method": method,
        "content_type": "json",
    }
    params.update(extra)
    url = "https://voip.ms/api/v1/rest.php?" + urllib.parse.urlencode(params)
    data = _http_get_json(url)
    if isinstance(data, dict) and data.get("status") == "error":
        raise ProviderError(str(data.get("error", "VoIP.ms error")))
    return data


def voipms_send(did, dst, message):
    """Send an SMS via VoIP.ms. Returns the provider reference id."""
    if len(message) > 2048:
        raise ProviderError("Message over 2048 characters is too long for SMS.")
    # sendSMS caps at 160 chars; sendMMS carries longer plain text.
    method = "sendSMS" if len(message) <= 160 else "sendMMS"
    data = voipms_call(method, {"did": did, "dst": dst, "message": message})
    ref = None
    if isinstance(data, dict):
        ref = data.get("sms") or data.get("id") or data.get("message_id")
    return str(ref) if ref else "voipms-%s" % int(time.time())


def voipms_test():
    data = voipms_call("getBalance", {})
    bal = data.get("balance") if isinstance(data, dict) else None
    return {"ok": True, "detail": "VoIP.ms reachable%s" % (
        (" — balance: %s %s" % (bal.get("current", "?"), bal.get("currency", "")))
        if isinstance(bal, dict) else "")}


def voipms_sync_inbox(did, days=3):
    """Pull recent inbound SMS from VoIP.ms into the inbox."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days)
    data = voipms_call("getSMS", {
        "did": did,
        "from": start.isoformat(),
        "to": end.isoformat(),
    })
    items = []
    if isinstance(data, dict):
        items = data.get("sms") or []
    imported = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        # Only inbound; if the record has no type marker, treat it as inbound.
        typ = str(item.get("type", "")).lower()
        if typ and typ not in ("1", "in", "inbound", "received"):
            continue
        sender = item.get("from") or item.get("contact") or ""
        body = item.get("message") or ""
        when = item.get("date") or ""
        if not sender or not body:
            continue
        phone = normalize_phone(sender)
        if not phone:
            continue
        with _db_lock:
            lead = db().execute(
                "SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
            if not lead:
                continue
            dup = db().execute(
                "SELECT id FROM messages WHERE lead_id=? AND direction='in' "
                "AND body=? AND substr(created_at,1,16)=substr(?,1,16)",
                (lead["id"], body, when)).fetchone()
            if dup:
                continue
        process_inbound(lead["id"], body, provider="voipms")
        imported += 1
    return {"ok": True, "imported": imported, "checked": len(items)}


def twilio_send(sid, token, from_number, to_number, message):
    url = ("https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json"
           % urllib.parse.quote(sid, safe=""))
    data = _http_post_form(url, {
        "From": from_number,
        "To": "+1" + to_number,
        "Body": message,
    }, auth="%s:%s" % (sid, token))
    if not isinstance(data, dict) or not data.get("sid"):
        raise ProviderError("Twilio did not return a message sid.")
    return data["sid"]


def twilio_test():
    sid = get_setting("twilio_sid")
    token = get_setting("twilio_token")
    if not sid or not token:
        raise ProviderError("Twilio is not configured (SID / auth token missing).")
    url = ("https://api.twilio.com/2010-04-01/Accounts/%s.json"
           % urllib.parse.quote(sid, safe=""))
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Basic " + base64.b64encode(
        ("%s:%s" % (sid, token)).encode("utf-8")).decode("ascii"))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise ProviderError("Twilio HTTP %s" % e.code)
    except Exception as e:
        raise ProviderError(str(e))
    return {"ok": True, "detail": "Twilio reachable — account %s (%s)" % (
        data.get("friendly_name", sid), data.get("status", "?"))}


# ---------------------------------------------------------------- core logic

def provider_configured():
    provider = get_setting("provider", "voipms")
    if provider == "twilio":
        return bool(get_setting("twilio_sid") and get_setting("twilio_token")
                    and get_setting("twilio_number"))
    return bool(get_setting("voipms_email") and get_setting("voipms_api_password")
                and get_setting("voipms_did"))


def todays_send_count():
    with _db_lock:
        row = db().execute(
            "SELECT COUNT(*) c FROM messages WHERE direction='out' "
            "AND status IN ('sent','queued') AND date(created_at)=date('now')"
        ).fetchone()
    return row["c"]


def daily_cap():
    try:
        return int(get_setting("daily_cap", "100"))
    except ValueError:
        return 100


def send_to_lead(lead, body, actor="ui"):
    """Send one SMS to a lead. Returns (message_row, error_or_None).

    actor 'ui' = the operator in the web UI; 'api' = a bot via the API key.
    Takeover blocks api sends; opt-out blocks both. With no provider
    configured the message is recorded as QUEUED and the lead is NOT
    advanced — never faked as sent.
    """
    if lead["opted_out"]:
        return None, "Lead has opted out (Do not contact)."
    if actor == "api" and lead["status"] == "taken_over":
        return None, "Conversation is taken over by the operator — agent sends are blocked."
    if todays_send_count() >= daily_cap():
        return None, "Daily cap reached (%d sends today)." % daily_cap()
    phone = lead["phone"]
    if not phone:
        return None, "Lead has no phone number."

    provider = get_setting("provider", "voipms")
    status, sid, error = "queued", None, None
    if provider_configured():
        try:
            if provider == "twilio":
                sid = twilio_send(get_setting("twilio_sid"),
                                  get_setting("twilio_token"),
                                  get_setting("twilio_number"), phone, body)
            else:
                sid = voipms_send(get_setting("voipms_did"), phone, body)
            status = "sent"
        except ProviderError as e:
            status, error = "failed", str(e)

    with _db_lock:
        conn = db()
        cur = conn.execute(
            "INSERT INTO messages(lead_id, direction, body, status, provider, "
            "provider_sid, error) VALUES(?, 'out', ?, ?, ?, ?, ?)",
            (lead["id"], body, status, provider, sid, error))
        msg_id = cur.lastrowid
        if status == "sent":
            conn.execute(
                "UPDATE leads SET last_texted_at=?, updated_at=?, "
                "status=CASE WHEN status='new' THEN 'texted' ELSE status END "
                "WHERE id=?", (now_iso(), now_iso(), lead["id"]))
        conn.commit()
        row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    return dict(row), (None if status != "failed" else error)


def process_inbound(lead_id, body, provider="webhook"):
    """Record an inbound reply; applies STOP and reply rules. Returns the lead."""
    with _db_lock:
        conn = db()
        conn.execute(
            "INSERT INTO messages(lead_id, direction, body, status, provider) "
            "VALUES(?, 'in', ?, 'received', ?)", (lead_id, body, provider))
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        name = (lead["name"] or lead["business"] or lead["phone"] or "Lead").strip()
        if is_stop_message(body):
            conn.execute(
                "UPDATE leads SET status='do_not_contact', opted_out=1, "
                "updated_at=? WHERE id=?", (now_iso(), lead_id))
            conn.commit()
            notify("stop", "%s replied STOP — marked Do not contact." % name, lead_id)
        else:
            if lead["status"] != "taken_over":
                conn.execute(
                    "UPDATE leads SET status='replied', updated_at=? WHERE id=?",
                    (now_iso(), lead_id))
            conn.commit()
            notify("reply", "Reply from %s: %s" % (
                name, body[:120]), lead_id)
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    return dict(lead)


def find_lead_by_phone(phone):
    phone = normalize_phone(phone)
    if not phone:
        return None
    with _db_lock:
        row = db().execute("SELECT * FROM leads WHERE phone=?", (phone,)).fetchone()
    return dict(row) if row else None


CALL_OUTCOMES = ["interested", "not_interested", "no_answer", "voicemail",
                 "callback", "wrong_number", "not_qualified", "do_not_contact",
                 "shift_campaign"]

# Campaigns a lead may be shifted to via the shift_campaign outcome:
# every campaign already present in leads, plus 'restaurant' (the
# new campaign, which has no leads until the first shift lands).
# Create-on-shift: a name that is NOT in that union is still accepted
# if it is a valid NEW campaign name (see is_valid_new_campaign) —
# the shift creates the campaign, and it then appears in
# GET /api/v1/campaigns automatically. Anything else is rejected
# with 400, so a typo or a stale client can't scatter leads into a
# campaign nobody watches.
def is_valid_new_campaign(name):
    """A valid brand-new campaign name: after strip().lower(), one
    lowercase word of letters, numbers and hyphens, 1-30 chars,
    starting and ending with a letter or digit. Examples: 'pharmacy',
    'djs', 'pet-grooming'. Rejects '', 'has space', 'under_score',
    '-lead', 'trail-', and anything over 30 chars."""
    return bool(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?",
                             (name or "").strip().lower()))


def valid_shift_campaigns():
    with _db_lock:
        rows = db().execute(
            "SELECT DISTINCT campaign FROM leads "
            "WHERE campaign<>''").fetchall()
    return sorted({r["campaign"] for r in rows} | {"restaurant"})

# Outcome -> (new status, opted_out). Outcomes missing here leave the
# lead's status unchanged — the call is just recorded.
CALL_STATUS_EFFECTS = {
    "interested": ("taken_over", False),
    "not_interested": ("do_not_contact", False),
    "wrong_number": ("do_not_contact", False),
    "not_qualified": ("not_qualified", False),
    "do_not_contact": ("do_not_contact", True),
}


def record_call(lead_id, outcome, note="", actor="api", to_campaign=None):
    """Record a phone call against a lead; applies the outcome's status
    effect. Returns (lead, call_row). No-answer / voicemail / callback
    leave the status unchanged — the call is just recorded.
    shift_campaign moves the lead to another campaign (to_campaign)
    and leaves status and opted_out untouched — the lead stays new,
    just in its new campaign's queue."""
    with _db_lock:
        conn = db()
        cur = conn.execute(
            "INSERT INTO calls(lead_id, outcome, note, actor) VALUES(?,?,?,?)",
            (lead_id, outcome, note, actor))
        call_id = cur.lastrowid
        if outcome == "shift_campaign":
            conn.execute(
                "UPDATE leads SET campaign=?, updated_at=? WHERE id=?",
                (to_campaign, now_iso(), lead_id))
        else:
            effect = CALL_STATUS_EFFECTS.get(outcome)
            if effect:
                status, opted_out = effect
                if opted_out:
                    conn.execute(
                        "UPDATE leads SET status=?, opted_out=1, updated_at=? "
                        "WHERE id=?", (status, now_iso(), lead_id))
                else:
                    conn.execute(
                        "UPDATE leads SET status=?, updated_at=? WHERE id=?",
                        (status, now_iso(), lead_id))
        conn.commit()
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        call = conn.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
    return dict(lead), dict(call)


# CSV import ---------------------------------------------------------------

HEADER_ALIASES = {
    "name": {"name", "contact name", "contact", "full name", "contact_name"},
    "business": {"business", "business name", "company", "company name",
                 "business_name"},
    "phone": {"phone", "phone number", "mobile", "cell", "tel", "telephone"},
    "email": {"email", "email address", "contact email", "e-mail"},
    "website": {"website", "web site", "url", "site"},
    "address": {"address", "street address", "street"},
    "city": {"city", "town"},
    "campaign": {"campaign"},
    "source": {"source", "generated by", "generated_by", "generatedby",
               "generated by bot"},
    "notes": {"notes", "note", "category", "reason"},
}


def _map_headers(fieldnames):
    mapping = {}
    for raw in (fieldnames or []):
        key = (raw or "").strip().lower()
        for field, aliases in HEADER_ALIASES.items():
            if key in aliases:
                mapping.setdefault(field, raw)
    return mapping


def import_csv_text(csv_text, filename, imported_by):
    """Import CSV text. Returns counts and per-row problems."""
    reader = csv.DictReader(io.StringIO(csv_text))
    mapping = _map_headers(reader.fieldnames)
    total = imported = dupes = invalid = 0
    problems = []
    with _db_lock:
        conn = db()
        for i, row in enumerate(reader, start=2):
            total += 1
            vals = {}
            for field, col in mapping.items():
                vals[field] = (row.get(col) or "").strip()
            # Category + reason both land in notes.
            notes_bits = []
            for col in ("category", "reason", "notes", "note"):
                v = (row.get(col) or "").strip()
                if v:
                    notes_bits.append(v)
            if notes_bits:
                vals["notes"] = "; ".join(notes_bits)
            phone = normalize_phone(vals.get("phone", ""))
            name = vals.get("name", "")
            business = vals.get("business", "")
            if not name and not business and not phone:
                invalid += 1
                problems.append("row %d: empty row, skipped" % i)
                continue
            if vals.get("phone") and not phone:
                invalid += 1
                problems.append("row %d: phone '%s' is not a usable number"
                                % (i, vals.get("phone")))
                continue
            if phone:
                existing = conn.execute(
                    "SELECT id FROM leads WHERE phone=?", (phone,)).fetchone()
                if existing:
                    dupes += 1
                    continue
            conn.execute(
                "INSERT INTO leads(name, business, phone, email, website, address, city, source, notes, campaign) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, business, phone, vals.get("email", ""), vals.get("website", ""),
                 vals.get("address", ""),
                 vals.get("city", ""),
                 vals.get("source", "") or imported_by, vals.get("notes", ""),
                 vals.get("campaign", "") or "delivery"))
            imported += 1
        conn.execute(
            "INSERT INTO import_batches(filename, imported_by, total_rows, imported, "
            "skipped_dupes, invalid) VALUES(?,?,?,?,?,?)",
            (filename, imported_by, total, imported, dupes, invalid))
        conn.commit()
    return {"total_rows": total, "imported": imported,
            "skipped_dupes": dupes, "invalid": invalid,
            "problems": problems[:20]}

# ---------------------------------------------------------------- HTTP layer

SESSION_DAYS = 7
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css",
        ".js": "application/javascript", ".png": "image/png",
        ".jpg": "image/jpeg", ".svg": "image/svg+xml"}


def _hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def create_api_key(name):
    raw = "crm_live_" + secrets.token_urlsafe(24)
    prefix = raw[:12]
    with _db_lock:
        db().execute(
            "INSERT INTO api_keys(name, key_hash, key_prefix) VALUES(?,?,?)",
            (name, _hash_key(raw), prefix))
        db().commit()
    return raw


def auth_api_key(handler):
    """Return the api_keys row for a Bearer/X-Api-Key header, or None."""
    auth = handler.headers.get("Authorization", "")
    key = None
    if auth.startswith("Bearer "):
        key = auth[7:].strip()
    elif handler.headers.get("X-Api-Key"):
        key = handler.headers.get("X-Api-Key").strip()
    if not key:
        return None
    with _db_lock:
        row = db().execute(
            "SELECT * FROM api_keys WHERE key_hash=?", (_hash_key(key),)).fetchone()
        if row:
            db().execute("UPDATE api_keys SET last_used_at=? WHERE id=?",
                         (now_iso(), row["id"]))
            db().commit()
    return dict(row) if row else None


def log_api_call(key_id, endpoint, method, status):
    with _db_lock:
        db().execute(
            "INSERT INTO api_log(key_id, endpoint, method, status) VALUES(?,?,?,?)",
            (key_id, endpoint, method, status))
        db().commit()


def create_session():
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _db_lock:
        db().execute("INSERT INTO sessions(token, expires_at) VALUES(?,?)",
                     (token, expires))
        db().commit()
    return token


def valid_session(handler):
    cookie = handler.headers.get("Cookie", "")
    m = re.search(r"crm_session=([^;]+)", cookie)
    if not m:
        return False
    with _db_lock:
        row = db().execute(
            "SELECT * FROM sessions WHERE token=? AND expires_at > ?",
            (m.group(1), now_iso())).fetchone()
    return bool(row)


def parse_multipart(body, content_type):
    m = re.search(r"boundary=([^;]+)", content_type or "")
    if not m:
        return {}
    boundary = m.group(1).strip().strip('"')
    parts = {}
    for seg in body.split(("--" + boundary).encode())[1:]:
        if seg.startswith(b"--"):
            break
        seg = seg.strip(b"\r\n")
        head, _, content = seg.partition(b"\r\n\r\n")
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        nm = re.search(r'name="([^"]+)"', disp)
        fn = re.search(r'filename="([^"]+)"', disp)
        if nm:
            parts[nm.group(1)] = {
                "filename": fn.group(1) if fn else None,
                "data": content,
                "content_type": headers.get("content-type", ""),
            }
    return parts


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    _cached_body = None  # reset per request in _route(); see _body()

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):
        # Never log bodies, tokens or message contents: method + path only.
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _err(self, code, msg):
        self._json(code, {"error": msg})

    def _body(self):
        if self._cached_body is None:
            length = int(self.headers.get("Content-Length") or 0)
            self._cached_body = self.rfile.read(length) if length else b""
        return self._cached_body

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _form_body(self):
        raw = self._body().decode("utf-8", "replace")
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PATCH(self):
        self._route()

    def do_DELETE(self):
        self._route()

    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        method = self.command
        # Always consume the request body up front. With HTTP/1.1 keep-alive,
        # an early error (e.g. 401 before the body is read) leaves those
        # bytes in the socket, and the next request on the reused connection
        # is then parsed as garbage (seen live as 501s). _body() caches, so
        # handlers still get the bytes. (One handler object serves every
        # request on a keep-alive connection, so the cache resets here,
        # per request, not per connection.)
        self._cached_body = None
        self._body()
        try:
            if path == "/healthz":
                return self._json(200, {"ok": True, "version": VERSION})
            if path.startswith("/webhooks/"):
                return self._webhook(path, parsed)
            if path.startswith("/api/v1/"):
                return self._api_v1(method, path, parsed)
            if path.startswith("/api/"):
                return self._api_ui(method, path, parsed)
            if method == "GET" and (path == "/" or path.startswith("/static/")):
                return self._static(path)
            return self._err(404, "Not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            self._err(500, "Server error: %s" % e)

    def _static(self, path):
        if path == "/":
            path = "/static/index.html"
        rel = path[len("/static/"):]
        if ".." in rel or "/" in rel:
            return self._err(404, "Not found")
        fpath = os.path.join(APP_DIR, "static", rel)
        if not os.path.exists(fpath):
            return self._err(404, "Not found")
        ext = os.path.splitext(fpath)[1]
        with open(fpath, "rb") as fh:
            # No-cache so a phone never runs a stale app.js after a deploy
            # (stale JS once shifted every column after Source one left).
            self._send(200, fh.read(), MIME.get(ext, "application/octet-stream"),
                       {"Cache-Control": "no-cache"})

    # -- UI API -----------------------------------------------------------
    def _api_ui(self, method, path, parsed):
        if path == "/api/login" and method == "POST":
            data = self._json_body()
            if (data.get("password") and ADMIN_PASSWORD
                    and hmac.compare_digest(data["password"], ADMIN_PASSWORD)):
                token = create_session()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie",
                                 "crm_session=%s; HttpOnly; Path=/; SameSite=Lax; "
                                 "Max-Age=%d" % (token, SESSION_DAYS * 86400))
                body = json.dumps({"ok": True}).encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._err(401, "Wrong password.")
        if not valid_session(self):
            return self._err(401, "Sign in required.")
        if path == "/api/logout" and method == "POST":
            cookie = self.headers.get("Cookie", "")
            m = re.search(r"crm_session=([^;]+)", cookie)
            if m:
                with _db_lock:
                    db().execute("DELETE FROM sessions WHERE token=?", (m.group(1),))
                    db().commit()
            return self._json(200, {"ok": True})
        if path == "/api/me":
            return self._json(200, {"ok": True, "provider": get_setting("provider", "voipms"),
                                    "provider_configured": provider_configured(),
                                    "daily_cap": daily_cap(),
                                    "sent_today": todays_send_count()})
        if path == "/api/dashboard":
            return self._json(200, self._dashboard())
        if path == "/api/leads":
            return self._leads_ui(method, parsed)
        if path == "/api/campaigns" and method == "GET":
            with _db_lock:
                rows = db().execute(
                    "SELECT DISTINCT campaign FROM leads "
                    "WHERE campaign<>'' ORDER BY campaign").fetchall()
            return self._json(200, {"campaigns": [r["campaign"] for r in rows]})
        m = re.match(r"^/api/leads/(\d+)$", path)
        if m:
            return self._lead_ui(method, int(m.group(1)))
        if path == "/api/import" and method == "POST":
            return self._import_ui()
        if path == "/api/import-batches":
            with _db_lock:
                rows = db().execute(
                    "SELECT * FROM import_batches ORDER BY id DESC LIMIT 20").fetchall()
            return self._json(200, {"batches": [dict(r) for r in rows]})
        if path == "/api/templates":
            return self._templates_ui(method)
        m = re.match(r"^/api/templates/(\d+)$", path)
        if m and method == "DELETE":
            with _db_lock:
                db().execute("DELETE FROM templates WHERE id=?", (int(m.group(1)),))
                db().commit()
            return self._json(200, {"ok": True})
        if path == "/api/send" and method == "POST":
            return self._send_ui("ui")
        if path == "/api/send-bulk" and method == "POST":
            return self._send_bulk_ui()
        if path == "/api/inbox":
            return self._inbox_ui()
        m = re.match(r"^/api/inbox/(\d+)$", path)
        if m:
            return self._inbox_thread_ui(method, int(m.group(1)))
        m = re.match(r"^/api/inbox/(\d+)/(reply|takeover|release)$", path)
        if m and method == "POST":
            return self._inbox_action_ui(int(m.group(1)), m.group(2))
        if path == "/api/notifications":
            with _db_lock:
                rows = db().execute(
                    "SELECT * FROM notifications ORDER BY id DESC LIMIT 50").fetchall()
            return self._json(200, {"notifications": [dict(r) for r in rows]})
        if path == "/api/notifications/read" and method == "POST":
            with _db_lock:
                db().execute("UPDATE notifications SET is_read=1 WHERE is_read=0")
                db().commit()
            return self._json(200, {"ok": True})
        if path == "/api/settings":
            return self._settings_ui(method)
        if path == "/api/test-connection" and method == "POST":
            return self._test_connection()
        if path == "/api/sync-inbox" and method == "POST":
            return self._sync_inbox()
        if path == "/api/api-keys":
            return self._api_keys_ui(method)
        m = re.match(r"^/api/api-keys/(\d+)$", path)
        if m and method == "DELETE":
            with _db_lock:
                db().execute("DELETE FROM api_keys WHERE id=?", (int(m.group(1)),))
                db().commit()
            return self._json(200, {"ok": True})
        if path == "/api/api-log":
            with _db_lock:
                rows = db().execute(
                    "SELECT l.*, k.name AS key_name FROM api_log l "
                    "LEFT JOIN api_keys k ON k.id=l.key_id "
                    "ORDER BY l.id DESC LIMIT 100").fetchall()
            return self._json(200, {"log": [dict(r) for r in rows]})
        if path == "/api/backup" and method == "POST":
            return self._backup()
        return self._err(404, "Not found")

    def _dashboard(self):
        with _db_lock:
            conn = db()
            counts = {s: 0 for s in STATUSES}
            for r in conn.execute(
                    "SELECT status, COUNT(*) c FROM leads GROUP BY status"):
                counts[r["status"]] = r["c"]
            campaign_counts = {}
            for r in conn.execute(
                    "SELECT campaign, COUNT(*) c FROM leads "
                    "WHERE campaign<>'' GROUP BY campaign"):
                campaign_counts[r["campaign"]] = r["c"]
            unread = conn.execute(
                "SELECT COUNT(*) c FROM notifications WHERE is_read=0").fetchone()["c"]
            recent = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT 10").fetchall()
            total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        return {"counts": counts, "total_leads": total,
                "campaign_counts": campaign_counts,
                "sent_today": todays_send_count(), "daily_cap": daily_cap(),
                "unread_notifications": unread,
                "provider": get_setting("provider", "voipms"),
                "provider_configured": provider_configured(),
                "recent": [dict(r) for r in recent]}

    def _leads_ui(self, method, parsed):
        if method == "GET":
            qs = urllib.parse.parse_qs(parsed.query)
            status = (qs.get("status", [""])[0] or "").strip()
            q = (qs.get("q", [""])[0] or "").strip()
            campaign = (qs.get("campaign", [""])[0] or "").strip()
            sort = (qs.get("sort", ["updated_at"])[0] or "updated_at").strip()
            sort_dir = (qs.get("dir", ["desc"])[0] or "desc").strip().lower()
            limit = int(qs.get("limit", ["100"])[0] or 100)
            offset = int(qs.get("offset", ["0"])[0] or 0)
            where, args = [], []
            if status in STATUSES:
                where.append("status=?")
                args.append(status)
            if campaign:
                where.append("campaign=?")
                args.append(campaign)
            if q:
                where.append("(name LIKE ? OR business LIKE ? OR phone LIKE ? "
                             "OR city LIKE ? OR email LIKE ? OR website LIKE ? "
                             "OR address LIKE ? OR source LIKE ? OR notes LIKE ? OR campaign LIKE ?)")
                args += ["%%%s%%" % q] * 10
            sort_cols = {"name", "business", "phone", "email", "website", "address", "city", "source",
                         "campaign", "status", "updated_at"}
            if sort not in sort_cols:
                sort = "updated_at"
            if sort_dir not in ("asc", "desc"):
                sort_dir = "desc"
            sql = "SELECT * FROM leads"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY %s COLLATE NOCASE %s, updated_at DESC" % (
                sort, sort_dir.upper())
            sql += " LIMIT ? OFFSET ?"
            args += [limit, offset]
            with _db_lock:
                rows = db().execute(sql, args).fetchall()
                total = db().execute(
                    "SELECT COUNT(*) c FROM leads" +
                    (" WHERE " + " AND ".join(where) if where else ""),
                    args[:-2]).fetchone()["c"]
            return self._json(200, {"leads": [lead_dict(r) for r in rows],
                                    "total": total})
        if method == "POST":
            data = self._json_body()
            name = str(data.get("name") or "").strip()
            business = str(data.get("business") or "").strip()
            email = str(data.get("email") or "").strip()
            website = str(data.get("website") or "").strip()
            address = str(data.get("address") or "").strip()
            city = str(data.get("city") or "").strip()
            campaign = str(data.get("campaign") or "delivery").strip()
            notes = str(data.get("notes") or "").strip()
            phone = normalize_phone(data.get("phone", ""))
            if data.get("phone") and not phone:
                return self._err(400, "Phone number is not usable.")
            if not name and not business and not phone:
                return self._err(
                    400, "Add at least a name, a business, or a phone number.")
            if phone:
                with _db_lock:
                    dup = db().execute("SELECT id FROM leads WHERE phone=?",
                                       (phone,)).fetchone()
                if dup:
                    return self._err(409, "A lead with this phone already exists.")
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO leads(name, business, phone, email, website, address, city, source, notes, campaign) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (name, business, phone, email, website, address, city, "manual", notes, campaign))
                db().commit()
                row = db().execute("SELECT * FROM leads WHERE id=?",
                                   (cur.lastrowid,)).fetchone()
            return self._json(201, {"lead": lead_dict(row)})
        return self._err(405, "Method not allowed")

    def _lead_ui(self, method, lead_id):
        with _db_lock:
            lead = db().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            return self._err(404, "Lead not found.")
        if method == "GET":
            with _db_lock:
                msgs = db().execute(
                    "SELECT * FROM messages WHERE lead_id=? ORDER BY id ASC",
                    (lead_id,)).fetchall()
            return self._json(200, {"lead": lead_dict(lead),
                                    "messages": [dict(m) for m in msgs]})
        if method == "PATCH":
            data = self._json_body()
            fields = {}
            for f in ("name", "business", "email", "website", "address", "city", "source", "notes", "campaign"):
                if f in data:
                    fields[f] = data[f]
            if "phone" in data:
                phone = normalize_phone(data["phone"])
                if data["phone"] and not phone:
                    return self._err(400, "Phone number is not usable.")
                fields["phone"] = phone
            if "status" in data:
                if data["status"] not in STATUSES:
                    return self._err(400, "Unknown status.")
                fields["status"] = data["status"]
            if not fields:
                return self._err(400, "Nothing to update.")
            fields["updated_at"] = now_iso()
            sets = ", ".join("%s=?" % k for k in fields)
            with _db_lock:
                db().execute("UPDATE leads SET %s WHERE id=?" % sets,
                             list(fields.values()) + [lead_id])
                db().commit()
                row = db().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            return self._json(200, {"lead": lead_dict(row)})
        if method == "DELETE":
            with _db_lock:
                db().execute("DELETE FROM messages WHERE lead_id=?", (lead_id,))
                db().execute("DELETE FROM leads WHERE id=?", (lead_id,))
                db().commit()
            return self._json(200, {"ok": True})
        return self._err(405, "Method not allowed")

    def _import_ui(self):
        ctype = self.headers.get("Content-Type", "")
        parts = parse_multipart(self._body(), ctype)
        file_part = parts.get("file")
        if not file_part or not file_part["data"]:
            return self._err(400, "No CSV file uploaded.")
        imported_by = ""
        if "imported_by" in parts:
            imported_by = parts["imported_by"]["data"].decode("utf-8", "replace").strip()
        text = file_part["data"].decode("utf-8-sig", "replace")
        result = import_csv_text(text, file_part["filename"] or "upload.csv",
                                 imported_by or "ui")
        notify("import", "Imported %d leads from %s (%d duplicates skipped, %d invalid)."
               % (result["imported"], file_part["filename"] or "upload.csv",
                  result["skipped_dupes"], result["invalid"]))
        return self._json(200, result)

    def _templates_ui(self, method):
        if method == "GET":
            with _db_lock:
                rows = db().execute(
                    "SELECT * FROM templates ORDER BY name").fetchall()
            return self._json(200, {"templates": [dict(r) for r in rows]})
        if method == "POST":
            data = self._json_body()
            if not data.get("name") or not data.get("body"):
                return self._err(400, "Template needs a name and a body.")
            with _db_lock:
                try:
                    cur = db().execute(
                        "INSERT INTO templates(name, body) VALUES(?,?)",
                        (data["name"], data["body"]))
                    db().commit()
                except sqlite3.IntegrityError:
                    return self._err(409, "A template with that name already exists.")
                row = db().execute("SELECT * FROM templates WHERE id=?",
                                   (cur.lastrowid,)).fetchone()
            return self._json(201, {"template": dict(row)})
        return self._err(405, "Method not allowed")

    def _resolve_body(self, data, lead):
        if data.get("template_id"):
            with _db_lock:
                tpl = db().execute("SELECT * FROM templates WHERE id=?",
                                   (data["template_id"],)).fetchone()
            if not tpl:
                return None, "Template not found."
            return render_template(tpl["body"], lead), None
        if data.get("body"):
            return data["body"], None
        return None, "Provide a message body or a template_id."

    def _send_ui(self, actor):
        data = self._json_body()
        lead_id = data.get("lead_id")
        with _db_lock:
            lead = db().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            return self._err(404, "Lead not found.")
        body, err = self._resolve_body(data, dict(lead))
        if err:
            return self._err(400, err)
        msg, err = send_to_lead(dict(lead), body, actor=actor)
        if err:
            code = 409 if "taken over" in err.lower() or "opted out" in err.lower() \
                else (429 if "cap" in err.lower() else 400)
            return self._err(code, err)
        return self._json(200, {"message": dict(msg)})

    def _send_bulk_ui(self):
        data = self._json_body()
        template_id = data.get("template_id")
        lead_ids = data.get("lead_ids") or []
        status = data.get("status") or ""
        q = (data.get("q") or "").strip()
        campaign = (data.get("campaign") or "").strip()
        with _db_lock:
            if lead_ids:
                placeholders = ",".join("?" for _ in lead_ids)
                leads = db().execute(
                    "SELECT * FROM leads WHERE id IN (%s)" % placeholders,
                    lead_ids).fetchall()
            else:
                where, args = [], []
                if status in STATUSES:
                    where.append("status=?")
                    args.append(status)
                if campaign:
                    where.append("campaign=?")
                    args.append(campaign)
                if q:
                    where.append("(name LIKE ? OR business LIKE ? OR phone LIKE ? "
                                 "OR city LIKE ? OR email LIKE ? OR website LIKE ? "
                                 "OR source LIKE ? OR notes LIKE ? OR campaign LIKE ?)")
                    args += ["%%%s%%" % q] * 9
                if not where:
                    return self._err(
                        400, "Provide lead_ids, a status, a campaign, or a search.")
                leads = db().execute(
                    "SELECT * FROM leads" + " WHERE " + " AND ".join(where),
                    args).fetchall()
        sent = queued = failed = skipped = 0
        errors = []
        for lead in leads:
            lead = dict(lead)
            if lead["opted_out"]:
                skipped += 1
                continue
            body, err = self._resolve_body(
                {"template_id": template_id, "body": data.get("body")}, lead)
            if err:
                return self._err(400, err)
            msg, err = send_to_lead(lead, body, actor="ui")
            if err:
                if "cap" in err.lower():
                    failed += 1
                    errors.append("cap reached — stopped")
                    break
                failed += 1
                errors.append("%s: %s" % (lead["name"] or lead["phone"], err))
            elif msg["status"] == "sent":
                sent += 1
            else:
                queued += 1
        return self._json(200, {"sent": sent, "queued": queued, "failed": failed,
                                "skipped_opted_out": skipped, "errors": errors[:10]})

    def _inbox_ui(self):
        with _db_lock:
            conn = db()
            rows = conn.execute(
                "SELECT l.*, MAX(m.created_at) AS last_at, "
                "(SELECT body FROM messages WHERE lead_id=l.id ORDER BY id DESC LIMIT 1) AS last_body, "
                "(SELECT COUNT(*) FROM notifications n WHERE n.lead_id=l.id AND n.is_read=0) AS unread "
                "FROM leads l JOIN messages m ON m.lead_id=l.id "
                "GROUP BY l.id ORDER BY last_at DESC").fetchall()
        convs = []
        for r in rows:
            d = lead_dict(r)
            d["last_at"] = r["last_at"]
            d["last_body"] = r["last_body"]
            d["unread"] = r["unread"]
            convs.append(d)
        return self._json(200, {"conversations": convs})

    def _inbox_thread_ui(self, method, lead_id):
        if method != "GET":
            return self._err(405, "Method not allowed")
        with _db_lock:
            lead = db().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            if not lead:
                return self._err(404, "Lead not found.")
            msgs = db().execute(
                "SELECT * FROM messages WHERE lead_id=? ORDER BY id ASC",
                (lead_id,)).fetchall()
            db().execute("UPDATE notifications SET is_read=1 WHERE lead_id=?",
                         (lead_id,))
            db().commit()
        return self._json(200, {"lead": lead_dict(lead),
                                "messages": [dict(m) for m in msgs]})

    def _inbox_action_ui(self, lead_id, action):
        with _db_lock:
            lead = db().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead:
            return self._err(404, "Lead not found.")
        if action == "reply":
            data = self._json_body()
            body, err = self._resolve_body(data, dict(lead))
            if err:
                return self._err(400, err)
            msg, err = send_to_lead(dict(lead), body, actor="ui")
            if err:
                return self._err(400, err)
            return self._json(200, {"message": dict(msg)})
        if action == "takeover":
            with _db_lock:
                db().execute(
                    "UPDATE leads SET prev_status=status, status='taken_over', "
                    "updated_at=? WHERE id=?", (now_iso(), lead_id))
                db().commit()
            notify("takeover", "the operator took over the conversation.", lead_id)
            return self._json(200, {"ok": True, "status": "taken_over"})
        if action == "release":
            with _db_lock:
                row = db().execute(
                    "SELECT prev_status FROM leads WHERE id=?", (lead_id,)).fetchone()
                back = row["prev_status"] if row and row["prev_status"] in STATUSES \
                    else "replied"
                db().execute(
                    "UPDATE leads SET status=?, prev_status=NULL, updated_at=? "
                    "WHERE id=?", (back, now_iso(), lead_id))
                db().commit()
            notify("takeover", "Conversation released back to the agents.", lead_id)
            return self._json(200, {"ok": True, "status": back})
        return self._err(404, "Not found")

    def _settings_ui(self, method):
        public_keys = ["provider", "voipms_email", "voipms_did",
                       "twilio_sid", "twilio_number", "daily_cap"]
        secret_keys = ["voipms_api_password", "twilio_token"]
        if method == "GET":
            out = {}
            for k in public_keys + secret_keys:
                v = get_setting(k)
                out[k] = v if k in public_keys else ""
                out[k + "_configured"] = bool(v)
            return self._json(200, {"settings": out})
        if method == "POST":
            data = self._json_body()
            if "provider" in data and data["provider"] in ("voipms", "twilio"):
                set_setting("provider", data["provider"])
            for k in ("voipms_email", "voipms_did", "twilio_sid", "twilio_number"):
                if k in data:
                    set_setting(k, str(data[k]).strip())
            for k in secret_keys:
                if data.get(k):
                    set_setting(k, str(data[k]))
            if "daily_cap" in data:
                try:
                    cap = int(data["daily_cap"])
                    if 0 < cap <= 10000:
                        set_setting("daily_cap", str(cap))
                except (TypeError, ValueError):
                    pass
            return self._json(200, {"ok": True})
        return self._err(405, "Method not allowed")

    def _test_connection(self):
        provider = get_setting("provider", "voipms")
        try:
            if provider == "twilio":
                result = twilio_test()
            else:
                result = voipms_test()
            return self._json(200, result)
        except ProviderError as e:
            return self._json(200, {"ok": False, "detail": str(e)})

    def _sync_inbox(self):
        if get_setting("provider", "voipms") != "voipms":
            return self._err(400, "Inbox sync is a VoIP.ms feature; provider is Twilio. "
                                  "Twilio replies arrive via the webhook.")
        did = get_setting("voipms_did")
        if not did:
            return self._err(400, "VoIP.ms DID is not configured.")
        try:
            result = voipms_sync_inbox(did)
            return self._json(200, result)
        except ProviderError as e:
            return self._json(200, {"ok": False, "detail": str(e)})

    def _api_keys_ui(self, method):
        if method == "GET":
            with _db_lock:
                rows = db().execute(
                    "SELECT id, name, key_prefix, created_at, last_used_at "
                    "FROM api_keys ORDER BY id DESC").fetchall()
            return self._json(200, {"keys": [dict(r) for r in rows]})
        if method == "POST":
            data = self._json_body()
            name = (data.get("name") or "").strip()
            if not name:
                return self._err(400, "Give the key a name (e.g. the bot's name).")
            raw = create_api_key(name)
            return self._json(201, {"key": raw,
                                    "note": "Copy it now — it is shown only once."})
        return self._err(405, "Method not allowed")

    def _backup(self):
        os.makedirs(os.path.join(APP_DIR, "backups"), exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(APP_DIR, "backups", "crm-%s.db" % stamp)
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return self._json(200, {"ok": True, "file": os.path.basename(dest)})

    # -- webhooks -----------------------------------------------------------
    def _webhook(self, path, parsed):
        if path == "/webhooks/twilio":
            if self.command != "POST":
                return self._err(405, "Method not allowed")
            return self._webhook_twilio()
        if path == "/webhooks/voipms":
            return self._webhook_voipms(parsed)
        return self._err(404, "Not found")

    def _webhook_twilio(self):
        fields = self._form_body()
        token = get_setting("twilio_token")
        if token:
            signature = self.headers.get("X-Twilio-Signature", "")
            if not signature:
                return self._err(403, "Missing Twilio signature.")
            if PUBLIC_BASE_URL:
                url = PUBLIC_BASE_URL + "/webhooks/twilio"
            else:
                scheme = self.headers.get("X-Forwarded-Proto", "http")
                url = "%s://%s/webhooks/twilio" % (scheme, self.headers.get("Host", ""))
            signed = url + "".join(k + fields[k] for k in sorted(fields))
            expected = base64.b64encode(hmac.new(
                token.encode("utf-8"), signed.encode("utf-8"),
                hashlib.sha1).digest()).decode("ascii")
            if not hmac.compare_digest(expected, signature):
                return self._err(403, "Bad Twilio signature.")
        sender = fields.get("From", "")
        body = fields.get("Body", "")
        phone = normalize_phone(sender)
        if not phone or not body:
            return self._json(200, {"ok": True, "ignored": True})
        lead = find_lead_by_phone(phone)
        if not lead:
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO leads(name, phone, source) VALUES(?, ?, 'twilio-inbound')",
                    (sender, phone))
                db().commit()
                lead = dict(db().execute("SELECT * FROM leads WHERE id=?",
                                         (cur.lastrowid,)).fetchone())
            notify("reply", "New inbound number %s texted in (no lead yet)."
                   % format_phone(phone), lead["id"])
        process_inbound(lead["id"], body, provider="twilio")
        return self._json(200, {"ok": True})

    def _webhook_voipms(self, parsed):
        params = {k: v[0] for k, v in
                  urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        if self.command == "POST":
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                params.update(self._json_body())
            else:
                params.update(self._form_body())
        if VOIPMS_WEBHOOK_SECRET:
            if params.get("secret") != VOIPMS_WEBHOOK_SECRET:
                return self._err(403, "Bad webhook secret.")
        sender = (params.get("from") or params.get("From") or params.get("sms_from")
                  or params.get("sender") or params.get("src") or "")
        body = (params.get("message") or params.get("Message") or params.get("body")
                or params.get("Body") or params.get("text") or params.get("sms") or "")
        phone = normalize_phone(sender)
        if not phone or not body:
            return self._json(200, {"ok": True, "ignored": True})
        lead = find_lead_by_phone(phone)
        if not lead:
            with _db_lock:
                cur = db().execute(
                    "INSERT INTO leads(name, phone, source) VALUES(?, ?, 'voipms-inbound')",
                    (sender, phone))
                db().commit()
                lead = dict(db().execute("SELECT * FROM leads WHERE id=?",
                                         (cur.lastrowid,)).fetchone())
            notify("reply", "New inbound number %s texted in (no lead yet)."
                   % format_phone(phone), lead["id"])
        process_inbound(lead["id"], body, provider="voipms")
        return self._json(200, {"ok": True})

    # -- bot API (v1) -------------------------------------------------------
    def _api_v1(self, method, path, parsed):
        endpoint = path[len("/api/v1"):]
        key = auth_api_key(self)
        if key is None:
            log_api_call(None, endpoint, method, 401)
            return self._err(401, "A valid API key is required "
                                  "(Authorization: Bearer <key>).")
        try:
            status, obj = self._api_v1_dispatch(method, path, parsed, key)
        except Exception as e:
            status, obj = 500, {"error": "Server error: %s" % e}
        log_api_call(key["id"], endpoint, method, status)
        return self._json(status, obj)

    def _api_v1_dispatch(self, method, path, parsed, key):
        if path == "/api/v1/me" and method == "GET":
            return 200, {"ok": True, "key_name": key["name"],
                         "key_prefix": key["key_prefix"]}
        if path == "/api/v1/importLeadsCsv" and method == "POST":
            data = self._json_body()
            csv_text = data.get("csv", "")
            if not csv_text:
                return 400, {"error": "Provide CSV text in the 'csv' field."}
            result = import_csv_text(csv_text,
                                     data.get("filename", "api-import.csv"),
                                     data.get("importedBy", key["name"]))
            return 200, result
        if path == "/api/v1/listLeads" and method == "GET":
            qs = urllib.parse.parse_qs(parsed.query)
            status = (qs.get("status", [""])[0] or "").strip()
            campaign = (qs.get("campaign", [""])[0] or "").strip()
            limit = min(int(qs.get("limit", ["100"])[0] or 100), 500)
            offset = int(qs.get("offset", ["0"])[0] or 0)
            where, args = [], []
            if status in STATUSES:
                where.append("status=?")
                args.append(status)
            if campaign:
                where.append("campaign=?")
                args.append(campaign)
            sql = "SELECT * FROM leads"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
            args += [limit, offset]
            with _db_lock:
                rows = db().execute(sql, args).fetchall()
            return 200, {"leads": [lead_dict(r) for r in rows]}
        if path == "/api/v1/campaigns" and method == "GET":
            # Read-only list of campaigns for the dialer's picker:
            # every distinct campaign present in leads, e.g.
            # {"campaigns": ["delivery"]}. Same shape as the UI's
            # /api/campaigns, but under Bearer auth for bots.
            with _db_lock:
                rows = db().execute(
                    "SELECT DISTINCT campaign FROM leads "
                    "WHERE campaign<>'' ORDER BY campaign").fetchall()
            return 200, {"campaigns": [r["campaign"] for r in rows]}
        if path in ("/api/v1/sendSms", "/api/v1/sendText") and method == "POST":
            # sendText is an alias: bots kept guessing it, so both names work.
            data = self._json_body()
            lead = None
            if data.get("lead_id"):
                with _db_lock:
                    row = db().execute("SELECT * FROM leads WHERE id=?",
                                       (data["lead_id"],)).fetchone()
                lead = dict(row) if row else None
            elif data.get("phone"):
                lead = find_lead_by_phone(data["phone"])
            if not lead:
                return 404, {"error": "Lead not found (lead_id or phone)."}
            body, err = self._resolve_body(data, lead)
            if err:
                return 400, {"error": err}
            msg, err = send_to_lead(lead, body, actor="api")
            if err:
                code = 409 if ("taken over" in err.lower()
                               or "opted out" in err.lower()) else \
                    (429 if "cap" in err.lower() else 400)
                return code, {"error": err}
            return 200, {"message": dict(msg)}
        if path == "/api/v1/listInbox" and method == "GET":
            qs = urllib.parse.parse_qs(parsed.query)
            lead_id = qs.get("lead_id", [""])[0]
            with _db_lock:
                conn = db()
                if lead_id:
                    lead = conn.execute("SELECT * FROM leads WHERE id=?",
                                        (lead_id,)).fetchone()
                    if not lead:
                        return 404, {"error": "Lead not found."}
                    msgs = conn.execute(
                        "SELECT * FROM messages WHERE lead_id=? ORDER BY id ASC",
                        (lead_id,)).fetchall()
                    return 200, {"lead": lead_dict(lead),
                                 "messages": [dict(m) for m in msgs]}
                rows = conn.execute(
                    "SELECT l.*, MAX(m.created_at) AS last_at, "
                    "(SELECT body FROM messages WHERE lead_id=l.id "
                    "ORDER BY id DESC LIMIT 1) AS last_body "
                    "FROM leads l JOIN messages m ON m.lead_id=l.id "
                    "GROUP BY l.id ORDER BY last_at DESC LIMIT 100").fetchall()
            convs = []
            for r in rows:
                d = lead_dict(r)
                d["last_at"] = r["last_at"]
                d["last_body"] = r["last_body"]
                convs.append(d)
            return 200, {"conversations": convs}
        if path == "/api/v1/listCalls" and method == "GET":
            # Read back a lead's call history (outcomes + notes) for the
            # dialer: GET /api/v1/listCalls?lead_id=<id>, newest first.
            # The dialer fetches this when a lead opens so the operator sees
            # what he wrote last time instead of starting blind.
            qs = urllib.parse.parse_qs(parsed.query)
            lead_id = qs.get("lead_id", [""])[0]
            if not lead_id:
                return 400, {"error": "Provide lead_id."}
            with _db_lock:
                lead = db().execute("SELECT * FROM leads WHERE id=?",
                                    (lead_id,)).fetchone()
                if not lead:
                    return 404, {"error": "Lead not found."}
                rows = db().execute(
                    "SELECT * FROM calls WHERE lead_id=? "
                    "ORDER BY id DESC LIMIT 50",
                    (lead_id,)).fetchall()
            return 200, {"lead": lead_dict(lead),
                         "calls": [dict(c) for c in rows]}
        if path == "/api/v1/recordInboundReply" and method == "POST":
            data = self._json_body()
            phone = normalize_phone(data.get("phone", ""))
            body = (data.get("body") or "").strip()
            if not phone or not body:
                return 400, {"error": "Provide phone and body."}
            lead = find_lead_by_phone(phone)
            if not lead:
                with _db_lock:
                    cur = db().execute(
                        "INSERT INTO leads(name, phone, source) VALUES(?, ?, ?)",
                        (data.get("phone"), phone, key["name"]))
                    db().commit()
                    lead = dict(db().execute("SELECT * FROM leads WHERE id=?",
                                             (cur.lastrowid,)).fetchone())
            lead = process_inbound(lead["id"], body, provider="api")
            action = ("marked Do not contact (STOP)"
                      if lead["opted_out"] else
                      ("held for the operator (taken over)" if lead["status"] == "taken_over"
                       else "marked Replied"))
            return 200, {"lead": lead, "action": action}
        if path == "/api/v1/recordCall" and method == "POST":
            data = self._json_body()
            lead = None
            if data.get("lead_id"):
                with _db_lock:
                    row = db().execute("SELECT * FROM leads WHERE id=?",
                                       (data["lead_id"],)).fetchone()
                lead = dict(row) if row else None
            elif data.get("phone"):
                lead = find_lead_by_phone(data["phone"])
            if not lead:
                return 404, {"error": "Lead not found (lead_id or phone)."}
            outcome = (data.get("outcome") or "").strip()
            if outcome not in CALL_OUTCOMES:
                return 400, {"error": "Unknown outcome. Use one of: %s"
                             % ", ".join(CALL_OUTCOMES)}
            to_campaign = None
            if outcome == "shift_campaign":
                to_campaign = (data.get("to_campaign") or "").strip().lower()
                allowed = valid_shift_campaigns()
                if to_campaign not in allowed and not is_valid_new_campaign(to_campaign):
                    return 400, {"error":
                                 "Unknown campaign. Use one of: %s, or a new "
                                 "campaign name (one lowercase word, letters, "
                                 "numbers and hyphens, 1-30 chars, starting "
                                 "and ending with a letter or digit)"
                                 % ", ".join(allowed)}
            note = str(data.get("note") or "").strip()
            lead, call = record_call(lead["id"], outcome, note=note,
                                     actor=key["name"],
                                     to_campaign=to_campaign)
            return 200, {"lead": lead_dict(lead), "call": call}
        if path == "/api/v1/updateLead" and method == "POST":
            # Edit a lead's business, contact name and/or address (the
            # dialer's "Edit name" button): POST {"lead_id": <id>,
            # "business": "...", "name": "...", "address": "..."}.
            # Only the fields present in the body are changed; the rest
            # is left alone. Either lead_id or phone identifies the lead,
            # like recordCall. updated_at is bumped so the edited
            # lead sorts as freshly touched.
            data = self._json_body()
            lead = None
            if data.get("lead_id"):
                with _db_lock:
                    row = db().execute("SELECT * FROM leads WHERE id=?",
                                       (data["lead_id"],)).fetchone()
                lead = dict(row) if row else None
            elif data.get("phone"):
                lead = find_lead_by_phone(data["phone"])
            if not lead:
                return 404, {"error": "Lead not found (lead_id or phone)."}
            sets, args = [], []
            if "business" in data:
                sets.append("business=?")
                args.append(str(data.get("business") or "").strip())
            if "name" in data:
                sets.append("name=?")
                args.append(str(data.get("name") or "").strip())
            if "address" in data:
                sets.append("address=?")
                args.append(str(data.get("address") or "").strip())
            if not sets:
                return 400, {"error": "Provide business, name and/or address to update."}
            sets.append("updated_at=?")
            args.append(now_iso())
            args.append(lead["id"])
            with _db_lock:
                db().execute("UPDATE leads SET " + ", ".join(sets) + " WHERE id=?",
                             args)
                db().commit()
                lead = db().execute("SELECT * FROM leads WHERE id=?",
                                    (lead["id"],)).fetchone()
            return 200, {"lead": lead_dict(lead)}
        return 404, {"error": "Unknown endpoint: %s" % path}


# ---------------------------------------------------------------- CLI / main

def cli_create_key(name):
    db()
    raw = create_api_key(name)
    print("API key for %r (copy it now — it is shown only once):" % name)
    print(raw)
    return 0


def cli_list_keys():
    db()
    with _db_lock:
        rows = db().execute(
            "SELECT id, name, key_prefix, created_at, last_used_at FROM api_keys "
            "ORDER BY id").fetchall()
    if not rows:
        print("No API keys yet.")
        return 0
    for r in rows:
        print("#%d %-20s %s  created %s  last used %s" % (
            r["id"], r["name"], r["key_prefix"], r["created_at"],
            r["last_used_at"] or "never"))
    return 0


def cli_revoke_key(prefix):
    db()
    with _db_lock:
        cur = db().execute("DELETE FROM api_keys WHERE key_prefix LIKE ?",
                           (prefix + "%",))
        db().commit()
    print("Revoked %d key(s)." % cur.rowcount)
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Lead Outreach CRM (self-hosted)")
    ap.add_argument("--create-key", metavar="NAME",
                    help="create a bot API key and print it once")
    ap.add_argument("--list-keys", action="store_true", help="list API keys")
    ap.add_argument("--revoke-key", metavar="PREFIX",
                    help="revoke API key(s) by prefix")
    args = ap.parse_args()
    if args.create_key:
        return cli_create_key(args.create_key)
    if args.list_keys:
        return cli_list_keys()
    if args.revoke_key:
        return cli_revoke_key(args.revoke_key)
    if not ADMIN_PASSWORD:
        print("ERROR: ADMIN_PASSWORD is not set. Put it in .env (see .env.example) "
              "or the environment, then start again.", file=sys.stderr)
        return 2
    db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Lead Outreach CRM %s listening on http://%s:%d" % (VERSION, HOST, PORT))
    print("Database: %s" % DB_PATH)
    print("Public URL: %s" % (PUBLIC_BASE_URL or "(not set — set PUBLIC_BASE_URL "
          "for correct Twilio webhook signatures)"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
