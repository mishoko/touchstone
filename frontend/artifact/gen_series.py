#!/usr/bin/env python3
"""Generator for the page's price series (hero and scenes 1, 3, 5).

Source: Uniswap v3 USDC/WETH 0.05% on mainnet (0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640),
blocks 26,357,020 to 26,443,420, one sample every 50 blocks (1,729 samples).
  spot_tick = slot0().tick at the block
  ref_tick  = (tickCumulative[now] - tickCumulative[now - 600 s]) / 600 from observe([600, 0]) at the block
              (the pool's own 10-minute TWAP; no approximation from samples)
Tracked beside the page: series.json is the series the page embeds; `--verify K` re-reads K samples on chain and
`--fetch` re-reads all of them (public RPC), so the page's prices trace to the chain, not to this file.
An earlier internal mock embedded exactly these columns; this script can
  --extract   rebuild series.json and series.txt beside this script from a local copy of that mock, or
  --verify K  re-read K evenly spaced samples on chain and compare (needs an archive RPC; default mevblocker),
  --fetch     re-read all 1,729 samples on chain into series.json (slow; not needed for the page).
series.txt encoding (what the page embeds): spot-tick deltas, then '|', then ref_tick - spot_tick (ref rounded).
"""
import json, re, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
POOL = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
B0, STEP, N = 26357020, 50, 1729
RPC = "https://rpc.mevblocker.io"


def rpc(method, params, url=RPC):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "series-gen"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["result"]


def s256(x):
    v = int(x, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def sample(block):
    obs = "0x883bdbfd" + "%064x" % 32 + "%064x" % 2 + "%064x" % 600 + "%064x" % 0
    w = rpc("eth_call", [{"to": POOL, "data": obs}, hex(block)])[2:]
    w = [w[i:i + 64] for i in range(0, len(w), 64)]
    ref = (s256(w[4]) - s256(w[3])) / 600
    s0 = rpc("eth_call", [{"to": POOL, "data": "0x3850c7bd"}, hex(block)])[2:]
    spot = s256(s0[64:128])
    return spot, ref


def write(st, rt):
    (HERE / "series.json").write_text(json.dumps({"st": st, "rt": [round(x) for x in rt]}))
    d = [st[0]] + [st[i] - st[i - 1] for i in range(1, len(st))]
    r = [round(rt[i]) - st[i] for i in range(len(st))]
    (HERE / "series.txt").write_text(",".join(map(str, d)) + "|" + ",".join(map(str, r)))


def mock_rows():
    s = (ROOT / "output" / "frontend-mock" / "v3.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="series">(.*?)</script>', s, re.S)
    return json.loads(m.group(1))["rows"]


def main(a):
    if a[:1] == ["--extract"]:
        rows = mock_rows()
        write([r[3] for r in rows], [r[4] for r in rows])
        print("extracted", len(rows))
    elif a[:1] == ["--verify"]:
        rows, k = mock_rows(), int(a[1]) if len(a) > 1 else 8
        worst = 0
        for j in range(k):
            i = j * (N - 1) // (k - 1)
            spot, ref = sample(B0 + STEP * i)
            assert rows[i][0] == B0 + STEP * i
            worst = max(worst, abs(spot - rows[i][3]), abs(ref - rows[i][4]))
            print(f"block {rows[i][0]}: spot {spot} vs {rows[i][3]}, ref {ref:.2f} vs {rows[i][4]}")
        print("worst abs difference (ticks):", round(worst, 4))
    elif a[:1] == ["--fetch"]:
        st, rt = [], []
        for i in range(N):
            s_, r_ = sample(B0 + STEP * i)
            st.append(s_); rt.append(r_)
        write(st, rt)
        print("fetched", N)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
