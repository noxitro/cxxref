"""rg で見つかる行が、cxxref(ltq refs と MCP サーバーの refs)の結果にも出ているかを突き合わせる。

  python -I bench/parity.py --root <ソースのルート> --index <索引.tsv> [--names A B ...] [--sample N] [--under 接頭辞]
                            [--no-ignore] [--out 結果.tsv]

名前ごとに `rg -w -F <名前>`(.gitignore に従う、ふつうのエージェントの探し方。--no-ignore を付けると無視されるファイルも)の
一致行と、ltq refs の出力に 1 行ずつ出ている行(パス:行)を比べ、rg の一致行を次に分ける。
  listed    その形の出力に 1 行ずつ出ている
  folded    その形の出力では、その行のあるフォルダが件数で出ている(「<フォルダ>/  N matches in M files」。path で 1 行ずつ出る)
  area      その行のあるフォルダは並んでいないが、上の階層が件数で出ている(「... N more folders (M matches) under: 上の階層/ 件数」)
  hidden    その形の出力のどこにも、その行のフォルダも上の階層も出ていない(並べきれなかった階層か、上限で切れた末尾)
  other     索引がその行を同じ名前の別の記号に解決している(修飾名で問うたときだけ。結果は件数で出る)
  missing   全文(上限なし)にも、フォルダごとの件数にも出ていない(漏れ。0 でなければ直す)
folded・area・hidden は、ltq がその行に付けた分類ごとにも数える(コードでありうる分類: index・inactive・dropped・macro・unparsed・
other・nonsrc、コードではない分類: comment・local・string・directive)。

出力の形は 4 つ: full(全文)、c1(--compact)、c2(--compact を 2 つ)は 1 節の行数の上限を外して測る(形そのものの違い)。
mcp は、MCP サーバーが実際に返す文面(mcp/server.py の run_ltq。既定の上限 200 行のまま、結果が 18,000 バイトを超えると
full → c1 → c2 → c2 で 1 節 60・25・10・5・2 行、と短くし、それでも超えると末尾を切る)。エージェントが受け取るのはこの形。

--sample N は、差分の索引(<索引>.alt.tsv。無効な #if の中に出てくる名前)から N 個を、名前のハッシュの順で選ぶ
(乱数を使わないので、同じ索引なら同じ名前になる)。--under を付けると、そのフォルダの行に出てくる名前だけ。
"""
import argparse
import collections
import contextlib
import glob
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'query'))
sys.path.insert(0, os.path.join(HERE, '..', 'mcp'))
import ltq  # noqa: E402
import server  # noqa: E402

LOC = re.compile(r'([^\s|\[\]()]+):(\d+)(?=\s|$)')
DIR = re.compile(r'\s+(.+)/  (\d+) matches in \d+ files$')
TAIL = re.compile(r'\s+\.\.\. \d+ more folders \(\d+ matches\) under: (.+)$')
FILE = re.compile(r'\s+(\S+)  (\d+) matches$')  # 1 つのフォルダに収まるときの、ファイルごとの件数
MOREFILES = re.compile(r'\s+\.\.\. \d+ more files \(\d+ matches\): (.+)$')
MODES = (('full', []), ('c1', ['--compact']), ('c2', ['--compact', '--compact']))
# 「索引に無い一致」の節の、分類の見出しの書き出し
GROUPS = (('inside comments', 'comment'), ('inside inactive', 'inactive'), ('inside macro arguments', 'dropped'),
          ('local variables', 'local'), ('inside macro definitions', 'macro'), ('inside string', 'string'),
          ('in #pragma', 'directive'), ('in files no indexed TU', 'unparsed'), ('other ->', 'other'))
CODE = ('index', 'inactive', 'dropped', 'macro', 'unparsed', 'other', 'nonsrc')
CATS = ('listed', 'folded', 'area', 'hidden', 'other', 'missing')


def find_rg():
    """rg.exe。PATH に無ければ、Copilot CLI に付いてくるもの(エージェントが実際に使うもの)の新しい版。"""
    p = shutil.which('rg')
    if p:
        return p
    base = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'copilot', 'pkg', 'win32-x64')
    found = glob.glob(os.path.join(base, '*', 'ripgrep', 'bin', 'win32-x64', 'rg.exe'))

    def ver(path):
        return tuple(int(x) for x in re.findall(r'\d+', path[len(base):].split(os.sep)[1]))

    return max(found, key=ver) if found else None


def rg_lines(rg, root, name, more=()):
    """{(ルートからの相対パス, 行)}。rg の既定(.gitignore に従い、隠しファイルとバイナリを飛ばす)のまま。"""
    r = subprocess.run([rg, '-w', '-F', '-n', '--no-heading', '--color', 'never', '--path-separator', '/', *more, '-e', name, './'],
                       cwd=root, stdin=subprocess.DEVNULL, capture_output=True)
    out = set()
    for line in r.stdout.split(b'\n'):
        m = re.match(rb'(?:\./)?(.+?):(\d+):', line)
        if m:
            out.add((m.group(1).decode('utf-8', 'replace'), int(m.group(2))))
    return out


