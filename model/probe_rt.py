"""Runtime for reachability probes (used by `mutants_model.py` to test equivalence claims).

An instrumented copy of `reference.py` calls these helpers at one mutation site. Each helper keeps the clean
semantics and records whether the site's distinguishing condition occurred, i.e. whether the mutant could have
behaved differently at that moment. Counts are written to the file named by $PROBE_OUT at exit.
"""
import atexit
import json
import os

HITS = {}


def _hit(key, cond):
    h = HITS.setdefault(key, [0, 0])
    h[0] += 1
    h[1] += 1 if cond else 0


def rel(key, i, ops, *vals):
    """A (possibly chained) comparison; the site is the i-th operator; distinguishing iff its operands are equal."""
    _hit(key, vals[i] == vals[i + 1])
    res = True
    for k, op in enumerate(ops):
        a, b = vals[k], vals[k + 1]
        res = res and {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "!=": a != b}[op]
    return res


def canon(x, depth=0):
    """A structural, comparable image of model state (objects by their attributes; the reference double skipped)."""
    if depth > 12:
        return "<deep>"
    if isinstance(x, (int, float, str, bool, type(None))):
        return x
    if isinstance(x, (list, tuple)):
        return tuple(canon(v, depth + 1) for v in x)
    if isinstance(x, set):
        return tuple(sorted(canon(v, depth + 1) for v in x))
    if isinstance(x, dict):
        return tuple(sorted((str(k), canon(v, depth + 1)) for k, v in x.items()))
    if callable(x) and not hasattr(x, "__dict__"):
        return "<fn>"
    d = getattr(x, "__dict__", None)
    if d is None and hasattr(x, "__slots__"):
        d = {k: getattr(x, k) for k in x.__slots__}
    if d is None:
        return repr(type(x))
    return (type(x).__name__,) + tuple(sorted((k, canon(v, depth + 1)) for k, v in d.items() if k not in ("ref", "fail", "tick_at")))


def opnd(key, orig, alt, as_bool=False):
    """An operand replacement; distinguishing iff the replacement evaluates differently (by truthiness for a
    boolean operator) or raises."""
    v = orig()
    try:
        a = alt()
        diff = (bool(a) != bool(v)) if as_bool else canon(a) != canon(v)
    except Exception:
        diff = True
    _hit(key, diff)
    return v


def snap(obj):
    return canon(obj)


def get(thunk):
    try:
        return canon(thunk())
    except Exception:
        return "<unset>"


def deleted(key, before, after):
    """A deleted statement; distinguishing iff the statement changed the state it writes."""
    _hit(key, before != after)


def raised(key):
    _hit(key, True)


@atexit.register
def _dump():
    out = os.environ.get("PROBE_OUT")
    if out:
        with open(out, "w") as fh:
            json.dump(HITS, fh)
