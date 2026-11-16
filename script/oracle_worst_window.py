#!/usr/bin/env python3
"""The reference reads whose gas is largest on a full Uniswap v3 oracle ring.

A v3 `observe([a, b])` runs, for each target older than the newest observation, a binary search over the ring; its gas
is dominated by the cold storage slots it reads. On a full ring of `n` observations `spacing` seconds apart (newest
one second old, index n - 1, oldest in slot 0), this script replays that search for every window the hook can read and
reports the windows that read the most distinct slots:
  - anchored reads: observe([s + REF_WINDOW, s]) for s in [0, GRID_SEC)        (the snapshot's refresh, `_readAnchor`)
  - markout reads:  observe([b + H, b]) for every age b the ring still covers  (`tryWindow` at a late settlement)
The G-1b fork test measures these windows on the real reference pool and sizes REF_GAS_CAP at 1.5x the worst.

  python3 script/oracle_worst_window.py [n] [spacing] [REF_WINDOW] [GRID_SEC] [H_MARKOUT]   (defaults: mainnet tier)
"""
import sys


def main(n=65535, spacing=12, ref_window=600, grid_sec=3600, h=120):
    def ts(j):  # observation j's timestamp relative to now
        return -1 - (n - 1 - j) * spacing

    cache = {}

    def path(ago):
        if ago in cache:
            return cache[ago]
        t = -ago
        if t >= ts(n - 1):
            s = {n - 1}
        else:
            # getSurroundingObservations reads the newest and the oldest, then binarySearch
            s, lo, hi = {n - 1, 0}, 0, n - 1
            while True:
                i = (lo + hi) // 2
                s |= {i % n, (i + 1) % n}
                before, after = ts(i % n), ts((i + 1) % n)
                if before <= t <= after:
                    break
                if before > t:
                    hi = i - 1
                else:
                    lo = i + 1
        cache[ago] = s
        return s

    best_a = max((len(path(s + ref_window) | path(s)), s + ref_window, s) for s in range(grid_sec))
    oldest_age = (n - 1) * spacing  # the oldest observation's age, less one second
    best_m = max((len(path(b + h) | path(b)), b + h, b) for b in range(1, oldest_age - h))
    print(f"n={n}: anchored worst {best_a[0]} slots at observe([{best_a[1]}, {best_a[2]}]); "
          f"markout worst {best_m[0]} slots at observe([{best_m[1]}, {best_m[2]}])")
    return best_a, best_m


if __name__ == '__main__':
    main(*[int(x) for x in sys.argv[1:]])
