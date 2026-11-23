#!/usr/bin/env python3
"""End-to-end verification for the self-hosted Lead Outreach CRM.

Starts crm.py on a throwaway DB, then exercises: UI load, login,
dashboard, CSV import (incl. Qualifier headers, dupes, invalid rows),
manual single-lead add (valid / empty / bad phone / duplicate),
queued sends with no provider, daily cap, templates, takeover,
bot API (good key / bad key / import / send / inbox / inbound),
both webhooks (incl. Twilio signature valid/invalid/missing),
STOP handling, backup, and the API log.

Usage: python3 test_crm.py   (exits 0 only if every check passes)
"""
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 18081
BASE = "http://127.0.0.1:%d" % PORT

passed, failed = [], []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" — " + str(detail) if detail and not cond else ""))


class Client:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def req(self, method, path, body=None, headers=None, raw=False):
        url = BASE + path
        data = None
        hdrs = dict(headers or {})
        if body is not None and not raw:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        elif raw:
            data = body
        r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with self.opener.open(r, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def jreq(self, method, path, body=None, headers=None):
        code, raw = self.req(method, path, body, headers)
        try:
            return code, json.loads(raw.decode())
        except Exception:
            return code, {"_raw": raw.decode("utf-8", "replace")}

    def headers(self, path):
        r = urllib.request.Request(BASE + path, method="GET")
        try:
            with self.opener.open(r, timeout=10) as resp:
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)


