"""試作: 「条件を反転して読み直す」。
作った構成では無効だった #if の分岐を、そのファイルの中だけ有効にして、そのファイルを読む TU を 1 つだけ解析し直す。
マクロの値は要らない(分岐の行を #if 1 / #if 0 に書き換えた写しを、ltidx の -overlay で元のパスに見せる)。
得られた行のうち、元は無効だった行のものだけを残す(差分)。

flip_main.py から使う
"""
import bisect, collections, json, os, re, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pp

sys.stdout.reconfigure(encoding='utf-8')
LTIDX = os.environ.get('LTIDX') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'indexer', 'build', 'ltidx.exe')


def load_base(index, only=None):
    """{path: [(b, e)]}(どの TU でも無効だった範囲)と、TU の一覧、ファイル -> それを読む TU。"""
    inactive, comments = collections.defaultdict(list), collections.defaultdict(set)
    with open(index + '.skip.tsv', 'rb') as f:
        for raw in f:
            p = raw.rstrip(b'\r\n').split(b'\t')
            if len(p) != 4:
                continue
            if p[1] == b'inactive':
                inactive[p[0].decode()].append((int(p[2].split(b':')[0]), int(p[3].split(b':')[0])))
            elif p[1] == b'comment':
                b, e = int(p[2].split(b':')[0]), int(p[3].split(b':')[0])
                if e > b:
                    comments[p[0].decode()].update(range(b + 1, e + 1))
    want = {p.encode() for p in inactive}
    row_lines = collections.defaultdict(list)
    with open(index, 'rb') as f:
        for raw in f:
            c = raw.split(b'\t', 4)
            if len(c) < 4:
                continue
            loc = c[3].rsplit(b':', 2)
            if loc[0] in want:
                row_lines[loc[0].decode()].append(int(loc[1]))
    res = {}
    for path, ranges in inactive.items():
        rl = sorted(set(row_lines.get(path, ())))
        keep = []
        for b, e in sorted(set(ranges)):
            j = bisect.bisect_right(rl, b)
            if not (j < len(rl) and rl[j] < e):
                keep.append((b, e))
        if keep:
            res[path] = keep
    tus = {}
    with open(index + '.tu.tsv', encoding='utf-8', errors='replace') as f:
        next(f)
        for line in f:
            c = line.rstrip('\n').split('\t')
            tus[c[0]] = (c[1] == '1', float(c[3]))
    readers = collections.defaultdict(list)
    with open(index + '.deps.tsv', encoding='utf-8', errors='replace') as f:
        next(f)
        for line in f:
            tu, _, fl = line.rstrip('\n').partition('\t')
            if fl in res:
                readers[fl].append(tu)
    return res, comments, tus, readers


def lines_of(ranges):
    s = set()
    for b, e in ranges:
        s.update(range(b + 1, e))
    return s


def body_first(br):
    return br.line_end + 1 if br.line_end + 1 < br.body_end else None


def choose(groups, uncovered, base_active):
    """このパスで有効にする分岐を選ぶ。{group の id: branch}。"""
    chosen = {}
    for g in groups:  # 文書の順(外側が先)
        for br in g.branches:
            f = body_first(br)
            if f is None or f not in uncovered or pp.is_const_false(br):
                continue
            par = g.parent
            if par is not None and body_first(par) in uncovered and chosen.get(id(par.group)) is not par:
                continue  # 親がまだ無効で、このパスでも有効にしない
            chain, x, ok = [], br, True
            while x is not None:
                if x is not br and x in base_active:
                    x = x.group.parent
                    continue  # 元の構成で有効な親は、そのまま評価させる(インクルードガードを壊さない)
                if chosen.get(id(x.group), x) is not x:
                    ok = False
                    break
                chain.append(x)
                x = x.group.parent
            if not ok:
                continue
            for x in chain:
                chosen[id(x.group)] = x
            break  # このグループからは 1 つ
    return chosen


