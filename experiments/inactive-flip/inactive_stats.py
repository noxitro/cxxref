"""索引の .skip.tsv の「無効な #if」を、条件ごとに数える。
- どれだけのファイル・行が、作った構成では無効か
- 無効にしている条件のマクロは、どこで決まるものか(compile_commands.json の -D / ソースの #define / どこにも無い)
usage: python -I inactive_stats.py <ソースのルート> <索引.tsv> [<compile_commands.json>]
"""
import bisect, collections, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pp

sys.stdout.reconfigure(encoding='utf-8')
ROOT, INDEX = sys.argv[1], sys.argv[2]
CDB = sys.argv[3] if len(sys.argv) > 3 else None

inactive = collections.defaultdict(list)
comments = collections.defaultdict(set)
with open(INDEX + '.skip.tsv', 'rb') as f:
    for raw in f:
        p = raw.rstrip(b'\r\n').split(b'\t')
        if len(p) != 4:
            continue
        if p[1] == b'inactive':
            inactive[p[0].decode()].append((int(p[2].split(b':')[0]), int(p[3].split(b':')[0])))
        elif p[1] == b'comment':
            b, e = int(p[2].split(b':')[0]), int(p[3].split(b':')[0])
            if e > b:  # 複数行のコメントの中の行(先頭の行は、コードの後ろのコメントかもしれないので除く)
                comments[p[0].decode()].update(range(b + 1, e + 1))

# その構成で読まれたファイル(.deps.tsv)
seen = set()
with open(INDEX + '.deps.tsv', 'rb') as f:
    for raw in f:
        i = raw.find(b'\t')
        seen.add(raw[i + 1:].rstrip(b'\r\n').decode('utf-8', 'replace'))
seen.discard('file')

# ソースで #define されるマクロ(索引の macro の行)
defined_src = set()
row_lines = collections.defaultdict(set)   # 無効な範囲のあるファイルの、索引に行のある行番号
want = {p.encode() for p in inactive}
with open(INDEX, 'rb') as f:
    for raw in f:
        c = raw.split(b'\t', 4)
        if len(c) < 4:
            continue
        if c[1] == b'macro':
            defined_src.add(c[0].decode('utf-8', 'replace'))
        loc = c[3].rsplit(b':', 2)
        if loc[0] in want:
            row_lines[loc[0].decode()].add(int(loc[1]))
# コマンドラインで渡されるマクロ
defined_cmd = set()
if CDB:
    for e in json.load(open(CDB, encoding='utf-8')):
        s = e.get('command') or ' '.join(e.get('arguments', []))
        defined_cmd.update(re.findall(r'(?:^|[\s"])[-/]D\s*"?([A-Za-z_]\w*)', s))

tot_lines = 0
for p in seen:
    try:
        with open(os.path.join(ROOT, p), 'rb') as f:
            tot_lines += f.read().count(b'\n') + 1
    except OSError:
        pass

n_files = n_ranges = n_lines = n_active_elsewhere = 0
by_class = collections.Counter()       # 分類 -> 行数
by_class_r = collections.Counter()     # 分類 -> 範囲の数
by_macro = collections.Counter()       # マクロ -> 行数
macro_class = {}
chain = collections.Counter()          # 無効な分岐を持つグループの、分岐の数
multi_inactive = 0                     # 1 つのグループに無効な分岐が 2 つ以上
unmatched = 0
for path, ranges in inactive.items():
    try:
        with open(os.path.join(ROOT, path), 'rb') as f:
            data = f.read()
    except OSError:
        continue
    groups = pp.parse(data, comments.get(path, ()))
    by_line = {br.line: br for g in groups for br in g.branches}
    # 範囲の中に索引の行があれば、どこかの TU(か 1 回目の include)では有効だった(インクルードガードで飛ばされた 2 回目など)
    rl = sorted(row_lines.get(path, ()))
    merged = []
    for b, e in sorted(set(ranges)):
        j = bisect.bisect_right(rl, b)
        if j < len(rl) and rl[j] < e:
            n_active_elsewhere += 1
        else:
            merged.append((b, e))
    if not merged:
        continue
    n_files += 1
    seen_groups = collections.Counter()
    for b, e in merged:
        n_ranges += 1
        lines = max(0, e - b - 1) if e > b else 0
        n_lines += lines
        br = by_line.get(b)
        if br is None:
            unmatched += 1
            by_class['(条件を読めない)'] += lines
            by_class_r['(条件を読めない)'] += 1
            continue
        seen_groups[id(br.group)] += 1
        if seen_groups[id(br.group)] == 1:
            chain[len(br.group.branches)] += 1
        # 範囲は、その分岐から次に有効になる分岐(か #endif)まで。間の分岐もまとめて 1 つの範囲になる
        g = br.group
        i = g.branches.index(br)
        k = sum(1 for x in g.branches[i:] if x.line < e or x is br)
        if k > 1:
            multi_inactive += 1
        if pp.is_const_false(br):
            cls = '#if 0(どの構成でも無効)'
        else:
            ms = pp.macros(pp.cond_text(br))
            kinds = set()
            for m in ms:
                c = ('コマンドラインの -D' if m in defined_cmd else 'ソースの #define' if m in defined_src
                     else 'どこにも定義が無い(コンパイラ・外から渡す・別プラットフォーム)')
                macro_class[m] = c
                by_macro[m] += lines
                kinds.add(c)
            if not ms:
                cls = '(マクロなしの式)'
            elif len(kinds) == 1:
                cls = next(iter(kinds))
            else:
                cls = '混在'
        by_class[cls] += lines
        by_class_r[cls] += 1

print(f'読まれたファイル {len(seen)}、行 {tot_lines}')
print(f'無効な #if のあるファイル {n_files}({n_files / max(1, len(seen)):.1%})、範囲 {n_ranges}、'
      f'無効な行 {n_lines}({n_lines / max(1, tot_lines):.2%})、条件を読めない範囲 {unmatched}')
print('ほかの TU か 1 回目の include では有効だった範囲(除いた):', n_active_elsewhere)
print('無効にしている条件のマクロの出どころ(行数・範囲の数):')
for c, n in by_class.most_common():
    print(f'  {n:8d} 行 {by_class_r[c]:6d} 範囲  {c}')
print('無効な分岐を持つグループの分岐の数:', dict(sorted(chain.items())))
print('1 つの範囲に分岐が 2 つ以上入っている範囲:', multi_inactive)
print('マクロごと(上位 30、行数):')
for m, n in by_macro.most_common(30):
    print(f'  {n:8d}  {m}  [{macro_class[m]}]')
