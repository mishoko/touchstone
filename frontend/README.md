# Frontend

One self-contained page: `artifact/index.html` (inline CSS and JS; Google Fonts the only external request).
The same file is published as the hosted artifact and served by the Cloudflare Worker.

- Check: `python3 frontend/artifact_check.py` and `python3 frontend/artifact_check.py --selftest`
  (numbers, addresses and wording against `eval/results/`, `audits/`, `script/Tiers.sol`, `script/demo/deployments-1301.json`).
- Preview: `python3 -m http.server -d frontend/artifact 8000`
- Deploy: `cd frontend/worker && npx wrangler deploy` (serves `frontend/artifact/`; `.assetsignore` skips the generator and its data),
  or upload `artifact/index.html` alone.
- `artifact/gen_series.py` builds `series.json` and `series.txt` (the encoded price series the page embeds) and can re-verify samples on chain.
