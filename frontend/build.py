#!/usr/bin/env python3
"""Build the static results page into frontend/dist/ (Python 3 standard library only).

Copies eval/results/*.json and script/demo/deployments-1301.json into dist/data/,
writes dist/data/manifest.json (one row per result file: id, file, section, badge),
copies index.html, app.js and style.css, and fails on banned words in the page source.

Preview: python3 frontend/build.py && python3 -m http.server -d frontend/dist 8000
"""
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = HERE / "dist"
DATA = DIST / "data"
RESULTS = ROOT / "eval" / "results"
DEPLOY = ROOT / "script" / "demo" / "deployments-1301.json"
PAGE_FILES = ["index.html", "app.js", "style.css"]
BANNED = ["illustrative", "lorem", "todo", "placeholder", "replay"]
# Wording the page must never use (whole words; a bare passed/total ratio such as 104/106).
BANNED_WORDS = [r"\baudited\b", r"\bsecure\b", r"\bguaranteed\b", r"all claims pass",
                r"\b\d+/\d+\b"]

# Rows of the main results table, in display order. Every other result file is
# rendered in the collapsed history section, so no file is ever dropped.
MAIN = ["E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "U-IC2", "E-IC", "E0b"]


def fail(msg):
    print("build: FAIL: " + msg, file=sys.stderr)
    sys.exit(1)


def badge(doc):
    """Badge text, straight from the verdict field."""
    v = doc.get("verdict")
    if v is None:
        return None
    if v == "FAIL" and str(doc.get("fail_reason", "")).startswith("control form"):
        return "FAIL (control form)"
    if v == "FAIL" and "withdrawn" in str(doc.get("wording_fail", "")):
        return "FAIL · claim withdrawn"
    return {"NOT_RUN": "NOT RUN"}.get(v, v)


def banned_check():
    for name in ("index.html", "app.js"):
        text = (HERE / name).read_text(encoding="utf-8").lower()
        for word in BANNED:
            if word in text:
                line = text[: text.index(word)].count("\n") + 1
                fail(f"{name}:{line} contains the banned word '{word}'")
        for pat in BANNED_WORDS:
            m = re.search(pat, text)
            if m:
                line = text[: m.start()].count("\n") + 1
                fail(f"{name}:{line} contains the banned wording '{m.group(0)}'")


def main():
    banned_check()
    if DIST.exists():
        shutil.rmtree(DIST)
    DATA.mkdir(parents=True)

    rows = []
    for path in sorted(RESULTS.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        rid = doc.get("id") or doc.get("eval") or path.stem
        if rid != path.stem:
            fail(f"{path.name}: id '{rid}' does not match the file name")
        shutil.copyfile(path, DATA / path.name)
        rows.append({
            "id": rid,
            "file": path.name,
            "section": "main" if rid in MAIN else "history",
            "verdict": doc.get("verdict"),
            "badge": badge(doc),
        })
    missing = [m for m in MAIN if m not in {r["id"] for r in rows}]
    if missing:
        fail("result files missing for " + ", ".join(missing))
    rows.sort(key=lambda r: (r["section"] != "main",
                             MAIN.index(r["id"]) if r["id"] in MAIN else 0, r["id"]))

    shutil.copyfile(DEPLOY, DATA / DEPLOY.name)
    manifest = {"results": rows, "deployments": DEPLOY.name,
                "repo": "https://github.com/mishoko/touchstone/blob/main/"}
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")

    for name in PAGE_FILES:
        shutil.copyfile(HERE / name, DIST / name)
    print(f"build: ok, {len(rows)} result files, dist at {DIST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