def main():
    tmp = tempfile.mkdtemp(prefix="crm-test-")
    db_path = os.path.join(tmp, "test.db")
    env = dict(os.environ, ADMIN_PASSWORD="test-admin-password",
               CRM_DB=db_path, HOST="127.0.0.1", PORT=str(PORT),
               PUBLIC_BASE_URL=BASE)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "crm.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # wait for health
        up = False
        for _ in range(50):
            try:
                with urllib.request.urlopen(BASE + "/healthz", timeout=2) as r:
                    up = r.status == 200
                    break
            except Exception:
                time.sleep(0.2)
        check("server starts and /healthz answers", up)
        if not up:
            print(proc.stdout.read() if proc.stdout else "")
            return 1

        c = Client()
        code, _ = c.req("GET", "/")
        check("UI loads (GET /)", code == 200)
        code, raw = c.req("GET", "/static/app.js")
        check("app.js serves", code == 200 and b"Lead Outreach CRM" in raw)
        code, _ = c.jreq("GET", "/api/me")
        check("unauthenticated /api/me is 401", code == 401)
        code, _ = c.jreq("POST", "/api/login", {"password": "wrong"})
        check("wrong password is 401", code == 401)
        code, _ = c.jreq("POST", "/api/login", {"password": "test-admin-password"})
        check("login works", code == 200)
        code, me = c.jreq("GET", "/api/me")
        check("authed /api/me works", code == 200 and me.get("provider") == "voipms")

        # template
        code, tpl = c.jreq("POST", "/api/templates",
                           {"name": "intro", "body": "Hi {name} from {business} in {city}, this is Alex."})
        check("create template", code == 201, tpl)
        tpl_id = tpl["template"]["id"]

        def upload_csv(csv_text, filename="leads.csv", by="test"):
            boundary = "----testboundary1234"
            parts = [
                ("--" + boundary).encode(),
                b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\nContent-Type: text/csv\r\n',
                csv_text.encode(),
                ("--" + boundary).encode(),
                b'Content-Disposition: form-data; name="imported_by"\r\n',
                by.encode(),
                ("--" + boundary + "--").encode(),
            ]
            body = b"\r\n".join(parts)
            code, raw = c.req("POST", "/api/import", body=body, raw=True,
                              headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
            return code, json.loads(raw.decode())

        # CSV import: normal row, dupe, invalid phone, empty row
        code, imp = upload_csv(
            "name,business,phone,email,city,source,notes\r\n"
            "Ana Torres,Torres Bakery,9545550101,ana@example.com,Fort Lauderdale,manual,likes morning delivery\r\n"
            "Ana Torres,Torres Bakery,9545550101,ana@example.com,Fort Lauderdale,manual,duplicate\r\n"
            "Bad Row,No Phone Co,123, bad@example.com,Miami,manual,\r\n"
            ",,,,\r\n")
        check("CSV import counts (1 in, 1 dupe, 2 invalid, 4 rows)",
              code == 200 and imp.get("imported") == 1 and imp.get("skipped_dupes") == 1
              and imp.get("invalid") == 2 and imp.get("total_rows") == 4, imp)

        # Qualifier-style headers in their own file
        code, imp_q = upload_csv(
            "business name,contact name,contact email,city,category,reason,status\r\n"
            "Sunset Cafe,Ben Ortiz,ben@example.com,Hollywood,cafe,needs lunch catering,verified\r\n",
            filename="qualifier.csv", by="qualifier")
        check("Qualifier headers import (1 in)",
              code == 200 and imp_q.get("imported") == 1, imp_q)

        code, leads = c.jreq("GET", "/api/leads")
        check("listLeads returns 2 leads", code == 200 and len(leads["leads"]) == 2, leads)
        ana = next(l for l in leads["leads"] if l["phone"] == "9545550101")
        ben = next(l for l in leads["leads"] if l["name"] == "Ben Ortiz")
        check("Qualifier row mapped (business/name/email/city, no phone)",
              ben["business"] == "Sunset Cafe" and ben["phone"] == "" and "cafe" in ben["notes"], ben)

        # queued send with no provider: recorded, lead NOT advanced
        code, sent = c.jreq("POST", "/api/send", {"lead_id": ana["id"], "template_id": tpl_id})
        check("send with no provider is queued, not faked",
              code == 200 and sent["message"]["status"] == "queued", sent)
        code, lead = c.jreq("GET", "/api/leads/%d" % ana["id"])
        check("queued send does not advance lead status",
              lead["lead"]["status"] == "new", lead["lead"])

        # daily cap
        c.jreq("POST", "/api/settings", {"daily_cap": 2})
        code, s2 = c.jreq("POST", "/api/send", {"lead_id": ana["id"], "body": "second"})
        check("second queued send under cap=2 ok", code == 200, s2)
        code, s3 = c.jreq("POST", "/api/send", {"lead_id": ana["id"], "body": "third"})
        check("third send hits daily cap (429)", code == 429, s3)
        c.jreq("POST", "/api/settings", {"daily_cap": 100})

        # API key
        code, key = c.jreq("POST", "/api/api-keys", {"name": "MyBot"})
        check("create API key", code == 201 and key["key"].startswith("crm_live_"), key)
        api_key = key["key"]

        bad = Client()
        code, _ = bad.jreq("GET", "/api/v1/listLeads",
                           headers={"Authorization": "Bearer crm_live_wrong"})
        check("bot API rejects bad key (401)", code == 401)
        code, _ = bad.jreq("GET", "/api/v1/listLeads")
        check("bot API rejects missing key (401)", code == 401)

        v1 = Client()
        code, lst = v1.jreq("GET", "/api/v1/listLeads",
                            headers={"Authorization": "Bearer " + api_key})
        check("bot API listLeads works", code == 200 and len(lst["leads"]) == 2, lst)
        code, imp2 = v1.jreq("POST", "/api/v1/importLeadsCsv",
                             {"csv": "name,phone\nCarla Ruiz,9545550103\n",
                              "importedBy": "mybot"},
                             headers={"Authorization": "Bearer " + api_key})
        check("bot API importLeadsCsv", code == 200 and imp2.get("imported") == 1, imp2)
        code, vs = v1.jreq("POST", "/api/v1/sendSms",
                           {"lead_id": ana["id"], "body": "Hi Ana, MyBot here."},
                           headers={"Authorization": "Bearer " + api_key})
        check("bot API sendSms queues (no provider)",
              code == 200 and vs["message"]["status"] == "queued", vs)
        code, vt = v1.jreq("POST", "/api/v1/sendText",
                           {"lead_id": ana["id"], "body": "Hi Ana, MyBot here (sendText)."},
                           headers={"Authorization": "Bearer " + api_key})
        check("bot API sendText alias queues (no provider)",
              code == 200 and vt["message"]["status"] == "queued", vt)
        code, _ = bad.jreq("POST", "/api/v1/sendText",
                           {"lead_id": ana["id"], "body": "x"})
        check("bot API sendText rejects missing key (401)", code == 401)
        code, nf = v1.jreq("POST", "/api/v1/sendText",
                           {"lead_id": 999999, "body": "x"},
                           headers={"Authorization": "Bearer " + api_key})
        check("bot API sendText unknown lead (404)", code == 404, nf)
        code, nb = v1.jreq("POST", "/api/v1/sendText",
                           {"lead_id": ana["id"]},
                           headers={"Authorization": "Bearer " + api_key})
        check("bot API sendText missing body (400)", code == 400, nb)

        # keep-alive: a 401 that never reads the body must not poison the
        # next request on the same connection (cloudflared reuses them)
        import http.client
        raw = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        raw.request("POST", "/api/v1/sendText",
                    body=json.dumps({"lead_id": ana["id"], "body": "x"}),
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer crm_live_wrong"})
        resp = raw.getresponse()
        check("bad-key sendText on reused connection (401)",
              resp.status == 401, resp.status)
        resp.read()
        raw.request("POST", "/api/v1/sendText",
                    body=json.dumps({"lead_id": ana["id"], "body": "second try"}),
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + api_key})
        resp2 = raw.getresponse()
        check("next request on same connection parses clean (200)",
              resp2.status == 200, resp2.status)
        resp2.read()
        raw.close()

        # inbound via API first, so takeover/release has a 'replied' status to restore
        code, inbound = v1.jreq("POST", "/api/v1/recordInboundReply",
                                {"phone": "9545550101", "body": "Sounds good, tell me more."},
                                headers={"Authorization": "Bearer " + api_key})
        check("bot API recordInboundReply marks Replied",
              code == 200 and inbound["lead"]["status"] == "replied", inbound)

        # takeover blocks API sends, not UI sends
        code, _ = c.jreq("POST", "/api/inbox/%d/takeover" % ana["id"], {})
        check("takeover via UI", code == 200)
        code, blocked = v1.jreq("POST", "/api/v1/sendSms",
                                {"lead_id": ana["id"], "body": "bot again"},
                                headers={"Authorization": "Bearer " + api_key})
        check("API send blocked while taken over (409)", code == 409, blocked)
        code, ok = c.jreq("POST", "/api/send", {"lead_id": ana["id"], "body": "Operator here."})
        check("UI send still works while taken over", code == 200, ok)
        code, rel = c.jreq("POST", "/api/inbox/%d/release" % ana["id"], {})
        check("release restores status", code == 200 and rel.get("status") == "replied", rel)

        # STOP via VoIP.ms webhook (form POST)
        code, _ = c.req("POST", "/webhooks/voipms",
                        body=b"from=%2B19545550101&message=STOP",
                        raw=True, headers={"Content-Type": "application/x-www-form-urlencoded"})
        check("VoIP.ms webhook accepts STOP", code == 200)
        code, lead = c.jreq("GET", "/api/leads/%d" % ana["id"])
        check("STOP marks Do not contact + opted out",
              lead["lead"]["status"] == "do_not_contact" and lead["lead"]["opted_out"] == 1,
              lead["lead"])
        code, refused = c.jreq("POST", "/api/send", {"lead_id": ana["id"], "body": "x"})
        check("send to opted-out lead refused", code in (400, 409), refused)

        # VoIP.ms webhook via GET query
        code, _ = c.req("GET", "/webhooks/voipms?from=9545550103&message=Hello%20there")
        check("VoIP.ms webhook via GET", code == 200)
        code, lead3 = c.jreq("GET", "/api/leads?status=replied")
        check("GET webhook recorded inbound reply",
              any(l["phone"] == "9545550103" for l in lead3["leads"]), lead3)

        # Twilio webhook: set token, valid signature passes, bad/missing fail
        c.jreq("POST", "/api/settings", {"twilio_token": "test-twilio-token",
                                         "twilio_sid": "ACtest", "twilio_number": "+15555550100"})
        params = {"From": "+19545550103", "To": "+15555550100", "Body": "Twilio reply here"}
        url = BASE + "/webhooks/twilio"
        signed = url + "".join(k + params[k] for k in sorted(params))
        sig = base64.b64encode(hmac.new(b"test-twilio-token", signed.encode(),
                                        hashlib.sha1).digest()).decode()
        body = urllib.parse.urlencode(params).encode()
        code, _ = c.req("POST", "/webhooks/twilio", body=body, raw=True,
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "X-Twilio-Signature": sig})
        check("Twilio webhook with valid signature", code == 200)
        code, _ = c.req("POST", "/webhooks/twilio", body=body, raw=True,
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "X-Twilio-Signature": "badbadbad"})
        check("Twilio webhook with bad signature is 403", code == 403)
        code, _ = c.req("POST", "/webhooks/twilio", body=body, raw=True,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        check("Twilio webhook without signature is 403", code == 403)

        # inbox + notifications + backup + api log
        code, inbox = c.jreq("GET", "/api/inbox")
        check("inbox lists conversations", code == 200 and len(inbox["conversations"]) >= 2, inbox)
        code, notifs = c.jreq("GET", "/api/notifications")
        check("notifications recorded",
              code == 200 and len(notifs["notifications"]) >= 3, notifs)
        code, bk = c.jreq("POST", "/api/backup", {})
        check("backup endpoint creates a file",
              code == 200 and os.path.exists(os.path.join(HERE, "backups", bk["file"])), bk)
        code, log = c.jreq("GET", "/api/api-log")
        check("API log recorded bot calls",
              code == 200 and len(log["log"]) >= 5, log)
        code, dash = c.jreq("GET", "/api/dashboard")
        check("dashboard counts",
              code == 200 and dash["counts"]["do_not_contact"] == 1, dash)

        # manual single-lead add (POST /api/leads)
        code, added = c.jreq("POST", "/api/leads",
                             {"name": "Dana Kim", "business": "Kim Florist",
                              "phone": "(954) 555-0107", "email": "dana@example.com",
                              "city": "Pompano Beach", "notes": "typed in by hand"})
        check("manual add lead (201, status new, source manual, phone normalized)",
              code == 201 and added["lead"]["status"] == "new"
              and added["lead"]["source"] == "manual"
              and added["lead"]["phone"] == "9545550107", added)
        code, empty = c.jreq("POST", "/api/leads", {})
        check("manual add with no name/business/phone is 400", code == 400, empty)
        code, badphone = c.jreq("POST", "/api/leads",
                                {"name": "No Number", "phone": "123"})
        check("manual add with unusable phone is 400", code == 400, badphone)
        code, dupe = c.jreq("POST", "/api/leads",
                            {"name": "Dana Again", "phone": "9545550107"})
        check("manual add with duplicate phone is 409", code == 409, dupe)

        # bot CSV: the exact six headers the bots generate, incl. website
        # and "generated by" (which lands in source)
        code, imp_bot = upload_csv(
            "business name,phone number,email,website,contact name,generated by\r\n"
            "Bot Test Bakery,9545550111,bot@example.com,www.bottestbakery.com,Mia Bot,Miles\r\n",
            filename="bot-leads.csv", by="drive-drop")
        check("bot CSV import (1 in)", code == 200 and imp_bot.get("imported") == 1, imp_bot)
        code, found = c.jreq("GET", "/api/leads?q=bottestbakery")
        check("bot CSV keeps website and generated-by in source (also found via website search)",
              code == 200 and len(found["leads"]) == 1
              and found["leads"][0]["website"] == "www.bottestbakery.com"
              and found["leads"][0]["source"] == "Miles", found)
        code, found_src = c.jreq("GET", "/api/leads?q=Miles")
        check("lead search finds by source (generated-by bot name)",
              code == 200 and len(found_src["leads"]) == 1
              and found_src["leads"][0]["source"] == "Miles", found_src)
        code, found_none = c.jreq("GET", "/api/leads?q=zzzznomatch")
        check("lead search with no match returns empty list",
              code == 200 and found_none["leads"] == [] and found_none["total"] == 0, found_none)

        # campaign: a 7th CSV column the bots put on every row from now on
        code, imp_dj = upload_csv(
            "business name,phone number,email,website,contact name,generated by,campaign\r\n"
            "DJ Test Beats,9545550114,dj@example.com,www.djtestbeats.com,Dee Jay,Miles,djs\r\n",
            filename="dj-leads.csv", by="drive-drop")
        check("bot CSV import keeps campaign column",
              code == 200 and imp_dj.get("imported") == 1, imp_dj)
        code, found_dj = c.jreq("GET", "/api/leads?campaign=djs")
        check("lead filter by campaign returns only that campaign",
              code == 200 and len(found_dj["leads"]) == 1
              and found_dj["leads"][0]["campaign"] == "djs"
              and found_dj["leads"][0]["business"] == "DJ Test Beats", found_dj)
        code, found_djq = c.jreq("GET", "/api/leads?q=djs")
        check("lead search finds by campaign value",
              code == 200 and len(found_djq["leads"]) == 1
              and found_djq["leads"][0]["campaign"] == "djs", found_djq)
        code, camps = c.jreq("GET", "/api/campaigns")
        check("campaigns endpoint lists distinct campaigns",
              code == 200 and "djs" in camps.get("campaigns", []), camps)
        # a 6-column CSV with no campaign column defaults to delivery
        code, imp_nocamp = upload_csv(
            "business name,phone number,email,website,contact name,generated by\r\n"
            "No Camp Shop,9545550115,nocamp@example.com,www.nocamp.example.com,,LeadBot\r\n",
            filename="no-campaign.csv", by="drive-drop")
        code, found_nc = c.jreq("GET", "/api/leads?q=No+Camp+Shop")
        check("bot CSV import without a campaign column defaults to delivery",
              code == 200 and imp_nocamp.get("imported") == 1
              and len(found_nc["leads"]) == 1
              and found_nc["leads"][0]["campaign"] == "delivery", found_nc)

        # bulk send respects the campaign/search filters (no provider in
        # the test env, so matching leads queue instead of sending)
        code, tpl = c.jreq("POST", "/api/templates",
                           {"name": "Bulk Test", "body": "Hi {name}, test"})
        check("template created for bulk test",
              code == 201 and tpl["template"]["id"], tpl)
        tpl_id = tpl["template"]["id"]
        code, bulk_dj = c.jreq("POST", "/api/send-bulk",
                               {"template_id": tpl_id, "campaign": "djs"})
        check("bulk send by campaign queues only that campaign's leads",
              code == 200 and bulk_dj.get("queued") == 1
              and bulk_dj.get("sent") == 0, bulk_dj)
        code, bulk_q = c.jreq("POST", "/api/send-bulk",
                              {"template_id": tpl_id, "q": "bottestbakery"})
        check("bulk send by search queues only matching leads",
              code == 200 and bulk_q.get("queued") == 1
              and bulk_q.get("sent") == 0, bulk_q)
        code, bulk_none = c.jreq("POST", "/api/send-bulk",
                                 {"template_id": tpl_id})
        check("bulk send with no selector is 400",
              code == 400, bulk_none)

        # sortable columns: tap a header, list comes back in that order
        code, s_asc = c.jreq("GET", "/api/leads?sort=business&dir=asc")
        biz_asc = [(l["business"] or "").lower() for l in s_asc["leads"]]
        check("leads sort by business ascending",
              code == 200 and biz_asc == sorted(biz_asc) and len(biz_asc) >= 3, biz_asc)
        code, s_desc = c.jreq("GET", "/api/leads?sort=business&dir=desc")
        biz_desc = [(l["business"] or "").lower() for l in s_desc["leads"]]
        check("leads sort by business descending",
              code == 200 and biz_desc == sorted(biz_desc, reverse=True), biz_desc)
        code, s_bad = c.jreq("GET", "/api/leads?sort=bogus&dir=sideways")
        check("leads sort with bad column/direction falls back to default",
              code == 200 and len(s_bad["leads"]) >= 3, s_bad)
        code, s_email = c.jreq("GET", "/api/leads?sort=email&dir=asc")
        em_asc = [(l["email"] or "").lower() for l in s_email["leads"]]
        check("leads sort by email ascending",
              code == 200 and em_asc == sorted(em_asc) and len(em_asc) >= 3, em_asc)

        # leads table scrolls sideways on a phone, and static files are
        # served no-cache so a deploy never leaves a stale app.js behind
        code, home = c.req("GET", "/")
        check("leads table sits in a sideways-scroll wrapper",
              code == 200 and b'class="tablewrap"' in home, home[:200])
        check("leads table has an Email column",
              code == 200 and b'data-sort="email"' in home, home[:200])
        check("leads table shows website, contact name and generated by, no city",
              code == 200 and b'data-sort="website"' in home and b"Contact name" in home
              and b"Generated by" in home and b'data-sort="city"' not in home, home[:200])
        code, js = c.req("GET", "/static/app.js")
        check("websites are shortened to 10 characters in the table",
              code == 200 and b"slice(0, 10)" in js, js[:200])
        code, css = c.req("GET", "/static/style.css")
        check("stylesheet defines the scroll wrapper",
              code == 200 and b".tablewrap" in css and b"overflow-x" in css, css[:200])
        code, hdrs = c.headers("/static/app.js")
        check("static files served no-cache",
              code == 200 and hdrs.get("Cache-Control") == "no-cache", hdrs)
        check("static URLs are versioned so a deploy busts caches",
              code == 200 and b"/static/app.js?v=" in home
              and b"/static/style.css?v=" in home, home[:400])

        # manual single-lead add with a website
        code, added_w = c.jreq("POST", "/api/leads",
                               {"name": "Web Owner", "business": "Web Shop",
                                "phone": "9545550112",
                                "website": "https://webshop.example.com"})
        check("manual add keeps website",
              code == 201 and added_w["lead"]["website"] == "https://webshop.example.com", added_w)

        # call dispositions: POST /api/v1/recordCall
        code, imp_call = v1.jreq("POST", "/api/v1/importLeadsCsv",
                                 {"csv": "name,phone\n"
                                         "Call One,9545550201\n"
                                         "Call Two,9545550202\n"
                                         "Call Three,9545550203\n"
                                         "Call Four,9545550204\n"
                                         "Call Five,9545550205\n"
                                         "Call Six,9545550206\n"
                                         "Call Seven,9545550207\n"
                                         "Call Eight,9545550208\n",
                                  "importedBy": "mybot"},
                                 headers={"Authorization": "Bearer " + api_key})
        check("bot API import leads for call tests",
              code == 200 and imp_call.get("imported") == 8, imp_call)
        code, call_leads = v1.jreq("GET", "/api/v1/listLeads?limit=500",
                                   headers={"Authorization": "Bearer " + api_key})
        by_phone = {l["phone"]: l for l in call_leads["leads"]}

        def record_call(lead_id=None, phone=None, outcome="no_answer", note="",
                        to_campaign=None):
            body = {"outcome": outcome, "note": note}
            if lead_id is not None:
                body["lead_id"] = lead_id
            if phone is not None:
                body["phone"] = phone
            if to_campaign is not None:
                body["to_campaign"] = to_campaign
            return v1.jreq("POST", "/api/v1/recordCall", body,
                           headers={"Authorization": "Bearer " + api_key})

        code, _ = bad.jreq("POST", "/api/v1/recordCall",
                           {"lead_id": by_phone["9545550201"]["id"],
                            "outcome": "interested"})
        check("bot API recordCall rejects missing key (401)", code == 401)
        code, _ = bad.jreq("POST", "/api/v1/recordCall",
                           {"lead_id": by_phone["9545550201"]["id"],
                            "outcome": "interested"},
                           headers={"Authorization": "Bearer crm_live_wrong"})
        check("bot API recordCall rejects bad key (401)", code == 401)
        code, nf = record_call(lead_id=999999, outcome="interested")
        check("bot API recordCall unknown lead_id (404)", code == 404, nf)
        code, nf2 = record_call(phone="9545550999", outcome="interested")
        check("bot API recordCall unknown phone (404)", code == 404, nf2)
        code, bad_out = record_call(lead_id=by_phone["9545550201"]["id"],
                                    outcome="maybe_later")
        check("bot API recordCall bad outcome (400)", code == 400, bad_out)
        code, no_out = v1.jreq("POST", "/api/v1/recordCall",
                               {"lead_id": by_phone["9545550201"]["id"]},
                               headers={"Authorization": "Bearer " + api_key})
        check("bot API recordCall missing outcome (400)", code == 400, no_out)

        # each outcome's status effect (opted_out=1 only on do_not_contact)
        cases = [
            ("9545550201", "interested", "taken_over", 0),
            ("9545550202", "not_interested", "do_not_contact", 0),
            ("9545550203", "wrong_number", "do_not_contact", 0),
            ("9545550204", "do_not_contact", "do_not_contact", 1),
            ("9545550205", "no_answer", "new", 0),
            ("9545550206", "voicemail", "new", 0),
            ("9545550207", "callback", "new", 0),
            ("9545550208", "not_qualified", "not_qualified", 0),
        ]
        for phone, outcome, status, opted in cases:
            lead_id = by_phone[phone]["id"]
            code, res = record_call(lead_id=lead_id, outcome=outcome,
                                    note="called: " + outcome)
            check("recordCall %s -> status %s, opted_out %d"
                  % (outcome, status, opted),
                  code == 200 and res["lead"]["status"] == status
                  and res["lead"]["opted_out"] == opted
                  and res["call"]["outcome"] == outcome
                  and res["call"]["lead_id"] == lead_id
                  and res["call"]["note"] == "called: " + outcome, res)

        # lookup by phone works too, and a second call on the same lead
        # is recorded alongside the first
        code, byph = record_call(phone="9545550201", outcome="callback",
                                 note="call back later")
        check("recordCall by phone (taken_over stays, call recorded)",
              code == 200 and byph["lead"]["status"] == "taken_over"
              and byph["call"]["outcome"] == "callback"
              and byph["call"]["lead_id"] == by_phone["9545550201"]["id"]
              and byph["call"]["note"] == "call back later", byph)

        # the calls table itself holds every successful call (9: 8 by id,
        # 1 by phone); the failed 401/404/400 calls recorded nothing
        import sqlite3
        conn = sqlite3.connect(db_path)
        n_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        conn.close()
        check("calls table recorded all 9 calls", n_calls == 9, n_calls)

        # listCalls reads the history back (dialer notes read-back):
        # newest first, outcomes + notes + who recorded them
        code, hist = v1.jreq(
            "GET", "/api/v1/listCalls?lead_id=%d" % by_phone["9545550201"]["id"],
            headers={"Authorization": "Bearer " + api_key})
        check("bot API listCalls returns both calls, newest first",
              code == 200 and len(hist["calls"]) == 2
              and hist["calls"][0]["outcome"] == "callback"
              and hist["calls"][0]["note"] == "call back later"
              and hist["calls"][1]["outcome"] == "interested"
              and hist["calls"][1]["note"] == "called: interested", hist)
        code, hist1 = v1.jreq(
            "GET", "/api/v1/listCalls?lead_id=%d" % by_phone["9545550205"]["id"],
            headers={"Authorization": "Bearer " + api_key})
        check("bot API listCalls single call with note",
              code == 200 and len(hist1["calls"]) == 1
              and hist1["calls"][0]["outcome"] == "no_answer"
              and hist1["calls"][0]["note"] == "called: no_answer", hist1)
        code, _ = v1.jreq("GET", "/api/v1/listCalls?lead_id=999999",
                          headers={"Authorization": "Bearer " + api_key})
        check("bot API listCalls unknown lead_id (404)", code == 404)
        code, _ = v1.jreq("GET", "/api/v1/listCalls",
                          headers={"Authorization": "Bearer " + api_key})
        check("bot API listCalls missing lead_id (400)", code == 400)
        code, _ = bad.jreq("GET", "/api/v1/listCalls?lead_id=%d"
                           % by_phone["9545550201"]["id"])
        check("bot API listCalls rejects missing key (401)", code == 401)
        code, log2 = c.jreq("GET", "/api/api-log")
        check("API log recorded recordCall calls",
              code == 200 and any(e.get("endpoint") == "/recordCall"
                                  and e.get("status") == 200
                                  for e in log2["log"]), log2)

        # "Not qualified" works end to end: dashboard counts it, the
        # leads filter finds it, and PATCH accepts it (the no-call route
        # for removing a lead that isn't a fit — e.g. a restaurant).
        # opted_out stays 0: that flag is only for people who asked to stop.
        code, dash2 = c.jreq("GET", "/api/dashboard")
        check("dashboard counts include not_qualified",
              code == 200 and dash2["counts"]["not_qualified"] == 1, dash2)
        code, nq = c.jreq("GET", "/api/leads?status=not_qualified")
        check("leads filter accepts not_qualified",
              code == 200 and len(nq["leads"]) == 1
              and nq["leads"][0]["phone"] == "9545550208"
              and nq["leads"][0]["status_label"] == "Not qualified", nq)
        code, patched = c.jreq("PATCH", "/api/leads/%d" % by_phone["9545550205"]["id"],
                               {"status": "not_qualified"})
        check("PATCH lead status to not_qualified (no call needed)",
              code == 200 and patched["lead"]["status"] == "not_qualified"
              and patched["lead"]["opted_out"] == 0, patched)
        code, bad_status = c.jreq("PATCH", "/api/leads/%d" % by_phone["9545550205"]["id"],
                                  {"status": "bogus_status"})
        check("PATCH unknown status is 400", code == 400, bad_status)
        code, nq2 = c.jreq("GET", "/api/leads?status=not_qualified")
        check("not_qualified leads leave the new pool (2 after PATCH)",
              code == 200 and len(nq2["leads"]) == 2, nq2)
        code, fresh = v1.jreq("GET", "/api/v1/listLeads?status=new&limit=500",
                              headers={"Authorization": "Bearer " + api_key})
        check("not_qualified leads no longer appear in the dialer queue (status=new)",
              code == 200 and all(l["phone"] not in ("9545550205", "9545550208")
                                  for l in fresh["leads"]), fresh)

        # shift_campaign moves a lead to another campaign and nothing
        # else: status stays new, opted_out stays 0, and the call is
        # recorded. The lead leaves the delivery queue and shows up
        # under the restaurant filter instead.
        code, bad_camp = record_call(lead_id=by_phone["9545550206"]["id"],
                                     outcome="shift_campaign",
                                     to_campaign="no_such_campaign")
        check("bot API recordCall shift_campaign bad campaign (400)",
              code == 400, bad_camp)
        code, no_camp = record_call(lead_id=by_phone["9545550206"]["id"],
                                    outcome="shift_campaign")
        check("bot API recordCall shift_campaign missing campaign (400)",
              code == 400, no_camp)
        code, shifted = record_call(lead_id=by_phone["9545550206"]["id"],
                                    outcome="shift_campaign",
                                    to_campaign="restaurant",
                                    note="restaurant, not delivery")
        check("recordCall shift_campaign -> campaign restaurant, status new, opted_out 0",
              code == 200 and shifted["lead"]["campaign"] == "restaurant"
              and shifted["lead"]["status"] == "new"
              and shifted["lead"]["opted_out"] == 0
              and shifted["call"]["outcome"] == "shift_campaign"
              and shifted["call"]["lead_id"] == by_phone["9545550206"]["id"]
              and shifted["call"]["note"] == "restaurant, not delivery", shifted)
        code, camps = c.jreq("GET", "/api/campaigns")
        check("campaigns list includes restaurant",
              code == 200 and "restaurant" in camps["campaigns"], camps)
        code, rest = v1.jreq("GET", "/api/v1/listLeads?campaign=restaurant&status=new",
                             headers={"Authorization": "Bearer " + api_key})
        check("shifted lead appears under the restaurant filter",
              code == 200 and len(rest["leads"]) == 1
              and rest["leads"][0]["phone"] == "9545550206", rest)
        code, dq = v1.jreq("GET", "/api/v1/listLeads?campaign=delivery&status=new",
                           headers={"Authorization": "Bearer " + api_key})
        check("shifted lead leaves the delivery queue (campaign=delivery&status=new)",
              code == 200 and all(l["phone"] != "9545550206"
                                  for l in dq["leads"]), dq)
        code, dash3 = c.jreq("GET", "/api/dashboard")
        check("dashboard campaign_counts includes restaurant",
              code == 200 and dash3["campaign_counts"].get("restaurant") == 1, dash3)
        code, hist2 = v1.jreq(
            "GET", "/api/v1/listCalls?lead_id=%d" % by_phone["9545550206"]["id"],
            headers={"Authorization": "Bearer " + api_key})
        check("listCalls shows the shift_campaign call",
              code == 200 and len(hist2["calls"]) == 2
              and hist2["calls"][0]["outcome"] == "shift_campaign"
              and hist2["calls"][0]["note"] == "restaurant, not delivery", hist2)

        # the allowlist is generic: any campaign already in leads is
        # accepted (delivery here), and restaurant is always accepted
        # even before it has leads. The dialer reads the list from the
        # API instead of hard-coding it.
        code, to_del = record_call(lead_id=by_phone["9545550207"]["id"],
                                   outcome="shift_campaign",
                                   to_campaign="delivery",
                                   note="staying in delivery")
        check("recordCall shift_campaign to delivery (existing campaign) accepted",
              code == 200 and to_del["lead"]["campaign"] == "delivery"
              and to_del["lead"]["status"] == "new"
              and to_del["lead"]["opted_out"] == 0, to_del)
        code, vcamps = v1.jreq("GET", "/api/v1/campaigns",
                               headers={"Authorization": "Bearer " + api_key})
        check("bot API v1 campaigns returns distinct campaigns",
              code == 200 and vcamps == {"campaigns": ["delivery", "djs", "restaurant"]},
              vcamps)
        code, _ = bad.jreq("GET", "/api/v1/campaigns")
        check("bot API v1 campaigns rejects missing key (401)", code == 401)
        code, _ = bad.jreq("GET", "/api/v1/campaigns",
                           headers={"Authorization": "Bearer crm_live_wrong"})
        check("bot API v1 campaigns rejects bad key (401)", code == 401)

        # create-on-shift: shifting to a brand-new VALID campaign name
        # creates the campaign — the lead moves, status stays new,
        # opted_out stays 0, and the new name shows up in the
        # campaigns list. The name is stored lowercased.
        code, created = record_call(lead_id=by_phone["9545550207"]["id"],
                                    outcome="shift_campaign",
                                    to_campaign="Pet-Grooming",
                                    note="new pet campaign")
        check("recordCall shift_campaign to brand-new campaign creates it",
              code == 200 and created["lead"]["campaign"] == "pet-grooming"
              and created["lead"]["status"] == "new"
              and created["lead"]["opted_out"] == 0
              and created["call"]["outcome"] == "shift_campaign", created)
        code, camps2 = v1.jreq("GET", "/api/v1/campaigns",
                               headers={"Authorization": "Bearer " + api_key})
        check("newly created campaign appears in GET /api/v1/campaigns",
              code == 200 and "pet-grooming" in camps2["campaigns"], camps2)
        code, pg = v1.jreq("GET",
                           "/api/v1/listLeads?campaign=pet-grooming&status=new",
                           headers={"Authorization": "Bearer " + api_key})
        check("shifted lead appears under the new campaign filter",
              code == 200 and len(pg["leads"]) == 1
              and pg["leads"][0]["phone"] == "9545550207", pg)

        # invalid new campaign names are still 400
        for bad_name in ("", "has space", "under_score", "-lead",
                         "trail-", "a" * 31):
            code, bad_name_res = record_call(
                lead_id=by_phone["9545550207"]["id"],
                outcome="shift_campaign", to_campaign=bad_name)
            check("recordCall shift_campaign invalid name %r (400)" % bad_name,
                  code == 400, bad_name_res)

        # v1.8: listLeads ordering is deterministic under updated_at
        # ties. updated_at is second-precision and bulk imports share
        # timestamps, so without an id tiebreak tied rows can come back
        # in a different order on every page fetch and offset paging
        # can recycle or skip rows. The tiebreak alone does NOT fix
        # the skip when a row leaves the filtered set mid-pass (the
        # dialer must count only still-matching leads in its offset);
        # it only guarantees identical data pages identically.
        code, imp_tie = v1.jreq("POST", "/api/v1/importLeadsCsv",
                                {"csv": "name,phone\n"
                                        "Tie One,9545550301\n"
                                        "Tie Two,9545550302\n"
                                        "Tie Three,9545550303\n"
                                        "Tie Four,9545550304\n"
                                        "Tie Five,9545550305\n",
                                 "importedBy": "mybot"},
                                headers={"Authorization": "Bearer " + api_key})
        check("bot API import leads for tiebreak tests",
              code == 200 and imp_tie.get("imported") == 5, imp_tie)
        import sqlite3 as _sqlite3
        _conn = _sqlite3.connect(db_path)
        _conn.execute("UPDATE leads SET updated_at='2026-11-22T00:00:00Z' "
                      "WHERE phone IN ('9545550301','9545550302','9545550303',"
                      "'9545550304','9545550305')")
        _conn.commit()
        tie_ids = [r[0] for r in _conn.execute(
            "SELECT id FROM leads WHERE phone IN ('9545550301','9545550302',"
            "'9545550303','9545550304','9545550305') ORDER BY id").fetchall()]
        _conn.close()
        code, tie_list = v1.jreq(
            "GET", "/api/v1/listLeads?campaign=delivery&status=new&limit=500",
            headers={"Authorization": "Bearer " + api_key})
        tie_order = [l["id"] for l in tie_list["leads"]
                     if l["id"] in set(tie_ids)]
        check("listLeads breaks updated_at ties by id DESC",
              code == 200 and tie_order == sorted(tie_ids, reverse=True),
              tie_order)
        # paging across the whole delivery+new set with limit=2: every
        # row exactly once, pages concatenated == one full fetch
        code, full = v1.jreq(
            "GET", "/api/v1/listLeads?campaign=delivery&status=new&limit=500",
            headers={"Authorization": "Bearer " + api_key})
        full_ids = [l["id"] for l in full["leads"]]
        paged_ids = []
        offset = 0
        ok_pages = True
        while True:
            code, pg = v1.jreq(
                "GET",
                "/api/v1/listLeads?campaign=delivery&status=new&limit=2&offset=%d"
                % offset,
                headers={"Authorization": "Bearer " + api_key})
            if code != 200:
                ok_pages = False
                break
            paged_ids.extend(l["id"] for l in pg["leads"])
            if len(pg["leads"]) < 2:
                break
            offset += 2
        check("listLeads offset paging under ties: no dupes, no skips, same order as full fetch",
              ok_pages and paged_ids == full_ids,
              (len(paged_ids), len(full_ids)))

        # migration: an old-schema DB without the website column gains it
        # on init_schema, and an import then stores a website
        mig_db = os.path.join(tmp, "old.db")
        mig_script = os.path.join(tmp, "migrate_check.py")
        with open(mig_script, "w") as fh:
            fh.write(
                "import json, os, sqlite3, sys\n"
                "sys.path.insert(0, %r)\n" % HERE +
                "db = os.environ['MIG_DB']\n"
                "c = sqlite3.connect(db)\n"
                "c.execute(\"CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT DEFAULT '', business TEXT DEFAULT '', phone TEXT DEFAULT '', "
                "email TEXT DEFAULT '', city TEXT DEFAULT '', source TEXT DEFAULT '', "
                "notes TEXT DEFAULT '', status TEXT DEFAULT 'new', prev_status TEXT, "
                "opted_out INTEGER DEFAULT 0, created_at TEXT DEFAULT '', "
                "updated_at TEXT DEFAULT '', last_texted_at TEXT)\")\n"
                "c.commit(); c.close()\n"
                "os.environ['CRM_DB'] = db\n"
                "import crm\n"
                "conn = crm.db()\n"
                "cols = [r[1] for r in conn.execute('PRAGMA table_info(leads)')]\n"
                "res = crm.import_csv_text('business name,phone number,email,website,contact name,generated by,campaign\\r\\nOld Shop,9545550113,old@example.com,oldshop.example.com,Ollie Old,LeadBot,djs\\r\\n', 'old.csv', 'drive-drop')\n"
                "row = conn.execute('SELECT website, source, campaign FROM leads WHERE phone=?', ('9545550113',)).fetchone()\n"
                "print(json.dumps({'migrated': 'website' in cols and 'campaign' in cols, 'imported': res.get('imported'), 'website': row[0] if row else None, 'source': row[1] if row else None, 'campaign': row[2] if row else None}))\n")
        mig = subprocess.run([sys.executable, mig_script],
                             env=dict(os.environ, MIG_DB=mig_db),
                             capture_output=True, text=True, timeout=30)
        mig_out = {}
        try:
            mig_out = json.loads([ln for ln in mig.stdout.splitlines() if ln.strip()][-1])
        except Exception:
            mig_out = {"error": mig.stdout + mig.stderr}
        check("migration adds website column to old DB",
              mig_out.get("migrated") is True, mig_out)
        check("import into migrated DB stores website and generated-by source",
              mig_out.get("imported") == 1
              and mig_out.get("website") == "oldshop.example.com"
              and mig_out.get("source") == "LeadBot", mig_out)
        check("migration adds campaign column and import stores it",
              mig_out.get("campaign") == "djs", mig_out)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    print("\n%d passed, %d failed" % (len(passed), len(failed)))
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
