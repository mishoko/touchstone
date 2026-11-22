#!/usr/bin/env python3
"""Check the hosted page source frontend/artifact/index.html against the files its numbers come from.

  python3 frontend/artifact_check.py [page.html]     # default: frontend/artifact/index.html

Fails on: an unfilled %%slot%%; a banned word or wording (BANNED, BANNED_WORDS, incl. a bare n/m ratio);
a link to /blob/main/; a result file in eval/results/ without its link on the page; mutation counts that differ from
audits/mutants.json; test counts that differ from audits/test-summary.json; DEMO-tier values that differ from
script/Tiers.sol demo(); an address or demo transaction of script/demo/deployments-1301.json missing from the page;
a limit title of LIMIT_TITLES missing; the page not a standalone document (doctype, UTF-8 charset in
the first 1024 bytes, one head and body), an external script or an internal identifier; E5 without 'FAIL (control form)'
or E-IC without NOT RUN; the video walkthrough card or its nav link missing; the E5 sentence of the binding wording missing.
The old results page's manifest, renderer and dist/ checks were retired with that page (s32).
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BANNED = ["illustrative", "lorem", "todo", "placeholder", "replay"]
# Wording the page must never use (whole words; a bare passed/total ratio such as 104/106).
BANNED_WORDS = [r"\baudited\b", r"\bsecure\b", r"\bguaranteed\b", r"all claims pass",
                r"\b\d+/\d+\b"]
# Titles of the limits the page must show (formerly the old results page's limits() list).
LIMIT_TITLES = ["Lag leak", "Reported, not asserted", "E-IC was not run", "Allocation ledger claim withdrawn"]

# The E-IC supplement's label (panel D3) contains "not a replay"; only that exact phrase is exempt from the scan.
sys.path.insert(0, str(ROOT / "eval"))
from eic_hist import LABEL as SUPPLEMENT_LABEL  # noqa: E402
SUPPLEMENT = ROOT / "eval" / "supplements" / "E-IC-historical.json"


def scan_text(text):
    """Lower-cased text with the exact supplement label removed (the only allowed use of 'replay')."""
    return text.lower().replace(SUPPLEMENT_LABEL.lower(), "")

errors = []


def check(ok, msg):
    if not ok:
        errors.append(msg)


def ceiling_number(text):
    """S31EF-04: a number within 40 characters after "depositor ceiling" (whitespace and hyphens collapsed, a digit
    after a section sign ignored), or a decimal or percentage within 40 characters before it."""
    t = re.sub(r"[\s-]+", " ", text.lower())
    for m in re.finditer(r"depositor ceiling", t):
        after, before = t[m.end(): m.end() + 40], t[max(0, m.start() - 40): m.start()]
        after = re.split(r"\. |\|", after)[0]
        before = re.split(r"\. |\|", before)[-1]
        if re.search(r"(?<![§\d.])\d", after) or re.search(r"\d\.\d|\d ?%", before):
            return True
    return False


# Internal identifiers that must never reach the public page (s28 fix review F-01..F-03).
INTERNAL_IDS = ("§", "M_N1", "tau_v", "GRID_SEC", "cells.*")
SERIES_BLOCK = re.compile(r'<script type="text/plain" id="series">.*?</script>', re.S)


def document_errors(html):
    """Errors in the page as a standalone document: it is served raw by the Worker, so it must declare its own
    doctype and UTF-8 charset (else windows-1252 mojibake and quirks mode), load no external script, and carry
    no internal identifier outside the encoded price series."""
    errs = []
    if not html.lstrip().lower().startswith("<!doctype html>"):
        errs.append("document: does not start with <!doctype html>")
    if not re.search(r'<meta charset="?utf-8"?', html[:1024], re.I):
        errs.append("document: no <meta charset=\"utf-8\"> in the first 1024 bytes")
    if not (html.count("<head>") == html.count("<body>") == 1 and html.find("</head>") < html.find("<body>")):
        errs.append("document: not exactly one <head> followed by one <body>")
    if re.search(r'<script[^>]+src="https?:', html):
        errs.append("document: loads an external script")
    text = SERIES_BLOCK.sub("", html)
    for w in INTERNAL_IDS:
        if w in text:
            errs.append(f"internal identifier on the page: {w}")
    return errs


def selftest():
    assert ceiling_number("the depositor\nceiling is 0.84")
    assert ceiling_number("0.84 of time, the depositor-ceiling input")
    assert not ceiling_number("The depositor ceiling (spec §3.5) is not measured.")
    assert not ceiling_number("the curator p5 condition or the depositor ceiling | — |")
    good = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>t</title>\n</head>\n<body>\n'
            '<script type="text/plain" id="series">§tau_v</script>\n</body>\n</html>\n')
    assert document_errors(good) == [], document_errors(good)
    bare = good.split("<title>", 1)[1].replace("</head>\n<body>\n", "")   # the pre-fix fragment shape
    assert any("doctype" in e for e in document_errors(bare)) and any("charset" in e for e in document_errors(bare))
    assert document_errors(good.replace("<title>t", '<script src="https://x.example/a.js"></script><title>t'))
    assert document_errors(good.replace("</body>", "<p>tau_v</p></body>"))
    print("artifact_check selftest OK")


def main(argv):
    if argv[:1] == ["--selftest"]:
        return selftest()
    page = Path(argv[0]) if argv else HERE / "artifact" / "index.html"
    html = page.read_text(encoding="utf-8")
    low = scan_text(html)   # the exact E-IC supplement label is the only allowed "replay"

    check("%%" not in html, "an unfilled %%slot%% remains")
    for e in document_errors(html):
        check(False, e)
    check("FAIL (control form)" in html, "E5: 'FAIL (control form)' missing")
    check(re.search(r'<a class="watch" id="video" href="https://youtu\.be/[\w-]{11}"', html) is not None
          and 'href="#video"' in html, "video: the walkthrough card or its nav link is missing")
    check(re.search(r'E-IC\.json">E-IC</a>[^\n]*?NOT RUN', html) is not None, "E-IC: the row does not show NOT RUN")
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
    a3 = dep["demoflow"].get("A3_rebalanceSwap_charged", {})   # S31VF-01: who paid A3's burn, from its Action event
    if a3.get("burned_bond_shares") and int(a3["burned_bond_shares"]) > int(a3.get("burned_bank_shares", 0)):
        check("most of it from the bond" in html, "A3: the page does not say most of the burn came from the bond")

    for title in LIMIT_TITLES:
        check(title in html, f"limit '{title}' missing")
    check("14 cells have a control test that profits with the defence removed" in html, "E5: the binding sentence is missing")
    check("FAIL · claim withdrawn" in html and "+ 1 withdrawn (E0b" in html, "E0b: withdrawn wording missing")

    # s30 fix review (S30AF-01/02/04/06): the U-IC2 vault values are the no-observer row; no "never charged" claim;
    # hidden tab panels stay hidden; the area counts are the listed counts of mutants.json
    nav = json.loads((ROOT / "eval" / "results" / "U-IC2.json").read_text())["reported"]["nav_end"]["gbm-120"]["no-observer"]
    vals = [round(nav[k]["mean"]) for k in ("breach-reset", "dodge", "edge-12h")]
    check(f"Without one, the vault ends with more value under those lax strategies in every volatility bucket "
          f"(at 120%: about {vals[0]} and {vals[1]} against {vals[2]}" in html, f"U-IC2: no-observer vault values {vals} not stated as such")
    check("never charged to you" not in low, "a depositor benefit claims third parties are never charged to them")
    if 'role="tabpanel"' in html and " hidden>" in html:
        check(".panel[hidden] { display: none; }" in html, "tab panels: the [hidden] rule is missing (all panels render)")
    areas = {}
    for m in muts:
        areas[m["area"]] = areas.get(m["area"], 0) + 1
    shown = {"guards": "listed, by area: guards", "ledger": "ledger (the charge)", "ladder": "ladder (breaches, vesting)",
             "bank": "bank (pay, bond, forfeits)", "records": "records", "witness": "witness"}
    for a, label in shown.items():
        check(f"<dt>{label}</dt><dd>{areas.get(a, 0)}</dd>" in html, f"mutation area {a} is not {areas.get(a, 0)}")
    other = len(muts) - sum(areas.get(a, 0) for a in shown)
    check(f"<dt>other areas</dt><dd>{other}</dd>" in html, f"mutation: other areas is not {other}")

    # s31 (S31ED-01/04): the E-IC supplement's line on the page and in README is its result file's public_line;
    # no depositor-ceiling number is published (F-025 open)
    readme = (ROOT / "README.md").read_text(encoding="utf-8") + "\n" + (ROOT / "docs" / "DETAILS.md").read_text(encoding="utf-8")   # s32: README + its full record
    if SUPPLEMENT.exists():
        line = json.loads(SUPPLEMENT.read_text())["public_line"]
        check(line in readme, "README/DETAILS: the E-IC supplement line differs from its result file's public_line")
        if json.loads(SUPPLEMENT.read_text())["reviews"].get("fix"):   # S31EF-05: the link shows the reviewed file
            check("b46accc" not in html, "page: the supplement link still points at the unreviewed result commit")
        check(line.replace("&", "&amp;").replace("'", "&#39;") in html or line in html,
              "page: the E-IC supplement line differs from its result file's public_line")
    else:
        check("E-IC historical supplement" not in html and "E-IC historical supplement" not in readme,
              "the E-IC supplement is mentioned without its result file")
    for text, name in ((low, "page"), (readme.lower(), "README/DETAILS")):
        check(not ceiling_number(text), f"{name}: a depositor-ceiling number (none is measured)")
    if SUPPLEMENT.exists():   # S31EF-04: a supplement net never appears outside its labelled line
        sd = json.loads(SUPPLEMENT.read_text())
        line = sd["public_line"]
        for nav in ("1850", "185"):
            v = f'{sd["by_nav"][nav]["rows"]["no-observer"]["edge-12h"]["net"]:+.3f}'
            for text, name in ((html, "page"), (readme, "README/DETAILS")):
                check(text.count(v) <= text.count(line), f"{name}: the supplement net {v} appears outside its labelled line")
        cited = round(sd["by_nav"]["1850"]["rows"]["no-observer"]["edge-12h"]["nav_end"]), \
            round(sd["by_nav"]["1850"]["rows"]["no-observer"]["lazy"]["nav_end"])
        check(f"about {cited[0]:,} against {cited[1]:,} ETH from 1,850" in " ".join(readme.split()),
              f"README/DETAILS: the supplement's end-NAV clause is not {cited} (S31EF-03)")
        check(f"{cited[0]:,} against {cited[1]:,} ETH from 1,850" in " ".join(html.split()),
              f"page: the supplement's end-NAV clause is not {cited} (S31VD-02)")

    # s32 (S32PD-11): the deal section's numbers are U-IC2's buckets, U-IC2-agg's bond burn and E8's charges
    m = re.search(r"var D = (\{.*?\});", html, re.S)
    check(m is not None or 'id="deal"' not in html, "deal: the data table var D is missing")
    if m:
        deal = json.loads(m.group(1))
        u = json.loads((ROOT / "eval" / "results" / "U-IC2.json").read_text())["buckets"]
        e8 = json.loads((ROOT / "eval" / "results" / "E8.json").read_text())["items"]["reference_charge_distribution"]["value"]
        agg = {r["bucket"]: r for r in json.loads((ROOT / "eval" / "supplements" / "U-IC2-agg.json").read_text())["rows"]
               if r["bot"] == "edge-12h" and abs(r["m"] - 0.022) < 1e-9 and not r["observer"]}
        for b, row in deal.items():
            want = [round(u[b]["mimic_mean"], 2), round(u[b]["mimic_p5"], 2), round(u[b]["cmp"]["breach-reset"]["bot_mean"], 2),
                    round(u[b]["cmp"]["lazy"]["bot_mean"], 2), round(u[b]["cmp"]["idle"]["bot_mean"], 2)]
            check(row[:5] == want, f"deal {b}: {row[:5]} is not U-IC2 {want}")
            check(max(c["bot_mean"] for c in u[b]["cmp"].values()) == u[b]["cmp"]["breach-reset"]["bot_mean"],
                  f"deal {b}: breach-reset is not the best shortcut")
            check(row[5] == f"{e8[b]['charges_bps_of_nav_per_year']:.1f} bps", f"deal {b}: charge {row[5]} is not E8")
            check(row[6] == f"{agg[b]['bond_burn_mean']:.3f} ETH", f"deal {b}: bond burn {row[6]} is not U-IC2-agg")
        check("hover-60" not in deal, "deal: the hover path is reported only, not gated (S32PD-03)")
    # s32 fix review (S32PF-02): the testnet vault has no outside depositors; the cards say so and claim no depositor gain
    if 'id="onchain"' in html:
        check("This test vault has no outside depositors" in html, "on-chain: the no-outside-depositors scope line is missing")
        check("Depositors' balances did not pay" not in html and "depositors' shares were not touched" not in html,
              "on-chain: a card claims a depositor protection the testnet run cannot show")

    if errors:
        print("artifact_check: FAIL\n  " + "\n  ".join(errors), file=sys.stderr)
        sys.exit(1)
    print(f"artifact_check: ok ({page.name})")


if __name__ == "__main__":
    main(sys.argv[1:])