def rewrite(data, groups, chosen):
    lines = data.split(b'\n')

    def put(br, text):
        cr = b'\r' if lines[br.line - 1].endswith(b'\r') else b''
        lines[br.line - 1] = text + cr
        for i in range(br.line, br.line_end):  # 続き行は空に(行番号を保つ)
            lines[i] = cr
    for g in groups:
        b = chosen.get(id(g))
        if b is None:
            continue
        i = g.branches.index(b)
        for x in g.branches[:i]:
            put(x, b'#if 0' if x.kind in ('if', 'ifdef', 'ifndef') else b'#elif 0')
        if b.kind in ('if', 'ifdef', 'ifndef'):
            put(b, b'#if 1')
        elif b.kind != 'else':
            put(b, b'#elif 1')
    return b'\n'.join(lines)


def run_ltidx(cdb_entry, root, rel, content, work):
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    ap = os.path.join(root, rel.replace('/', os.sep))
    rep = os.path.join(work, 'flip-' + os.path.basename(ap))
    with open(rep, 'wb') as f:
        f.write(content)
    ov = os.path.join(work, 'overlay.tsv')
    with open(ov, 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'{ap}\t{rep}\n')
    e = dict(cdb_entry)
    json.dump([e], open(os.path.join(work, 'compile_commands.json'), 'w', encoding='utf-8'))
    out = os.path.join(work, 'out.tsv')
    t0 = time.time()
    r = subprocess.run([LTIDX, '-p', work, '-root', root, '-o', out, '-j', '1', '-overlay', ov], capture_output=True,
                       text=True, encoding='utf-8', errors='replace')
    sec = time.time() - t0
    rows, skips, errors = [], [], -1
    if os.path.exists(out):
        with open(out, 'rb') as f:
            for raw in f:
                c = raw.rstrip(b'\r\n').split(b'\t')
                if len(c) >= 4:
                    loc = c[3].rsplit(b':', 2)
                    if loc[0].decode('utf-8', 'replace') == rel:
                        rows.append((int(loc[1]), raw.rstrip(b'\r\n').decode('utf-8', 'replace')))
        with open(out + '.skip.tsv', 'rb') as f:
            for raw in f:
                p = raw.rstrip(b'\r\n').split(b'\t')
                if len(p) == 4 and p[1] == b'inactive' and p[0].decode('utf-8', 'replace') == rel:
                    skips.append((int(p[2].split(b':')[0]), int(p[3].split(b':')[0])))
        with open(out + '.tu.tsv', encoding='utf-8', errors='replace') as f:
            next(f)
            for line in f:
                errors = int(line.split('\t')[2])
    return rows, skips, errors, sec, r.returncode


def flip_file(root, rel, base_ranges, comment_lines, entry, work, passes):
    with open(os.path.join(root, rel), 'rb') as f:
        data = f.read()
    groups = pp.parse(data, comment_lines)
    base_off = lines_of(base_ranges)
    dead = set()
    for g in groups:
        for br in g.branches:
            if pp.is_const_false(br) and body_first(br) in base_off:
                dead.update(range(br.line_end + 1, br.body_end))
    base_active = {br for g in groups for br in g.branches if body_first(br) is not None and body_first(br) not in base_off}
    target = base_off - dead
    uncovered = set(target)
    out_rows, stats = {}, []
    for n in range(passes):
        chosen = choose(groups, uncovered, base_active)
        if not chosen:
            break
        rows, skips, errors, sec, rc = run_ltidx(entry, root, rel, rewrite(data, groups, chosen), os.path.join(work, f'p{n}'))
        if errors < 0:
            stats.append((n, len(chosen), 0, 0, errors, sec))
            break
        # 同じ TU の中で 2 回目に読まれたとき(インクルードガードで全体が飛ぶ)の範囲は、中に索引の行があるので除く
        rl = sorted({ln for ln, _ in rows})
        skips = [(b, e) for b, e in set(skips)
                 if not ((j := bisect.bisect_right(rl, b)) < len(rl) and rl[j] < e)]
        now_off = lines_of(skips)
        newly = {ln for ln in uncovered if ln not in now_off}
        got = 0
        for ln, raw in rows:
            if ln in newly or (ln in target and ln not in now_off):
                if raw not in out_rows:
                    out_rows[raw] = n
                    got += 1
        stats.append((n, len(chosen), len(newly), got, errors, sec))
        if not newly:
            break
        uncovered -= newly
    return target, uncovered, dead, out_rows, stats