class Shown:
    """refs の出力に出ているもの。locs: {(パス, 行): 分類}(1 行ずつ出ている行)、dirs: {(フォルダ, 分類)}(件数で出ている
    フォルダ)、tops: {(上の階層, 分類)}(並べきれなかったフォルダをまとめた階層)。ソースの文面(` | ` より後)は見ない。"""

    def __init__(self, text):
        self.text = text
        self.size = len(text.encode('utf-8'))
        self.locs, self.dirs, self.tops, self.files = {}, set(), set(), set()
        group, last_dir = '', ''
        for line in text.splitlines():
            if line.startswith('== '):
                group = 'nonsrc' if 'not C/C++ sources' in line else '' if line.startswith('== text matches') else 'index'
                continue
            if not line.startswith('  '):
                continue
            body = line.strip()
            g = next((k for head, k in GROUPS if body.startswith(head)), None)
            if g and group != 'nonsrc' and re.search(r': \d+$', body):
                group = g
                continue
            m = DIR.match(line)
            if m:
                self.dirs.add((m.group(1), group))
                continue
            m = FILE.match(line)
            if m:
                self.files.add((m.group(1), group))
                last_dir = m.group(1).rsplit('/', 1)[0]
                continue
            m = MOREFILES.match(line)
            if m:
                for part in m.group(1).split(', '):
                    t = re.match(r'(.+) \d+$', part)
                    if t:
                        self.files.add((f'{last_dir}/{t.group(1)}', group))
                continue
            m = TAIL.match(line)
            if m:
                for part in m.group(1).split(', '):
                    t = re.match(r'(.+)/ \d+$', part)
                    if t:
                        self.tops.add((t.group(1), group))
                continue
            for m in LOC.finditer(line.split('  | ', 1)[0]):
                self.locs.setdefault((m.group(1), int(m.group(2))), group)

    def where(self, p, ln, group):
        """(p, ln) の行(分類は group)が、この出力のどこに出ているか。"""
        if (p, ln) in self.locs:
            return 'listed'
        d = p.rsplit('/', 1)[0] if '/' in p else '.'
        if (d, group) in self.dirs or (p, group) in self.files:
            return 'folded'
        if any(g == group and (t == '.' and '/' not in d or d == t or d.startswith(t + '/')) for t, g in self.tops):
            return 'area'
        return 'hidden'


def ltq_shown(idx, root, index, name, extra):
    idx.renew()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ltq.main(['--root', root, '--index', index, 'refs', name] + extra, idx=idx)
    return Shown(out.getvalue())


def indexed_lines(idx, short):
    """索引(差分を含む)が、名前の末尾が short の記号として解決している行。"""
    b = short.encode('utf-8')
    return {(r[3], r[4]) for r in idx.rows([b + b'\t'], lambda raw: True) if r[0].rsplit('::', 1)[-1] == short}


def sample(index, n, under=''):
    """<索引>.alt.tsv の記号の名前(末尾)から n 個。識別子として rg -w と同じに探せるものだけ。
    under を渡すと、そのフォルダ(ルートからの相対パスの接頭辞)の行に出てくる名前だけ。"""
    names = set()
    try:
        with open(index + ltq.ALT, 'rb') as f:
            for line in f:
                cols = line.split(b'\t')
                if under and (len(cols) < 4 or not cols[3].decode('utf-8', 'replace').lower().startswith(under.lower())):
                    continue
                s = cols[0].decode('utf-8', 'replace').rsplit('::', 1)[-1]
                if re.fullmatch(r'[A-Za-z_]\w{3,}', s):
                    names.add(s)
    except OSError:
        pass
    return sorted(names, key=lambda s: hashlib.md5(s.encode()).hexdigest())[:n]


