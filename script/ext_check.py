#!/usr/bin/env python3
"""Checks of the per-vault extension (spec §4.12, lever (c); §9.1 U-ext (e), (g), (h)).

(e) ABI: the allow-list (the functions of `IVaultEntries`) does not meet the vault's ABI (a vault function with an
    allow-listed selector would shadow the forwarded entry point); the vault's fallback names exactly the
    allow-list; the extension's non-view ABI is the allow-list plus `EXTRA` (its own entries reached only from the
    vault's `unlockCallback`) plus the inherited ERC-20 functions (`ERC20_EXEMPT`: called directly they act on the
    extension's own empty storage).
(g) Layout: the extension's flattened storage layout is identical to the vault's (one `VaultStorage`).
(h) Bytecode: the extension's runtime has no SELFDESTRUCT or CALLCODE, and DELEGATECALL only to the linked libraries
    (the extension's sources contain no `delegatecall`, so every DELEGATECALL in its bytecode is a library call;
    the contract must then have link references).

  python3 script/ext_check.py             # all three checks (exit 1 on any failure)
  python3 script/ext_check.py --selftest  # each check against a planted defect it must catch, and a clean control
"""
import json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'script'))
from storage_layout import inspect as layout_of  # noqa: E402

VAULT, EXT, ENTRIES = 'TouchstoneVault', 'TouchstoneVaultExtension', 'IVaultEntries'
EXTRA = {'dispatch'}  # P4 adds `roll` here with the record write
ERC20_EXEMPT = {'transfer', 'approve', 'transferFrom'}
EXT_SOURCES = ('src/TouchstoneVaultExtension.sol', 'src/TouchstoneVaultBase.sol')
OP_SELFDESTRUCT, OP_CALLCODE, OP_DELEGATECALL = 0xFF, 0xF2, 0xF4


def forge_inspect(c, field):
    r = subprocess.run(['forge', 'inspect', c, field, '--json'], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f'ext_check: forge inspect {c} {field} failed:\n{r.stderr[-1500:]}')
    return json.loads(r.stdout)


def sig(f):
    return f"{f['name']}({','.join(_t(i) for i in f['inputs'])})"


def _t(i):
    t = i['type']
    if t.startswith('tuple'):
        return '(' + ','.join(_t(c) for c in i['components']) + ')' + t[len('tuple'):]
    return t


def funcs(abi):
    return [f for f in abi if f.get('type') == 'function']


# ------------------------------------------------------------------ (e)
def check_abi(vault_abi, ext_abi, entries_abi, fallback_names):
    errs = []
    allow = {sig(f) for f in funcs(entries_abi)}
    allow_names = {f['name'] for f in funcs(entries_abi)}
    vault_sigs = {sig(f) for f in funcs(vault_abi)}
    for s in sorted(allow & vault_sigs):
        errs.append(f'(e) the vault defines allow-listed {s}: it would shadow the forwarded entry point')
    if fallback_names != allow_names:
        errs.append(f'(e) the vault fallback forwards {sorted(fallback_names)}, the allow-list is {sorted(allow_names)}')
    nonview = {f['name'] for f in funcs(ext_abi) if f.get('stateMutability') not in ('view', 'pure')}
    want = allow_names | EXTRA | ERC20_EXEMPT
    for n in sorted(nonview - want):
        errs.append(f'(e) the extension has a non-view function {n} outside the allow-list, {sorted(EXTRA)} and the ERC-20 set')
    for n in sorted(want - nonview):
        errs.append(f'(e) the extension lacks the non-view function {n}')
    return errs


def fallback_names(src):
    body = src[src.index('fallback(bytes calldata'):]
    body = body[:body.index('return _delegate(data);')]
    return set(re.findall(r'IVaultEntries\.(\w+)\.selector', body))


# ------------------------------------------------------------------ (g)
def check_layout(vault_layout, ext_layout):
    if vault_layout != ext_layout:
        return ['(g) the extension storage layout differs from the vault layout']
    return []


# ------------------------------------------------------------------ (h)
def opcodes(code):
    """The opcodes of a runtime (a linear sweep that skips PUSH data; the CBOR metadata tail is stripped)."""
    if len(code) >= 2:
        n = int.from_bytes(code[-2:], 'big')
        if n + 2 <= len(code):
            code = code[:-(n + 2)]
    i, out = 0, []
    while i < len(code):
        op = code[i]
        out.append(op)
        i += 1 + (op - 0x5F if 0x60 <= op <= 0x7F else 0)
    return out


