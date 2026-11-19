# Frontend worker note (P-24)

Built: a static results page. `build.py` copies `eval/results/*.json` and `script/demo/deployments-1301.json` into `dist/data/`, writes `manifest.json` (badge from each `verdict`), and fails on banned words. `app.js` renders everything from those files. No external scripts or fonts.

Preview: `python3 frontend/build.py && python3 frontend/check.py && python3 -m http.server -d frontend/dist 8000`

Checked: headless Chrome render (desktop; 360 px in an iframe, no horizontal scroll). Light mode not eyeballed.

Open questions:
- Question wording per ID is our own short copy in `app.js` (the files' `question` fields mention a historical replay).
- E5's `disclosed_unasserted_tails` is shown verbatim; it is long and technical (GRID, tau_v).
- U-IC.json holds a bare `Infinity`; the loader reads it as a string.
- E0b-fit (no verdict) sits in the history section as "no verdict".
- `check.py` checks the manifest and renderers, not the live DOM (no JS engine in the stdlib).
- Banned-word check also rejects `audited`, `secure`, `guaranteed` and bare `n/m` ratios.
