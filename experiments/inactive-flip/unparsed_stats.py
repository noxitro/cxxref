"""どの TU からも読まれないソース(unparsed)を数え、同じフォルダーか近くの TU のコマンドを借りられるかを見る。
usage: python -I unparsed_stats.py <ルート> <索引.tsv> <compile_commands.json> [<除くフォルダー>...]
"""
import collections, json, os, sys

sys.stdout.reconfigure(encoding='utf-8')
ROOT, INDEX, CDB = sys.argv[1:4]
EXCL = tuple(x.replace('\\', '/').rstrip('/') + '/' for x in sys.argv[4:])
SRC = {'.cpp', '.cc', '.cxx', '.c'}
HDR = {'.h', '.hpp', '.hxx', '.inl', '.hh', '.ipp'}

seen = set()
with open(INDEX + '.deps.tsv', 'rb') as f:
    for raw in f:
        i = raw.find(b'\t')
        seen.add(raw[i + 1:].rstrip(b'\r\n').decode('utf-8', 'replace').lower())
tus = collections.defaultdict(list)  # フォルダー -> TU
for e in json.load(open(CDB, encoding='utf-8')):
    if 'file' not in e:
        continue
    p = os.path.normpath(os.path.join(e.get('directory', ''), e['file']))
    try:
        rel = os.path.relpath(p, ROOT).replace('\\', '/')
    except ValueError:
        continue
    if not rel.startswith('..'):
        tus[os.path.dirname(rel).lower()].append(rel)

n = collections.Counter()
lines = collections.Counter()
borrow = collections.Counter()
for d, dirs, files in os.walk(ROOT):
    rd = os.path.relpath(d, ROOT).replace('\\', '/')
    rd = '' if rd == '.' else rd
    dirs[:] = [x for x in dirs if not x.startswith('.') and not (rd + '/' + x + '/').lstrip('/').startswith(EXCL)]
    for fn in files:
        ext = os.path.splitext(fn)[1].lower()
        if ext not in SRC and ext not in HDR:
            continue
        rel = (rd + '/' + fn).lstrip('/')
        kind = 'src' if ext in SRC else 'hdr'
        try:
            with open(os.path.join(d, fn), 'rb') as f:
                ln = f.read().count(b'\n') + 1
        except OSError:
            continue
        if rel.lower() in seen:
            n[kind + ' 読まれた'] += 1
            lines[kind + ' 読まれた'] += ln
            continue
        n[kind + ' 読まれない'] += 1
        lines[kind + ' 読まれない'] += ln
        if kind == 'src':
            # 借りられる TU: 同じフォルダー、無ければ親をたどる(4 段まで)
            x, hop = rd.lower(), 0
            while True:
                if tus.get(x):
                    borrow[f'{hop} 段上' if hop else '同じフォルダー'] += 1
                    break
                if not x or hop >= 4:
                    borrow['近くに TU が無い'] += 1
                    break
                x, hop = os.path.dirname(x), hop + 1
for k in sorted(n):
    print(f'{k}: {n[k]} ファイル、{lines[k]} 行')
print('読まれない .cpp が借りられる TU:', dict(borrow))
