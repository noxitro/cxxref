"""試作: 行を囲む #if の条件を、問い合わせのときにソースから読んで付ける(索引には何も足さない)。
usage: python -I annotate.py <ルート> <相対パス:行>...
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pp

sys.stdout.reconfigure(encoding='utf-8')


def conds(groups, line):
    """その行を囲む分岐の条件を、外側から。"""
    out = []
    for g in groups:
        for br in g.branches:
            if br.line_end < line < (br.body_end or 0):
                out.append((g.depth, pp.cond_text(br)))
    return [c for _, c in sorted(out)]


root = sys.argv[1]
cache = {}
for arg in sys.argv[2:]:
    rel, _, ln = arg.rpartition(':')
    if rel not in cache:
        with open(os.path.join(root, rel), 'rb') as f:
            data = f.read()
        cache[rel] = (pp.parse(data), data.split(b'\n'))
    groups, src = cache[rel]
    cs = conds(groups, int(ln))
    text = src[int(ln) - 1].decode('utf-8', 'replace').strip()
    print(f'{rel}:{ln}  [#if ' + ' && '.join(cs) + f']  | {text[:110]}')
