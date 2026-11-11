"""差分パス: 作った構成では無効な #if の中を仮に有効にして解析し、索引の差分(<索引>.alt.tsv)を作る。

索引(ltidx)は compile_commands.json の構成で有効なコードだけを解析するので、無効な #if の中の呼び出しや定義は索引に入らない。
関数の名前や引数を変えるときにそこが漏れないよう、索引を作った後にこのスクリプトを走らせる(index-project.ps1 と、MCP サーバーの
自動更新が呼ぶ)。構成をまるごともう 1 つ作るのではなく、無効な範囲のあるファイルごとに、そのファイルを読む TU を 1 つだけ解析し直す。

やり方(ファイル F ごと):
1. F の無効な分岐を選び、その行を `#if 1`(前の分岐は `#if 0` / `#elif 0`)に書き換えた写しを作る。行数は変えない。
   マクロの値は要らない。元の構成で有効な分岐(インクルードガードなど)には触らない。`#if 0` は対象にしない。
2. 条件が「どこにも定義の無いマクロが定義されていること」だけなら、そのマクロを -D でも渡す
   (同じマクロで囲まれた、他のヘッダーの宣言も有効になる)。
3. ltidx の -overlay で写しを元のパスに見せ、F を読む TU を 1 つ解析する(-keep-going: エラーがあっても先へ進む)。
4. 得られた行のうち、F の、元は無効だった行のものだけを残す。#if / #else の両方を有効にはできないので、残りがあれば繰り返す。

出力:
- <索引>.alt.tsv   索引と同じ 5 列。ltq が索引と合わせて引き、[inactive: #if 条件] を付けて出す
- <索引>.alt.json  ファイルごとの署名と結果。次に走らせたとき、何も変わっていないファイルは解析し直さない

仮に有効にしたコードは、その構成でコンパイルできるとは限らない(別のプラットフォーム向けのヘッダーが無い、など)。
エラーの後も解析を続けるが、宣言が見つからない呼び出しは行にならない。漏れが無いことは保証しない(ltq は、行にならなかった
無効な範囲の中の文字列の一致を、別に 1 行ずつ出す)。

usage: python -I altpass.py --index <root>\\.index\\index.tsv [--root DIR] [--cdb DIR] [--jobs N] [--passes N]
                            [--ltidx EXE] [--no-define] [--force] [--only REL ...]
       --root / --cdb / --jobs を省くと、<索引>.recipe.json(index-project.ps1 が書く)から読む
"""
import argparse
import bisect
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'query'))
import ppcond  # noqa: E402

ALT_VERSION = 1   # 作り方を変えたら上げる(前の結果を使い回さない)
PASSES = 3        # 1 つのファイルで繰り返す回数の上限(#if / #elif / #else を順に有効にする)
TIMEOUT = 900     # 1 回の解析の上限(秒)
POS = re.compile(r'\(*\s*(?:defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?|([A-Za-z_]\w*))\s*\)*')
NEG = re.compile(r'\(*\s*!\s*defined\s*\(?\s*([A-Za-z_]\w*)\s*\)?\s*\)*')


def find_ltidx():
    """index-project.ps1・mcp/autoupdate.py と同じ順で探す。"""
    if os.environ.get('LTIDX'):
        return os.environ['LTIDX']
    cands = [os.path.join(HERE, '..', 'indexer', 'build', 'ltidx.exe')]
    if os.environ.get('LOCALAPPDATA'):
        cands.append(os.path.join(os.environ['LOCALAPPDATA'], 'cxxref', 'ltidx', 'ltidx.exe'))
    return next((c for c in cands if os.path.exists(c)), None)


