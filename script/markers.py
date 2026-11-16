#!/usr/bin/env python3
"""No mutation markers in the tree. `script/mutate.py` writes `<file>.mutorig` (the original of the file it mutates)
and `output/MUTATION_IN_PROGRESS` while a mutant is applied and removes both when it restores the file. Either one
left in the tree means a mutated source may be committed or tested; this check fails (exit 1) if any is present.

  python3 script/markers.py [--root <dir>]
"""
import os, sys

SKIP = {'.git', 'lib', 'out', 'cache', 'out-oversized', 'cache-oversized', 'node_modules', 'broadcast'}


def find(root):
    hits = []
    for d, ds, fs in os.walk(root):
        ds[:] = [x for x in ds if x not in SKIP]
        for f in fs:
            if f.endswith('.mutorig') or f == 'MUTATION_IN_PROGRESS':
                hits.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(hits)


def main(argv):
    root = argv[argv.index('--root') + 1] if '--root' in argv else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = find(root)
    for h in hits:
        print('mutation marker:', h)
    print('markers', 'OK' if not hits else 'FAIL')
    return 1 if hits else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
