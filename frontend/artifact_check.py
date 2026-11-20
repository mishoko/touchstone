#!/usr/bin/env python3
"""Check the hosted page source frontend/artifact/index.html against the files its numbers come from.

  python3 frontend/artifact_check.py [page.html]     # default: frontend/artifact/index.html

Fails on: an unfilled %%slot%%; a banned word or wording (the lists of frontend/build.py, incl. a bare n/m ratio);
a link to /blob/main/; a result file in eval/results/ without its link on the page; mutation counts that differ from
audits/mutants.json; test counts that differ from audits/test-summary.json; DEMO-tier values that differ from
script/Tiers.sol demo(); an address or demo transaction of script/demo/deployments-1301.json missing from the page;
a limit title of frontend/app.js limits() missing; the E5 sentence of the binding wording missing.
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from build import BANNED, BANNED_WORDS  # noqa: E402

errors = []


def check(ok, msg):
    if not ok:
        errors.append(msg)


def main(argv):
    page = Path(argv[0]) if argv else HERE / "artifact" / "index.html"
    html = page.read_text(encoding="utf-8")
    low = html.lower()

    check("%%" not in html, "an unfilled %%slot%% remains")
    for w in BANNED:
        check(w not in low, f"banned word '{w}'")
    for pat in BANNED_WORDS:
        m = re.search(pat, low)
        check(not m, f"banned wording '{m.group(0) if m else ''}'")
    check("/blob/main/" not in html, "a source link points to /blob/main/ instead of a commit")

    for path in sorted((ROOT / "eval" / "results").glob("*.json")):
        check(f"eval/results/{path.name}\"" in html, f"{path.stem}: no link to its result file")

    muts = json.loads((ROOT / "audits" / "mutants.json").read_text())["mutants"]
    wired = [m for m in muts if m.get("status") != "EQUIVALENT" and m.get("kill") != "pending"]
    killed = [m for m in wired if m.get("status") == "KILLED"]
    eq = [m for m in muts if m.get("status") == "EQUIVALENT"]
    for label, n in (("listed", len(muts)), ("wired (in the denominator)", len(wired)),
                     ("killed by the named test", len(killed)), ("equivalent (no input can tell them apart), listed apart", len(eq))):
        check(f"<dt>{label}</dt><dd>{n}</dd>" in html, f"mutation: '{label}' is not {n}")
    check(f"{len(killed)} killed" in html, f"mutation: headline is not {len(killed)} killed")

    ts = json.loads((ROOT / "audits" / "test-summary.json").read_text())
    t = ts["total"]
    check(f"{t['passed']:,} passed" in html, f"tests: headline is not {t['passed']:,} passed")
    check(f"{t['failed']} failed. {t['skipped']} skipped." in html, "tests: failed/skipped differ")
    for d, row in ts["by_directory"].items():
        check(f"<dt>{d}</dt><dd>{row['passed']:,}</dd>" in html, f"tests: directory {d} is not {row['passed']}")
    for k, slot in (("fuzz", "by kind: fuzz"), ("invariant", "invariant"), ("plain", "plain")):
        check(f"<dt>{slot}</dt><dd>{ts['by_kind'][k]:,}</dd>" in html, f"tests: kind {k} differs")

    tiers = (ROOT / "script" / "Tiers.sol").read_text()
    demo = tiers[tiers.index("function demo()"):]
    demo = demo[: demo.index("\n    }")]
    v = {k: int(x) for k, x in re.findall(r"t\.(\w+) = (\d+);", demo)}
    for frag in (f"{v['REF_WINDOW']} s TWAP", f"{v['H_MARKOUT']} s window", f"{v['CURE_BLOCKS']:,} blocks (30 min)",
                 f"{v['EPOCH']:,} blocks (1 h)", f"cooldown       {v['COOLDOWN']} blocks"):
        check(frag in html, f"DEMO tier: '{frag}' not on the page (script/Tiers.sol demo())")

    dep = json.loads((ROOT / "script" / "demo" / "deployments-1301.json").read_text())
    for grp in ("deploy", "step0b"):
        for k, val in dep[grp].items():
            if isinstance(val, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", val):
                check(val in html, f"deployments: {k} {val} missing")
    for k, row in dep["demoflow"].items():
        if isinstance(row, dict):
            for f in ("tx", "settle_tx"):
                if row.get(f):
                    check(row[f] in html, f"demoflow: {k}.{f} missing")
    check(f"{dep['deploy']['m_perBlock']:,}" in html, "deploy: m_perBlock missing")

    app = (HERE / "app.js").read_text()
    for title in re.findall(r'item\("([^"]+)"', app):
        check(title.rstrip(".") in html, f"limit '{title}' of frontend/app.js missing")
    check("14 cells have a control test that profits with the defence removed" in html, "E5: the binding sentence is missing")
    check("FAIL · claim withdrawn" in html and "+ 1 withdrawn (E0b" in html, "E0b: withdrawn wording missing")

    if errors:
        print("artifact_check: FAIL\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(1)
    print(f"artifact_check: ok ({page.name})")


if __name__ == "__main__":
    main(sys.argv[1:])
