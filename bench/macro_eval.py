"""同じソースを古い ltidx と新しい ltidx で作った 2 つの索引を、同じ ltq で引き比べる。
- 関数(呼び出しのある記号)を乱数で選び、「索引に無い一致」の分類ごとの件数と、理由の付かない other の件数を比べる
- ルートで定義されたマクロを乱数で選び、索引の答え(展開の数)と、ソースの単語の一致(#define の行を除く)を比べる
usage: python -I macro_eval.py <ltq のフォルダ> <ルート> <古い索引> <新しい索引> [関数の数] [マクロの数]
"""
import collections
import contextlib
import io
import os
import random
import re
import sys

sys.path.insert(0, sys.argv[1])
import ltq  # noqa: E402

root, old_idx, new_idx = sys.argv[2], sys.argv[3], sys.argv[4]
n_fn = int(sys.argv[5]) if len(sys.argv) > 5 else 150
n_mac = int(sys.argv[6]) if len(sys.argv) > 6 else 60
rnd = random.Random(13)


def skip_path(p):
    return p.startswith(('vcpkg_installed/', 'build/', 'External/', '3rdParty/')) or '/_deps/' in p or 'reflection_generated/' in p


calls, macros = collections.Counter(), {}
with open(new_idx, encoding='utf-8', errors='replace') as f:
    for line in f:
        c = line.split('\t', 4)
        if len(c) < 5 or skip_path(c[3]):
            continue
        if c[1] == 'call' and not c[0].startswith('?::'):
            calls[c[0]] += 1
        elif c[1] == 'macro':
            macros.setdefault(c[0], c[3])
fns = sorted(s for s in calls if re.fullmatch(r'[\w:~]+', s) and not s.rsplit('::', 1)[-1].startswith('operator'))
mac = sorted(m for m in macros if re.fullmatch(r'[A-Za-z_]\w*', m) and not m.startswith('_'))
fns = rnd.sample(fns, min(n_fn, len(fns)))
mac = rnd.sample(mac, min(n_mac, len(mac)))

CATS = [('comment', 'inside comments'), ('inactive', 'inside inactive'), ('dropped', 'inside macro arguments'),
        ('macro', 'inside macro definitions'), ('string', 'inside string literals'), ('unparsed', 'in files no indexed TU'),
        ('other', 'other ->')]


def run(idx, name):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ltq.main(['--root', root, 'refs', name], idx=idx)
    t = out.getvalue()
    cats = {}
    for k, head in CATS:
        m = re.search(r'^  ' + re.escape(head) + r'[^\n]*: (\d+)$', t, re.M)
        cats[k] = int(m.group(1)) if m else 0
    sec = {}
    for s in ('calls', 'macro expansions', 'definitions and declarations'):
        m = re.search(r'^== ' + re.escape(s) + r'[^\n]*: (\d+)$', t, re.M)
        sec[s] = int(m.group(1)) if m else 0
    return t, cats, sec


I_old, I_new = ltq.Index([old_idx], root), ltq.Index([new_idx], root)
tot = {'old': collections.Counter(), 'new': collections.Counter()}
size = collections.Counter()
moved = []
for s in fns:
    for tag, idx in (('old', I_old), ('new', I_new)):
        t, cats, sec = run(idx, s)
        tot[tag].update(cats)
        tot[tag]['queries with other > 0'] += cats['other'] > 0
        size[tag] += len(t.encode('utf-8'))
    if tot['new']['dropped'] and len(moved) < 8:
        pass
print(f'functions: {len(fns)}')
for k, _ in CATS:
    print(f'  {k:9} old {tot["old"][k]:6}  new {tot["new"][k]:6}')
print(f'  queries with other > 0: old {tot["old"]["queries with other > 0"]}  new {tot["new"]["queries with other > 0"]}')
print(f'  output bytes: old {size["old"]}  new {size["new"]}  ({size["new"] / max(1, size["old"]) - 1:+.1%})')

# マクロ: 新しい索引の展開の数と、ソースの単語の一致(#define の行・コメントは ltq の分類で除く)
found_old = found_new = 0
cov = []
for m in mac:
    t0, c0, s0 = run(I_old, m)
    t1, c1, s1 = run(I_new, m)
    found_old += 'no symbol in the index matches' not in t0
    found_new += 'no symbol in the index matches' not in t1
    cov.append((m, s1['macro expansions'], s1['definitions and declarations'], c0['other'], c1['other'], c1['inactive']))
print(f'macros: {len(mac)}  found old {found_old}  new {found_new}')
print(f'  unexplained text matches (other): old {sum(x[3] for x in cov)}  new {sum(x[4] for x in cov)}')
print(f'  expansions listed: {sum(x[1] for x in cov)}  definitions listed: {sum(x[2] for x in cov)}')
left = [x for x in cov if x[4]]
print(f'  macros with other left: {len(left)}')
for x in left[:12]:
    print('   ', x)