def supports_overlay(ltidx):
    """この ltidx に -overlay と -keep-going があるか(古い配布物には無い)。"""
    try:
        r = subprocess.run([ltidx, '--help'], capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    text = r.stdout + r.stderr
    return b'keep-going' in text and b'overlay' in text


def norm(p):
    return os.path.normcase(os.path.normpath(p))


def lines_of(ranges):
    """無効な範囲 [(始まりの行, 終わりの行)] の中の行(#if の行と #endif / #else の行は含まない)。"""
    s = set()
    for b, e in ranges:
        s.update(range(b + 1, e))
    return s


def load_base(index, only=None):
    """索引から、差分パスの対象を読む。
    戻り値: (無効な範囲 {path: [(b, e)]}, 索引に行のある行 {path: set}, コメントの行 {path: set}, TU {絶対パス: (ok, 秒)},
             読む TU {path: [TU]}, TU が読むファイル {TU: [path]}, ソースに定義のあるマクロの集合)
    範囲 1 つは、続けて無効な分岐をまとめて持つ(#if から、有効な #else まで、など。途中の #elif の行を含む)。"""
    inactive, comments = collections.defaultdict(set), collections.defaultdict(set)
    with open(index + '.skip.tsv', 'rb') as f:
        for raw in f:
            p = raw.rstrip(b'\r\n').split(b'\t')
            if len(p) != 4 or p[1] not in (b'inactive', b'comment'):
                continue
            try:
                b, e = int(p[2].split(b':')[0]), int(p[3].split(b':')[0])
            except ValueError:
                continue  # 見出しの行
            if p[1] == b'inactive':
                inactive[p[0]].add((b, e))
            elif e > b:
                comments[p[0]].update(range(b + 1, e + 1))
    if only is not None:
        keep = {p.encode('utf-8') for p in only}
        inactive = {k: v for k, v in inactive.items() if k in keep}
    row_lines, macros = collections.defaultdict(set), set()
    with open(index, 'rb') as f:
        for raw in f:
            c = raw.split(b'\t', 4)
            if len(c) < 4:
                continue
            if c[1] == b'macro':
                macros.add(c[0])
            loc = c[3].rsplit(b':', 2)
            if len(loc) == 3 and loc[0] in inactive and loc[1].isdigit():
                row_lines[loc[0]].add(int(loc[1]))
    ranges = {}
    for path, rs in inactive.items():
        keep = sorted((b, e) for b, e in rs if e - b > 1)
        if keep:
            ranges[path.decode('utf-8', 'replace')] = keep
    tus = {}
    with open(index + '.tu.tsv', encoding='utf-8', errors='replace') as f:
        next(f, None)
        for line in f:
            c = line.rstrip('\n').split('\t')
            if len(c) >= 4:
                try:
                    tus[c[0]] = (c[1] == '1', float(c[3]))
                except ValueError:
                    tus[c[0]] = (c[1] == '1', 0.0)
    readers, files_of, names = collections.defaultdict(list), collections.defaultdict(list), {}
    with open(index + '.deps.tsv', encoding='utf-8', errors='replace') as f:
        next(f, None)
        for line in f:
            tu, _, fl = line.rstrip('\n').partition('\t')
            tu, fl = names.setdefault(tu, tu), names.setdefault(fl, fl)  # 同じ文字列を 1 つにする(O3DE で 240 万行)
            files_of[tu].append(fl)
            if fl in ranges:
                readers[fl].append(tu)
    return (ranges, {k.decode('utf-8', 'replace'): v for k, v in row_lines.items()},
            {k.decode('utf-8', 'replace'): v for k, v in comments.items() if k in inactive}, tus, readers, files_of,
            {m.decode('utf-8', 'replace') for m in macros})


def unparsed_ranges(ranges, row_lines, groups):
    """無効な範囲のうち、索引に行の無いもの。行があれば、別の TU では有効だった範囲で、もう索引に入っている
    (.skip.tsv は TU ごとの範囲の和)。条件の行そのものにある行(#elif の条件のマクロ)は数えない。"""
    rl = sorted(set(row_lines) - ppcond.directive_lines(groups))
    out = []
    for b, e in ranges:
        i = bisect.bisect_right(rl, b)
        if i == len(rl) or rl[i] >= e:
            out.append((b, e))
    return out


def load_cdb(cdb_dir):
    """{正規化した絶対パス: compile_commands.json の項目}(同じファイルが複数あれば最初のもの)と、-D で渡されているマクロ。"""
    with open(os.path.join(cdb_dir, 'compile_commands.json'), encoding='utf-8') as f:
        entries = json.load(f)
    cdb, defined = {}, set()
    for e in entries:
        if 'file' not in e:
            continue
        cdb.setdefault(norm(os.path.join(e.get('directory', ''), e['file'])), e)
        s = e.get('command') or ' '.join(e.get('arguments', []))
        defined.update(re.findall(r'(?:^|[\s"])[-/]D\s*"?([A-Za-z_]\w*)', s))
    return cdb, defined


def choose(groups, uncovered, base_active):
    """このパスで有効にする分岐を選ぶ。{group の id: branch}。1 つのグループからは 1 つ。
    まだ有効にしていない行(uncovered)から始まる分岐を、文書の順に選ぶ。無効な親の中の分岐は、その親をこのパスで有効に
    するときだけ選ぶ。元の構成で有効な親(base_active)はそのまま評価させる(インクルードガードを壊さない)。"""
    chosen = {}
    for g in groups:  # 文書の順(外側が先)
        for br in g.branches:
            f = br.body_first()
            if f is None or f not in uncovered or ppcond.is_const_false(br):
                continue
            par = g.parent
            if par is not None and par.body_first() in uncovered and chosen.get(id(par.group)) is not par:
                continue  # 親がまだ無効で、このパスでも有効にしない
            chain, x, ok = [], br, True
            while x is not None:
                if x is not br and x in base_active:
                    x = x.group.parent
                    continue
                if chosen.get(id(x.group), x) is not x:
                    ok = False  # 同じグループの別の分岐を、もう選んでいる
                    break
                chain.append(x)
                x = x.group.parent
            if not ok:
                continue
            for x in chain:
                chosen[id(x.group)] = x
            break
    return chosen


def rewrite(data, groups, chosen):
    """選んだ分岐が有効になるよう、条件の行を書き換えた写し。行数は変えない(続き行は空にする)。"""
    lines = data.split(b'\n')

    def put(br, text):
        cr = b'\r' if lines[br.line - 1].endswith(b'\r') else b''
        lines[br.line - 1] = text + cr
        for i in range(br.line, br.line_end):
            lines[i] = b'\r' if lines[i].endswith(b'\r') else b''
    for g in groups:
        b = chosen.get(id(g))
        if b is None:
            continue
        for x in g.branches[:g.branches.index(b)]:
            put(x, b'#if 0' if x.kind in ppcond.OPEN else b'#elif 0')
        if b.kind in ppcond.OPEN:
            put(b, b'#if 1')
        elif b.kind != 'else':
            put(b, b'#elif 1')
    return b'\n'.join(lines)


def defines_for(br, known):
    """この分岐を有効にするために -D で渡せるマクロ(渡せなければ None)。
    渡すのは、条件が「defined(X)」「X」の && だけでできていて(!defined(Y) は、Y に定義が無ければ可)、X がソースにも
    コマンドラインにも定義が無く、コンパイラの予約名(_ で始まる)でもないとき。値や比較の要る条件は推測しない。"""
    if br.kind in ('ifndef', 'elifndef', 'else'):
        return None
    cond = ppcond.own_cond(br)
    if '||' in cond:
        return None
    need = []
    for term in cond.split('&&'):
        term = term.strip()
        m = NEG.fullmatch(term)
        if m:
            if m.group(1) in known:
                return None
            continue
        m = POS.fullmatch(term)
        if not m:
            return None
        need.append(m.group(1) or m.group(2))
    if any(x.startswith('_') or x in known or x in ('true', 'false', 'defined') for x in need):
        return None
    return need


def add_defines(entry, names):
    """compile_commands.json の項目に -D<名前> を足した写し(コンパイラの名前のすぐ後ろに入れる)。"""
    e = dict(entry)
    flags = ['-D' + n for n in names]
    if not flags:
        return e
    if e.get('arguments'):
        e['arguments'] = list(e['arguments'][:1]) + flags + list(e['arguments'][1:])
    else:
        cmd = e.get('command', '')
        m = re.match(r'\s*(?:"[^"]*"|\S+)', cmd)
        k = m.end() if m else 0
        e['command'] = cmd[:k] + ' ' + ' '.join(flags) + cmd[k:]
    return e


def run_ltidx(ltidx, entry, root, rel, content, work):
    """F(rel)を content に差し替えて、entry の TU を 1 つ解析する。
    戻り値: (F の行 [(行番号, 索引の行)], F の無効な範囲, エラーの数(索引ができなければ -1), 秒)"""
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    ap = os.path.join(root, rel.replace('/', os.sep))
    rep = os.path.join(work, 'alt-' + os.path.basename(ap))
    with open(rep, 'wb') as f:
        f.write(content)
    ov = os.path.join(work, 'overlay.tsv')
    with open(ov, 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'{ap}\t{rep}\n')
    with open(os.path.join(work, 'compile_commands.json'), 'w', encoding='utf-8') as f:
        json.dump([entry], f)
    out = os.path.join(work, 'out.tsv')
    t0 = time.time()
    try:
        subprocess.run([ltidx, '-p', work, '-root', root, '-o', out, '-j', '1', '-overlay', ov, '-keep-going'],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=TIMEOUT,
                       creationflags=getattr(subprocess, 'IDLE_PRIORITY_CLASS', 0))
    except (OSError, subprocess.SubprocessError):
        return [], [], -1, time.time() - t0
    sec = time.time() - t0
    rows, skips, errors = [], [], -1
    rb = rel.encode('utf-8')
    try:
        with open(out, 'rb') as f:
            for raw in f:
                raw = raw.rstrip(b'\r\n')
                c = raw.split(b'\t', 4)
                if len(c) == 5:
                    loc = c[3].rsplit(b':', 2)
                    if len(loc) == 3 and loc[0] == rb:
                        rows.append((int(loc[1]), raw))
        with open(out + '.skip.tsv', 'rb') as f:
            for raw in f:
                p = raw.rstrip(b'\r\n').split(b'\t')
                if len(p) == 4 and p[1] == b'inactive' and p[0] == rb:
                    skips.append((int(p[2].split(b':')[0]), int(p[3].split(b':')[0])))
        with open(out + '.tu.tsv', encoding='utf-8', errors='replace') as f:
            next(f, None)
            for line in f:
                errors = int(line.split('\t')[2])
    except (OSError, ValueError, IndexError):
        return [], [], -1, sec
    return rows, skips, errors, sec


def flip_file(ltidx, root, rel, data, ranges, groups, entry, work, passes, known):
    """ファイル 1 つの差分を作る。known は、-D で渡さないマクロの集合(None なら -D を使わない)。
    戻り値: (索引の行(bytes)の一覧, {target, covered, rows, runs, errors, failed, sec, defines})"""
    n_lines = data.count(b'\n') + 1
    base_off = lines_of(ranges)
    dirs = ppcond.directive_lines(groups)  # 数えるときは、条件の行(途中の #elif や、中の #if)を除く
    dead = set()  # #if 0 の中。どの構成でも有効にならないので、対象にしない
    for g in groups:
        for br in g.branches:
            if ppcond.is_const_false(br) and br.body_first() in base_off:
                dead.update(range(br.line_end + 1, min(br.body_end, n_lines + 1)))
    base_active = {br for g in groups for br in g.branches if br.body_first() is not None and br.body_first() not in base_off}
    target = base_off - dead
    uncovered = set(target)
    out, st = {}, {'target': len(target - dirs), 'covered': 0, 'rows': 0, 'runs': 0, 'errors': 0, 'failed': 0, 'sec': 0.0,
                   'defines': []}
    for n in range(passes):
        chosen = choose(groups, uncovered, base_active)
        if not chosen:
            break
        names = set()
        if known is not None:
            for br in chosen.values():
                names.update(defines_for(br, known) or ())
        rows, skips, errors, sec = run_ltidx(ltidx, add_defines(entry, sorted(names)), root, rel,
                                             rewrite(data, groups, chosen), os.path.join(work, f'p{n}'))
        st['runs'] += 1
        st['sec'] += sec
        if errors < 0:
            st['failed'] += 1
            break
        st['errors'] += 1 if errors else 0
        st['defines'] = sorted(set(st['defines']) | names)
        now_off = lines_of(skips)
        newly = {ln for ln in uncovered if ln not in now_off}
        for ln, raw in rows:
            if ln in target and ln not in now_off:
                out.setdefault(raw, None)
        if not newly:
            break
        uncovered -= newly
    shutil.rmtree(work, ignore_errors=True)
    st['covered'] = len(target - dirs) - len(uncovered - dirs)
    st['rows'] = len(out)
    st['sec'] = round(st['sec'], 2)
    return list(out), st


def sha(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else repr(p).encode('utf-8'))
        h.update(b'\0')
    return h.hexdigest()


def row_key(raw):
    c = raw.split(b'\t', 4)
    loc = c[3].rsplit(b':', 2)
    return loc[0], int(loc[1]), int(loc[2]), raw


def replace(src, dst, tries=20):
    """os.replace。Windows では、問い合わせ(MCP サーバー)が dst を開いている間は置き換えられないので、少し待って繰り返す。"""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.25)


