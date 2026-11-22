#!/usr/bin/env python3
"""CRM morning attention digest — READ ONLY.

Prints a short plain-text report of what needs attention in the
Lead Outreach CRM: replies waiting on the operator, leads gone quiet after
being texted, and leads never texted. Never sends SMS, never changes
any lead status. Uses PRAGMA query_only so no write can happen.

Usage: python3 attention.py   (or set CRM_DB to the database path)
"""
import os
import sqlite3
from datetime import datetime

DB = os.environ.get("CRM_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "crm.db"))
SHOW = 10


def one_line(s, n=80):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def main():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA query_only=ON")
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    total = cur.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    out = []
    out.append("CRM attention - %s" % datetime.now().strftime("%a %Y-%m-%d"))
    out.append("")

    # 1. Waiting on the operator: replied (or taken over) with the latest inbound reply.
    rows = cur.execute(
        """
        SELECT l.business, l.phone,
               (SELECT m.body FROM messages m
                 WHERE m.lead_id = l.id AND m.direction = 'in'
                 ORDER BY m.created_at DESC LIMIT 1) AS reply,
               (SELECT m.created_at FROM messages m
                 WHERE m.lead_id = l.id AND m.direction = 'in'
                 ORDER BY m.created_at DESC LIMIT 1) AS reply_at
        FROM leads l
        WHERE LOWER(l.status) IN ('replied', 'taken over') AND l.opted_out = 0
        ORDER BY COALESCE(reply_at, '') DESC
        """
    ).fetchall()
    out.append("Waiting on the operator: %d" % len(rows))
    for r in rows[:SHOW]:
        out.append(
            '  - %s (%s): "%s" [%s]'
            % (
                r["business"] or "?",
                r["phone"],
                one_line(r["reply"]),
                (r["reply_at"] or "")[:10],
            )
        )
    if len(rows) > SHOW:
        out.append("  ... and %d more" % (len(rows) - SHOW))
    if not rows:
        out.append("  (none)")
    out.append("")

    # 2. Gone quiet: texted 3+ days ago, no inbound reply since.
    rows = cur.execute(
        """
        SELECT l.business, l.phone,
               CAST(julianday('now') - julianday(l.last_texted_at) AS INTEGER) AS days
        FROM leads l
        WHERE LOWER(l.status) = 'texted' AND l.opted_out = 0
          AND l.last_texted_at IS NOT NULL
          AND l.last_texted_at <= datetime('now', '-3 days')
          AND NOT EXISTS (
              SELECT 1 FROM messages m
              WHERE m.lead_id = l.id AND m.direction = 'in'
                AND m.created_at >= l.last_texted_at)
        ORDER BY l.last_texted_at ASC
        """
    ).fetchall()
    out.append("Gone quiet (texted 3+ days ago, no reply): %d" % len(rows))
    for r in rows[:SHOW]:
        out.append(
            "  - %s (%s): %d days ago" % (r["business"] or "?", r["phone"], r["days"])
        )
    if len(rows) > SHOW:
        out.append("  ... and %d more" % (len(rows) - SHOW))
    if not rows:
        out.append("  (none)")
    out.append("")

    # 3. Never texted: status new, oldest first.
    rows = cur.execute(
        """
        SELECT business, phone, created_at
        FROM leads
        WHERE LOWER(status) = 'new' AND opted_out = 0
        ORDER BY created_at ASC
        """
    ).fetchall()
    out.append("Never texted: %d" % len(rows))
    for r in rows[:SHOW]:
        out.append(
            "  - %s (%s) [%s]"
            % (r["business"] or "?", r["phone"], (r["created_at"] or "")[:10])
        )
    if len(rows) > SHOW:
        out.append("  ... and %d more" % (len(rows) - SHOW))
    if not rows:
        out.append("  (none)")
    out.append("")
    out.append("CRM total leads: %d" % total)
    print("\n".join(out))


if __name__ == "__main__":
    main()
