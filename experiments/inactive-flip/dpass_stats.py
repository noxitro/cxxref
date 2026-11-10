"""無効な #if のうち、「どこにも定義の無いマクロを -D で渡せば有効になる」ものがどれだけあるかを数える。
対象: 範囲の先頭が #if / #ifdef で、条件が「defined(X)」「X」の && だけでできていて(!defined(Y) は、Y がどこにも
定義されていなければ可)、X がソースにもコマンドラインにも定義が無く、コンパイラの予約名(_ で始まる)でもないもの。
usage: python -I dpass_stats.py <ソースのルート> <索引.tsv> <compile_commands.json>
"""
import bisect, collections, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pp

sys.stdout.reconfigure(encoding='utf-8')
ROOT, INDEX, CDB = sys.argv[1:4]

inactive, comments = collections.defaultdict(list), collections.defaultdict(set)
with open(INDEX + '.skip.tsv', 'rb') as f:
    for raw in f:
        p = raw.rstrip(b'\r\n').split(b'\t')
        if len(p) != 4 or p[1] not in (b'inactive', b'comment'):
            continue
        b, e =int(p[2].split(b':')[0]), int(p[3].split(b':')[0])
        if p[1] == b'inactive':
            inactive[p[0].decode()].append((b, e))
        elif p[1] == b'comment' and e > b:
            comments[p[0].decode()].update(range(b + 1, e + 1))
defined_src, row_lines = set(), collections.defaultdict(set)
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
defined_cmd = set()
for e in json.load(open(CDB, encoding='utf-8')):
    s = e.get('command') or ' '.join(e.get('arguments', []))
    defined_cmd.update(re.findall(r'(?:^|[\s"])[-/]D\s*"?([A-Za-z_]\w*)', s))
known = defined_src | defined_cmd

POS = re.compile(r'\(*\s*(?:defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?|([A-Za-z_]\w*))\s*\)*')
NEG = re.compile(r'\(*\s*!\s*defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?\s*\)*')


def classify(br):
    """('ok', [マクロ]) か (理由, None)。"""
    if br.kind not in ('if', 'ifdef'):
        return ('#elif・#else の分岐' if br.kind != 'ifndef' else '#ifndef(定義を消す必要がある)'), None
    cond = f'defined({br.cond})' if br.kind == 'ifdef' else br.cond
    if '||' in cond:
        return '|| を含む', None
    need = []
    for term in cond.split('&&'):
        term = term.strip()
        m = NEG.fullmatch(term)
        if m:
            if m.group(1) in known:
                return '!defined(定義のあるマクロ)', None
            continue
        m = POS.fullmatch(term)
        if not m:
            return '比較・関数形のマクロなど', None
        need.append(m.group(1) or m.group(2))
    if any(x.startswith('_') for x in need):
        return 'コンパイラ・プラットフォームの予約名', None
    if any(x in known for x in need):
        return 'ソースか -D に定義のあるマクロ(このTUでは未定義)', None
    return 'ok', need


tot = collections.Counter()
why = collections.Counter()
macros = collections.Counter()
for path, ranges in inactive.items():
    try:
        with open(os.path.join(ROOT, path), 'rb') as f:
            data = f.read()
    except OSError:
        continue
    groups = pp.parse(data, comments.get(path, ()))
    by_line = {br.line: br for g in groups for br in g.branches}
    rl = sorted(row_lines.get(path, ()))
    for b, e in sorted(set(ranges)):
        j = bisect.bisect_right(rl, b)
        if j < len(rl) and rl[j] < e:
            continue
        br = by_line.get(b)
        if br is None or pp.is_const_false(br):
            continue
        n = max(0, e - b - 1)
        tot['lines'] += n
        tot['ranges'] += 1
        k, need = classify(br)
        first = max(0, min(e, br.body_end or e) - br.line_end - 1)  # 先頭の分岐の本体だけが有効になる
        if need is None:
            why[k] += n
            continue
        tot['d_lines'] += first
        tot['d_ranges'] += 1
        why['-D で有効になる分岐の後ろの #elif・#else(残り)'] += n - first
        for x in need:
            macros[x] += first
print(f"無効な行(#if 0 を除く){tot['lines']}、範囲 {tot['ranges']}")
print(f"-D で渡せば有効になる: {tot['d_lines']} 行({100 * tot['d_lines'] / max(1, tot['lines']):.1f}%)、範囲 {tot['d_ranges']}、"
      f"マクロ {len(macros)} 個")
print('渡せない理由(行数):')
for k, v in why.most_common():
    print(f'  {v:7d}  {k}')
print('渡すマクロ(上位 15、行数):', macros.most_common(15))
