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
check("disclosed_unasserted_tails" in app, "app.js does not render E5 disclosed_unasserted_tails")
check(not re.search(r"<script[^>]+src=\"https?:", (DIST / "index.html").read_text()), "external script")

if errors:
    print("check: FAIL\n  " + "\n  ".join(errors), file=sys.stderr)
    sys.exit(1)
print(f"check: ok, {len(rows)} result IDs rendered; E5 badge 'FAIL (control form)'")