def main():
    ap = argparse.ArgumentParser(prog='parity')
    ap.add_argument('--root', required=True)
    ap.add_argument('--index', required=True)
    ap.add_argument('--names', nargs='*', default=[])
    ap.add_argument('--sample', type=int, default=0, help='also take N names from <index>.alt.tsv')
    ap.add_argument('--under', default='', help='with --sample: only names that appear under this root-relative prefix')
    ap.add_argument('--no-ignore', action='store_true', help='pass --no-ignore to rg (also search files .gitignore hides)')
    ap.add_argument('--rg-short', action='store_true', help='修飾名でも rg は末尾の名前で引く(別の記号に解決された行は other)')
    ap.add_argument('--rg', help='rg.exe (default: PATH, or the one bundled with Copilot CLI)')
    ap.add_argument('--out', help='write the lines that the MCP form does not list one by one (one per row) to this TSV')
    a = ap.parse_args()
    rg = a.rg or find_rg()
    if not rg:
        print('rg not found; pass --rg', file=sys.stderr)
        return 2
    root = os.path.abspath(a.root)
    index = os.path.abspath(a.index)
    cfg = server.Config(root, index, None)  # 自動更新なし。上限は既定(server.MAX_BYTES)
    idx = cfg.open_index([index], root)
    names = list(dict.fromkeys(a.names + sample(index, a.sample, a.under)))
    forms = [m for m, _ in MODES] + ['mcp']
    total = {m: dict.fromkeys(CATS, 0) for m in forms}
    by_group = {m: collections.Counter() for m in forms}  # (分け方, 分類) -> 行数
    shape = collections.Counter()
    secs = []
    rows_out = []
    extra_only = 0

    # MCP サーバーが最後にどの形で ltq を呼んだかを覚える(run_ltq は収まる形が出るまで ltq.main を呼び直す)
    last = []
    real_main = ltq.main

    def spy(argv, idx=None):
        last[:] = argv
        return real_main(argv, idx=idx)

    ltq.main = spy
    print('name\trg\t' + '\t'.join(f'{m}:listed' for m in forms) + '\tmcp:folded\tmcp:area\tmcp:hidden\tother\tmissing\tltq_only'
          '\tbytes(full/c1/c2/mcp)\tmcp_form\tmcp_sec')
    for name in names:
        short = name.rsplit('::', 1)[-1]
        # --rg-short: 修飾名で問うときも、rg は末尾の名前で引く(エージェントが呼び出し元を探すときの引き方)
        want = rg_lines(rg, root, short if a.rg_short else name, ['--no-ignore'] if a.no_ignore else [])
        known = indexed_lines(idx, short)
        t0 = time.time()  # MCP の形を先に測る(この名前の初回。ソースの走査と文面の検索を含む時間)
        _, text = server.run_ltq(cfg, ['refs', name])
        sec = time.time() - t0
        secs.append(sec)
        form = ('full', 'c1', 'c2')[last.count('--compact')] + (f'+limit{last[last.index("--limit") + 1]}' if '--limit' in last else '')
        if 'output cut at' in text:
            form += '+cut'
        shape[form] += 1
        shown = {m: ltq_shown(idx, root, index, name, extra + ['--limit', '1000000']) for m, extra in MODES}
        shown['mcp'] = Shown(text)
        full = shown['full']
        per = {m: dict.fromkeys(CATS, 0) for m in forms}
        for p, ln in sorted(want):
            d = p.rsplit('/', 1)[0] if '/' in p else '.'
            # 全文での分類。全文でも 1 行ずつは出ない行(文字列・指令が 10 行を超えるとき)は、フォルダの件数の分類
            g = full.locs.get((p, ln)) or next((k for f, k in sorted(full.dirs) if f == d), None)
            for m in forms:
                if g is not None:
                    c = shown[m].where(p, ln, g)
                elif (p, ln) in known:
                    c = 'other'
                else:
                    c = 'missing'
                per[m][c] += 1
                total[m][c] += 1
                if c in ('folded', 'area', 'hidden'):
                    by_group[m][(c, g)] += 1
                if m == 'mcp' and c != 'listed':
                    rows_out.append((name, c + (':' + g if g else ''), f'{p}:{ln}'))
        only = len(set(full.locs) - want)
        extra_only += only
        f = per['mcp']
        print(f'{name}\t{len(want)}\t' + '\t'.join(str(per[m]['listed']) for m in forms) +
              f'\t{f["folded"]}\t{f["area"]}\t{f["hidden"]}\t{f["other"]}\t{f["missing"]}\t{only}'
              f'\t{"/".join(str(shown[m].size) for m in forms)}\t{form}\t{sec:.1f}', flush=True)
    ltq.main = real_main
    print()
    print(f'names: {len(names)}; rg lines: {sum(total["full"].values())}')

    def groups(m, c):
        g = sorted(((k[1], n) for k, n in by_group[m].items() if k[0] == c), key=lambda kv: -kv[1])
        code = sum(n for k, n in g if k in CODE)
        return (f' (code-like {code}: ' + ', '.join(f'{k} {n}' for k, n in g if k in CODE) + '; not code: '
                + ', '.join(f'{k} {n}' for k, n in g if k not in CODE) + ')') if g else ''

    for m in forms:
        t = total[m]
        label = 'mcp (what the MCP server returns)' if m == 'mcp' else f'{m} (no --limit)'
        print(f'  {label}: listed {t["listed"]}, folded {t["folded"]}{groups(m, "folded")}, '
              f'area {t["area"]}{groups(m, "area")}, hidden {t["hidden"]}{groups(m, "hidden")}, '
              f'other {t["other"]}, missing {t["missing"]}')
    print('  form returned by the MCP server: ' + ', '.join(f'{k} {v}' for k, v in sorted(shape.items())))
    if secs:
        s = sorted(secs)
        print(f'  MCP refs time per name: median {s[len(s) // 2]:.1f} s, max {s[-1]:.1f} s')
    print(f'  lines only in ltq (not in rg: ignored files, rows where the name is not spelled on the line): {extra_only}')
    if a.out:
        with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
            f.write('name\tkind\tlocation\n')
            for r in rows_out:
                f.write('\t'.join(r) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