def read_alt(path):
    """前の差分を {path: [索引の行]} で。"""
    res = collections.defaultdict(list)
    try:
        with open(path, 'rb') as f:
            for raw in f:
                raw = raw.rstrip(b'\r\n')
                c = raw.split(b'\t', 4)
                if len(c) == 5:
                    res[c[3].rsplit(b':', 2)[0].decode('utf-8', 'replace')].append(raw)
    except OSError:
        pass
    return res


def run(a):
    index = os.path.abspath(a.index)
    rec = {}
    try:
        with open(index + '.recipe.json', encoding='utf-8-sig') as f:
            rec = json.load(f)
    except (OSError, ValueError):
        pass
    root, cdb_dir = a.root or rec.get('root'), a.cdb or rec.get('cdb')
    jobs = a.jobs or rec.get('jobs') or max(1, (os.cpu_count() or 2) // 2)
    if not root or not cdb_dir:
        print(f'alt pass: no --root / --cdb and no {index}.recipe.json')
        return 2
    root = os.path.abspath(root)
    if not all(os.path.exists(index + s) for s in ('', '.skip.tsv', '.deps.tsv', '.tu.tsv')):
        print('alt pass: skipped (the index or its .skip.tsv / .deps.tsv / .tu.tsv is missing)')
        return 0
    ltidx = a.ltidx or find_ltidx()
    if not ltidx or not os.path.exists(ltidx):
        print('alt pass: skipped (ltidx.exe not found)')
        return 0
    if not supports_overlay(ltidx):
        print('alt pass: skipped (this ltidx has no -overlay / -keep-going; get the current one with scripts\\get-ltidx.bat).'
              ' Code inside inactive #if branches is listed by text only')
        return 0
    t0 = time.time()
    ranges, row_lines, comments, tus, readers, files_of, macros = load_base(index, a.only or None)
    cdb, defined = load_cdb(cdb_dir)
    known = None if a.no_define else macros | defined
    st = os.stat(ltidx)
    tool = (ALT_VERSION, st.st_size, st.st_mtime_ns, a.passes, known is not None)
    alt, state_path = index + '.alt.tsv', index + '.alt.json'
    old_rows, old_units = {}, {}
    if not a.force or a.only:  # --only では、指定外のファイルの前の結果を残すので、--force でも読む
        try:
            with open(state_path, encoding='utf-8') as f:
                old = json.load(f)
            if old.get('version') == ALT_VERSION:
                old_units, old_rows = old.get('units', {}), read_alt(alt)
        except (OSError, ValueError):
            pass

    stat_cache, dep_cache = {}, {}

    def dep_sig(tu):
        if tu not in dep_cache:
            parts = []
            for d in sorted(files_of.get(tu, ())):
                s = stat_cache.get(d)
                if s is None:
                    try:
                        x = os.stat(os.path.join(root, d))
                        s = (x.st_mtime_ns, x.st_size)
                    except OSError:
                        s = (0, -1)
                    stat_cache[d] = s
                parts.append((d, s))
            dep_cache[tu] = sha(parts)
        return dep_cache[tu]

    units, todo, no_tu = {}, [], 0
    for rel in sorted(ranges):
        ap = norm(os.path.join(root, rel))
        cands = sorted((t for t in readers.get(rel, ()) if norm(t) in cdb),
                       key=lambda t: (norm(t) != ap, not tus.get(t, (False, 0))[0], tus.get(t, (False, 9e9))[1], t))
        try:
            with open(os.path.join(root, rel), 'rb') as f:
                data = f.read()
        except OSError:
            cands = []
        groups = ppcond.parse(data, comments.get(rel, ())) if cands else None
        rs = unparsed_ranges(ranges[rel], row_lines.get(rel, ()), groups) if cands else ranges[rel]
        if not rs:
            continue  # どの範囲も、別の TU で解析されている
        if not cands:
            no_tu += 1
            continue
        tu = cands[0]
        entry = cdb[norm(tu)]
        # 変わりうるもの: 作り方と ltidx、F の中身、無効な範囲、選んだ TU とそのコマンド、その TU が読むファイル
        sig = sha(tool, rel, hashlib.sha1(data).digest(), rs, tu, json.dumps(entry, sort_keys=True), dep_sig(tu),
                  sorted(known & set(re.findall(r'[A-Za-z_]\w*', data.decode('latin-1')))) if known else None)
        prev = old_units.get(rel)
        if prev and prev.get('sig') == sig and not a.force:
            units[rel] = (old_rows.get(rel, []), prev)
        else:
            todo.append((rel, tu, entry, data, sig, rs, groups))
    folder = os.path.dirname(index)
    for name in os.listdir(folder):  # 途中で止められた回の残り
        p = os.path.join(folder, name)
        try:
            if name.startswith('alt-work-') and os.path.isdir(p) and time.time() - os.stat(p).st_mtime > 86400:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
    work = tempfile.mkdtemp(dir=folder, prefix='alt-work-')

    def one(arg):
        i, (rel, tu, entry, data, sig, rs, groups) = arg
        try:
            rows, s = flip_file(ltidx, root, rel, data, rs, groups, entry, os.path.join(work, str(i)), a.passes, known)
        except Exception as ex:  # 1 つのファイルの失敗で全体を止めない
            rows, s = [], {'target': 0, 'covered': 0, 'rows': 0, 'runs': 0, 'errors': 0, 'failed': 1, 'sec': 0.0,
                           'defines': [], 'error': f'{type(ex).__name__}: {ex}'}
        s.update(sig=sig, tu=os.path.relpath(tu, root).replace('\\', '/') if norm(tu).startswith(norm(root)) else tu)
        return rel, rows, s

    try:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            for n, (rel, rows, s) in enumerate(ex.map(one, enumerate(todo)), 1):
                units[rel] = (rows, s)
                if a.verbose:
                    print(f'[{n}/{len(todo)}] {rel}: {s["covered"]}/{s["target"]} lines, {s["rows"]} rows, '
                          f'{s["runs"]} runs ({s["errors"]} with errors, {s["failed"]} failed), {s["sec"]} s'
                          + (f', -D {" ".join(s["defines"])}' if s['defines'] else ''), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if a.only:  # 指定外のファイルは解析していない。前の結果を残す(書き直すと消える)
        only = set(a.only)
        for rel, s in old_units.items():
            if rel not in only and rel not in units:
                units[rel] = (old_rows.get(rel, []), s)
    rows = sorted((r for rs, _ in units.values() for r in rs), key=row_key)
    tmp = alt + '.tmp'
    with open(tmp, 'wb') as f:
        for r in rows:
            f.write(r + b'\n')
    replace(tmp, alt)
    tot = collections.Counter()
    for _, s in units.values():
        for k in ('target', 'covered', 'rows', 'runs', 'errors', 'failed'):
            tot[k] += s.get(k, 0)
    state = {'version': ALT_VERSION, 'finished': time.time(), 'seconds': round(time.time() - t0, 1),
             'files': len(units), 'reparsed': len(todo), 'no_tu': no_tu, 'total': dict(tot),
             'units': {rel: s for rel, (_, s) in sorted(units.items())}}
    tmp = state_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    replace(tmp, state_path)
    print(f'alt pass: {len(units)} files with inactive #if branches ({len(todo)} analyzed, {len(units) - len(todo)} unchanged'
          + (f', {no_tu} without a TU' if no_tu else '') + f'); {tot["covered"]}/{tot["target"]} inactive lines parsed, '
          f'{len(rows)} rows -> {alt} ({time.time() - t0:.0f} s)')
    if tot['failed']:
        print(f'  {tot["failed"]} files could not be analyzed (see "failed" in {state_path})')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='altpass', description='index the code inside inactive #if branches (best effort)')
    ap.add_argument('--index', required=True, help='the index TSV built by ltidx')
    ap.add_argument('--root', help='source root (default: from <index>.recipe.json)')
    ap.add_argument('--cdb', help='folder of compile_commands.json (default: from <index>.recipe.json)')
    ap.add_argument('--jobs', type=int, help='parallel ltidx runs (default: from the recipe, or half of the cores)')
    ap.add_argument('--passes', type=int, default=PASSES, help=f'max reparses per file (default {PASSES})')
    ap.add_argument('--ltidx', help='ltidx.exe (default: LTIDX, indexer\\build, or the downloaded one)')
    ap.add_argument('--no-define', action='store_true', help='do not pass -D for macros that are defined nowhere')
    ap.add_argument('--force', action='store_true', help='analyze every file again')
    ap.add_argument('--only', nargs='*', help='only these files (paths relative to the root)')
    ap.add_argument('--verbose', action='store_true', help='one line per analyzed file')
    return run(ap.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main())
