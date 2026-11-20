#!/usr/bin/env python3
"""Write audits/test-summary.json from a `forge test --json` output: test counts by directory and kind.

  FORK_BLOCK_1=26443420 forge test --json > <out.json>
  python3 script/test_summary.py <out.json> [--commit <sha>]     # writes audits/test-summary.json
  python3 script/test_summary.py --selftest

Each test is counted under its file's directory below test/ (unit, fuzz, manipulation, invariant, fork, deploy,
eval, storage) and by kind: invariant (`invariant_*`), fuzz (a test with fuzz runs), or plain. The file records
passed, failed and skipped in total, so a page that prints a count can cite this file.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "audits" / "test-summary.json"


def summarise(doc):
    by_dir, total = {}, {"passed": 0, "failed": 0, "skipped": 0}
    kinds = {"invariant": 0, "fuzz": 0, "plain": 0}
    for suite, body in doc.items():
        path = suite.split(":")[0]
        parts = Path(path).parts
        d = parts[1] if len(parts) > 2 and parts[0] == "test" else (parts[0] if parts else "?")
        row = by_dir.setdefault(d, {"passed": 0, "failed": 0, "skipped": 0, "suites": 0})
        row["suites"] += 1
        for name, res in (body.get("test_results") or {}).items():
            st = str(res.get("status", ""))
            key = "passed" if st == "Success" else "skipped" if st == "Skipped" else "failed"
            row[key] += 1
            total[key] += 1
            kind = res.get("kind") or {}
            if name.startswith("invariant"):
                kinds["invariant"] += 1
            elif isinstance(kind, dict) and "Fuzz" in kind:
                kinds["fuzz"] += 1
            else:
                kinds["plain"] += 1
    return {"total": total, "by_directory": dict(sorted(by_dir.items())), "by_kind": kinds}


def selftest():
    doc = {
        "test/unit/A.t.sol:A": {"test_results": {"test_a()": {"status": "Success", "kind": {"Unit": {"gas": 1}}},
                                                 "test_b(uint256)": {"status": "Success", "kind": {"Fuzz": {"runs": 256}}}}},
        "test/fork/K.t.sol:K": {"test_results": {"test_k()": {"status": "Skipped", "kind": {"Unit": {"gas": 1}}}}},
        "test/invariant/V.t.sol:V": {"test_results": {"invariant_x()": {"status": "Failure", "kind": {"Invariant": {}}}}},
    }
    s = summarise(doc)
    assert s["total"] == {"passed": 2, "failed": 1, "skipped": 1}, s
    assert s["by_directory"]["unit"]["passed"] == 2 and s["by_directory"]["fork"]["skipped"] == 1, s
    assert s["by_kind"] == {"invariant": 1, "fuzz": 1, "plain": 2}, s
    print("test_summary selftest OK")


def main(argv):
    if argv[:1] == ["--selftest"]:
        return selftest()
    if not argv:
        sys.exit(__doc__)
    text = Path(argv[0]).read_text(encoding="utf-8")
    start = text.index("{")  # forge may print compiler lines before the JSON
    doc = json.loads(text[start:])
    s = summarise(doc)
    commit = argv[argv.index("--commit") + 1] if "--commit" in argv else None
    s = {"command": "FORK_BLOCK_1=26443420 forge test --json", "commit": commit, **s}
    OUT.write_text(json.dumps(s, indent=1) + "\n", encoding="utf-8")
    t = s["total"]
    print(f"test_summary: {t['passed']} passed, {t['failed']} failed, {t['skipped']} skipped -> {OUT.relative_to(ROOT)}")
    if t["failed"] or t["skipped"]:
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
