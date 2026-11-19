#!/usr/bin/env python3
"""Check the built page's data (run after build.py; standard library only).

Asserts: every result file in eval/results/ is in dist/data/manifest.json and copied;
every ID has a renderer in app.js; badges follow the verdict field; E5 shows
"FAIL (control form)"; E0b shows "FAIL · claim withdrawn"; the history section holds U-IC, E0, E0a.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = HERE / "dist"
errors = []


def check(ok, msg):
    if not ok:
        errors.append(msg)


man_path = DIST / "data" / "manifest.json"
if not man_path.exists():
    sys.exit("check: FAIL: run python3 frontend/build.py first")
man = json.loads(man_path.read_text(encoding="utf-8"))
rows = {r["id"]: r for r in man["results"]}
app = (DIST / "app.js").read_text(encoding="utf-8")
renderers = set(re.findall(r'^    "([A-Za-z0-9-]+)": function', app, re.M))

# s28 fix review (F-01..F-03, m-06): no free-text data field is rendered verbatim (it carries internal identifiers);
# the D1 E5 sentence and the testnet base rate are on the page
html = (DIST / "index.html").read_text(encoding="utf-8")
for field in ("fail_reason", "disclosed_unasserted_tails", "single_builder_bound", "d.fails", "d.note"):
    check(field not in app, f"app.js renders the data field {field} verbatim")
for word in ("§", "M_N1", "tau_v", "GRID_SEC", "cells.*"):
    check(word not in app and word not in html, f"internal identifier on the page: {word}")
check("cells have a control test that profits with the defence removed" in app, "E5: the D1 control sentence is missing")
check("provisional base rate of 1% a year" in html, "testnet: the base rate note is missing")

for path in sorted((ROOT / "eval" / "results").glob("*.json")):
    rid = path.stem
    check(rid in rows, f"{rid}: missing from the manifest")
    check((DIST / "data" / path.name).exists(), f"{rid}: data file not copied")
    check(rid in renderers, f"{rid}: no key-number renderer in app.js")
    if rid in rows:
        verdict = json.loads(path.read_text(encoding="utf-8")).get("verdict")
        check(rows[rid]["verdict"] == verdict, f"{rid}: manifest verdict differs from the file")
        badge = rows[rid]["badge"] or ""
        if verdict:
            check(badge.startswith(verdict.replace("_", " ")), f"{rid}: badge '{badge}' not from verdict {verdict}")

check(rows.get("E5", {}).get("badge") == "FAIL (control form)", "E5 does not show 'FAIL (control form)'")
check(rows.get("E0b", {}).get("badge") == "FAIL · claim withdrawn", "E0b does not show 'FAIL · claim withdrawn'")
check(rows.get("E-IC", {}).get("badge") == "NOT RUN", "E-IC does not show 'NOT RUN'")
for rid in ("U-IC", "E0", "E0a"):
    check(rows.get(rid, {}).get("section") == "history", f"{rid}: not in the history section")
check(rows.get("U-IC2", {}).get("section") == "main", "U-IC2: not in the main table")
check((DIST / "data" / man["deployments"]).exists(), "deployments file not copied")
check("reported_unasserted_tails_wei" in app and "parkingWhileTheCacheLagsIsBoundedByItsInRangeTime" in app, "app.js does not render the E5 tails (all four, incl. M-N1 lane B)")
check(not re.search(r"<script[^>]+src=\"https?:", (DIST / "index.html").read_text()), "external script")

if errors:
    print("check: FAIL\n  " + "\n  ".join(errors), file=sys.stderr)
    sys.exit(1)
print(f"check: ok, {len(rows)} result IDs rendered; E5 badge 'FAIL (control form)'")
