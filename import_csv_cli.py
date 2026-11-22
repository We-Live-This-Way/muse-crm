#!/usr/bin/env python3
"""Import a lead CSV file into the CRM database directly.

Handy for scheduled imports: copy a CSV to the CRM server and run
this script there (e.g. over SSH), so no API key has to live in any schedule.
It reuses crm.import_csv_text, so headers, dedupe-by-phone, and row
validation behave exactly like a UI or API import.

Usage:
    python3 import_csv_cli.py <file.csv> [--imported-by NAME]

Prints the import result as JSON. Exit 0 when the file was processed
(even if some rows were invalid); exit 2 on usage / read errors.
The database is the one next to crm.py (or $CRM_DB when set).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import crm  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Import a lead CSV into the CRM")
    ap.add_argument("csv_file", help="CSV file to import")
    ap.add_argument("--imported-by", default="drive-drop",
                    help="value recorded as the import source")
    args = ap.parse_args()
    try:
        with open(args.csv_file, "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError as exc:
        print("ERROR: cannot read %s: %s" % (args.csv_file, exc),
              file=sys.stderr)
        return 2
    result = crm.import_csv_text(text, os.path.basename(args.csv_file),
                                 args.imported_by)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