def check_bytecode(code, link_refs, sources):
    errs = []
    ops = opcodes(code)
    if OP_SELFDESTRUCT in ops:
        errs.append('(h) the extension runtime contains SELFDESTRUCT')
    if OP_CALLCODE in ops:
        errs.append('(h) the extension runtime contains CALLCODE')
    for path, text in sources.items():
        if re.search(r'\bdelegatecall\b', re.sub(r'//[^\n]*|/\*.*?\*/', '', text, flags=re.S)):
            errs.append(f'(h) {path} contains delegatecall: only linked-library calls may DELEGATECALL')
    if OP_DELEGATECALL in ops and not link_refs:
        errs.append('(h) the extension runtime DELEGATECALLs but links no library')
    return errs


def runtime(c):
    r = subprocess.run(['forge', 'inspect', c, 'deployedBytecode'], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f'ext_check: forge inspect {c} deployedBytecode failed:\n{r.stderr[-1500:]}')
    obj = r.stdout.strip()
    obj = re.sub(r'__\$[0-9a-fA-F]{34}\$__', '0' * 40, obj[2:] if obj.startswith('0x') else obj)
    return bytes.fromhex(obj)


def link_refs(c):
    r = subprocess.run(['forge', 'inspect', c, 'deployedBytecode'], cwd=ROOT, capture_output=True, text=True)
    return '__$' in r.stdout


def main():
    errs = []
    vault_src = open(os.path.join(ROOT, 'src/TouchstoneVault.sol')).read()
    errs += check_abi(forge_inspect(VAULT, 'abi'), forge_inspect(EXT, 'abi'), forge_inspect(ENTRIES, 'abi'),
                      fallback_names(vault_src))
    errs += check_layout(layout_of(VAULT), layout_of(EXT))
    srcs = {p: open(os.path.join(ROOT, p)).read() for p in EXT_SOURCES}
    errs += check_bytecode(runtime(EXT), link_refs(EXT), srcs)
    for e in errs:
        print('ext_check FAIL:', e)
    if not errs:
        print('ext_check OK: (e) ABI split, (g) layout identical, (h) bytecode clean')
    return 1 if errs else 0


def selftest():
    fn = lambda n, ins=(), m='nonpayable': dict(type='function', name=n, stateMutability=m,
                                                inputs=[dict(type=t) for t in ins])
    entries = [fn('seed', ['uint256']), fn('deposit', ['uint256']), fn('withdraw', ['uint256'])]
    ext = entries + [fn('dispatch', ['uint8']), fn('VAULT', m='view')] + [fn(n) for n in ERC20_EXEMPT]
    vault = [fn('settle'), fn('setPositions', ['int24'])]
    names = {'seed', 'deposit', 'withdraw'}
    ok = not check_abi(vault, ext, entries, names)                               # control
    ok &= bool(check_abi(vault + [fn('deposit', ['uint256'])], ext, entries, names))  # a shadowing selector
    ok &= bool(check_abi(vault, ext + [fn('sweep')], entries, names))              # an extra extension entry
    ok &= bool(check_abi(vault, ext, entries, names - {'withdraw'}))               # the fallback misses one
    lay = '   0  0   32 _balances: mapping\n'
    ok &= not check_layout(lay, lay)
    ok &= bool(check_layout(lay, lay + '  99  0   32 _extra: uint256\n'))           # a planted divergence
    clean = bytes([0x60, 0xFF, 0x60, 0xF2, 0x00])                                    # PUSH1 data is skipped
    ok &= not check_bytecode(clean, False, {'a.sol': 'x.call(y);'})
    ok &= bool(check_bytecode(bytes([0x60, 0x01, 0xFF]), False, {}))                 # a planted SELFDESTRUCT
    ok &= bool(check_bytecode(bytes([0xF2]), False, {}))                             # CALLCODE
    ok &= bool(check_bytecode(bytes([0xF4]), False, {}))                             # DELEGATECALL, no library
    ok &= not check_bytecode(bytes([0xF4]), True, {'a.sol': '// delegatecall in a comment'})
    ok &= bool(check_bytecode(bytes([0xF4]), True, {'a.sol': 'x.delegatecall(y);'}))  # a planted delegatecall
    print('ext_check selftest', 'OK' if ok else 'FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(selftest() if '--selftest' in sys.argv else main())
