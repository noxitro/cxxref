"""段階 2 の問い合わせ CLI: ltidx の索引(TSV)を引き、ソースのテキスト一致と突き合わせる。

  ltq refs <名前>            参照・定義・dep 候補と、索引に無いテキスト一致の理由
  ltq check <path:line>...   その行が索引でどう扱われているか(参照・コメント・無効な #if)

エージェントが読む前提なので、出力の見出しは英語(短く、文字化けしない)。
"""
import argparse
import bisect
import codecs
import collections
import contextlib
import mmap
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

SRC_EXT = {'.h', '.hh', '.hpp', '.hxx', '.c', '.cc', '.cpp', '.cxx', '.inl', '.ipp', '.inc', '.ixx', '.cppm', '.tpp', '.txx',
           '.m', '.mm'}
TU_EXT = {'.c', '.cc', '.cpp', '.cxx', '.cppm', '.ixx', '.m', '.mm'}  # 単独でコンパイルされうるもの
DEFS = ('def', 'decl')
OVERRIDE = 'override'  # 3 列目は呼び出し元ではなく、上書きされる基底のメソッド
CP_CODECS = {65001: None, 51932: 'euc_jp', 20932: 'euc_jp', 28591: 'latin-1'}  # ほかは cp<番号>
SKIP_SCAN_FILES = 64  # 範囲を引くファイルがこれより多ければ、.skip.tsv を 1 つずつ find せず、1 回通して読む
SIDE = ('.tu.tsv', '.skip.tsv', '.deps.tsv')  # 索引本体の隣に ltidx が書くファイル
SEEN_CACHE = '.seen.cache'  # .deps.tsv に載るパスの一覧の写し(ltq が書く)
READ_CHUNK = 64 << 20


def index_signature(paths):
    """索引のファイル(本体と隣のファイル)の (パス, 大きさ, 更新日時)。MCP サーバーが、開いた索引を使い回してよいかを見る。"""
    out = []
    for p in paths:
        for f in (p,) + tuple(p + s for s in SIDE):
            try:
                st = os.stat(f)
                out.append((f, st.st_size, st.st_mtime_ns))
            except OSError:
                out.append((f, None, None))
    return tuple(out)


def deps_paths(dp):
    """.deps.tsv(1 行目は見出し、2 列目が TU の読んだファイル)に載っているパスの集合(bytes)。
    大きな索引では .deps.tsv が数 GB あり、読むだけで 30 秒近くかかる(UE5 の 6.7 GB で 27 秒)ので、
    .deps.tsv の大きさと更新日時を鍵にした一覧(<索引>.seen.cache)を隣に書き、次からはそれを読む。
    書けないフォルダでは書かずに続ける。"""
    st = os.stat(dp)
    key = f'cxxref-seen 1 {st.st_size} {st.st_mtime_ns}\n'.encode()
    cache = dp[:-len('.deps.tsv')] + SEEN_CACHE
    try:
        with open(cache, 'rb') as f:
            data = f.read()
        if data.startswith(key):
            return set(data[len(key):].split(b'\n')) - {b''}
    except OSError:
        pass
    raw, rest = set(), b''
    pat = re.compile(rb'\t([^\t\r\n]+)')
    with open(dp, 'rb') as f:
        next(f, None)
        while True:  # 一度に全部を読むと、大きな索引では数 GB のメモリを使うので、行の切れ目で区切って読む
            block = f.read(READ_CHUNK)
            if not block:
                raw.update(pat.findall(rest))
                break
            block = rest + block
            cut = block.rfind(b'\n') + 1
            raw.update(pat.findall(block, 0, cut))
            rest = block[cut:]
    tmp = f'{cache}.{os.getpid()}.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(key + b'\n'.join(sorted(raw)) + b'\n')
        os.replace(tmp, cache)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
    return raw


def cp_codec(cp):
    """Windows のコードページ番号を Python の codec 名に。UTF-8 か分からなければ None。"""
    name = CP_CODECS.get(cp, f'cp{cp}')
    if name is None:
        return None
    try:
        codecs.lookup(name)
    except LookupError:
        return None
    return name


def to_utf8(data, codec):
    """ltidx と同じく、UTF-16(BOM 付き)と UTF-8 として正しくないソースを UTF-8 のバイト列に直す。
    後者は索引を作ったときのコードページ(codec)で読む。索引の列番号は直した後のバイト位置。"""
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try:
            return data.decode('utf-16').encode('utf-8')
        except UnicodeDecodeError:
            return data
    if codec is None or data.startswith(b'\xef\xbb\xbf'):
        return data
    try:
        data.decode('utf-8')
        return data
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(codec).encode('utf-8')
    except UnicodeDecodeError:
        return data


def longest_increasing(pairs):
    """(old, new) の並び(old の昇順)から、new も増えていく最長の部分列。"""
    tails, vals, prev = [], [], [None] * len(pairs)  # tails[k]: 長さ k+1 の列の末尾の添え字、vals[k]: その new
    for i, (_, n) in enumerate(pairs):
        k = bisect.bisect_left(vals, n)
        prev[i] = tails[k - 1] if k else None
        if k == len(tails):
            tails.append(i)
            vals.append(n)
        else:
            tails[k], vals[k] = i, n
    out, i = [], tails[-1] if tails else None
    while i is not None:
        out.append(pairs[i])
        i = prev[i]
    return out[::-1]


def grep_lines(path, needles):
    """path のファイルのうち、needles(bytes)のどれかを含む行(改行を除く)を、ファイルの順に返す。
    改行で始まる needle は、残りの部分で始まる行に当たる。
    大きな索引を 1 行ずつ Python で調べると遅いので、mmap の find で当たった行だけを取り出す。"""
    needles = [n for n in needles if n]
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if not needles or size == 0:
        return []
    found = {}
    with open(path, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        for n in needles:
            head = n[:1] == b'\n'
            if head and m[:len(n) - 1] == n[1:]:
                i = 0  # 1 行目
            else:
                i = m.find(n)
                if head and i >= 0:
                    i += 1  # find が返すのは直前の改行の位置
            while i >= 0:
                b = m.rfind(b'\n', 0, i) + 1
                e = m.find(b'\n', i)
                e = size if e < 0 else e
                if b not in found:
                    found[b] = m[b:e].rstrip(b'\r')
                i = m.find(n, e)
                if head and i >= 0:
                    i += 1
    return [found[b] for b in sorted(found)]


def canon_line(raw):
    """ltidx が索引に入れるソース行と同じ形(前後の空白を除き 200 バイトまで、タブと改行は空白)。"""
    s = raw.strip()[:200]
    return s.translate(bytes.maketrans(b'\t\r\n', b'   ')).decode('utf-8', 'replace')


class Part:
    """1 つの構成(Debug、Release など)の索引。名前はファイル名の拡張子を除いたもの。"""

    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.mtime = os.path.getmtime(path)


class Index:
    """1 つ以上の構成の索引をまとめて引く。行は和集合にし、一部の構成にしか無い行には構成名を付ける。"""

    def __init__(self, paths, root):
        self.root = os.path.abspath(root)
        self.parts = [Part(p) for p in paths]
        self.mtime = min(p.mtime for p in self.parts)  # 鮮度は最も古い索引で判定する
        self._seen = None
        self._fresh = None
        self._roots = None
        self._codec = False
        self._sources = None
        self._lines = {}
        self._maps = {}  # (構成名, path) -> 行の付け直しの対応。rows() が作り、skips() も使う
        self._tus = {}
        self._memo = {}

    def renew(self):
        """ソースから読んだもの(走査・鮮度・ファイルの行・行の付け直し・問い合わせの途中の結果)を捨てる。
        索引のファイルから読んだもの(seen・TU の記録・codec)は残す。MCP サーバーが、索引が変わっていない間に
        Index を使い回すときに呼ぶ。"""
        self._fresh = None
        self._sources = None
        self._lines = {}
        self._maps = {}
        self._memo = {}

    def cached(self, key, make):
        """同じ Index で同じ問い合わせを繰り返すとき(MCP サーバーが結果を短い形で出し直すとき)に、
        索引の検索とテキスト検索をやり直さない。renew() で捨てる。"""
        if key not in self._memo:
            self._memo[key] = make()
        return self._memo[key]

    def _current(self, path):
        if path not in self._lines:
            try:
                with open(os.path.join(self.root, path), 'rb') as f:
                    data = to_utf8(f.read(), self.codec())
            except OSError:
                data = None
            self._lines[path] = None if data is None else [canon_line(x) for x in data.split(b'\n')]
        return self._lines[path]

    def _stale(self, part):
        """その索引の後に変わった(か消えた)、索引にあるファイル。"""
        out = set()
        for path in self.freshness()[0]:
            try:
                if os.path.getmtime(os.path.join(self.root, path)) > part.mtime:
                    out.add(path)
            except OSError:
                out.add(path)
        src = self.sources()
        return out | {p for p in self.seen()[part.name]
                      if p not in src and not os.path.exists(os.path.join(self.root, p))}

    def _line_map(self, path, anchors):
        """索引にある (行, ソース行) を今のファイルから探し、{索引の行: 今の行 か None}。
        1) 索引でも今のファイルでも 1 回しか現れない行を対応づけ、今の行番号が増えていく最長の並びだけを固定する
           (行の入れ替えで食い違う対応は捨てる)。
        2) 残りの行(`Run();` のように何度も現れる行など)は、前後の固定した行の間だけを、
           直前に対応づけた行のずれの近くから探す。1 つの行に 2 つを割り当てない。"""
        old = {}
        for line, src in anchors:
            old.setdefault(line, src)
        lines = sorted(old)
        cur = self._current(path)
        if cur is None:
            return {line: None for line in lines}
        pos = {}
        for i, s in enumerate(cur, 1):
            pos.setdefault(s, []).append(i)
        count = {}
        for s in old.values():
            count[s] = count.get(s, 0) + 1
        pairs = [(o, pos[old[o]][0]) for o in lines if count[old[o]] == 1 and len(pos.get(old[o], ())) == 1]
        res = dict(longest_increasing(pairs))
        fixed = sorted(res)
        prev_o, prev_n = 0, 0
        for o in lines:
            if o in res:
                prev_o, prev_n = o, res[o]
                continue
            i = bisect.bisect_right(fixed, o)
            hi = res[fixed[i]] if i < len(fixed) else len(cur) + 1
            cand = pos.get(old[o], [])
            want = o + (prev_n - prev_o)
            j = bisect.bisect_left(cand, want)
            best = None
            for n in (cand[j - 1] if j > 0 else None, cand[j] if j < len(cand) else None):
                if n is not None and prev_n < n < hi and (best is None or abs(n - want) < abs(best - want)):
                    best = n
            if best is None and j < len(cand) and cand[j] <= prev_n:  # want が直前の行より前にある
                k = bisect.bisect_right(cand, prev_n)
                best = cand[k] if k < len(cand) and cand[k] < hi else None
            res[o] = best
            if best is not None:
                prev_o, prev_n = o, best
        return res

    def _set_map(self, part, path, lmap):
        known = sorted((o, n - o) for o, n in lmap.items() if n is not None)
        self._maps[(part.name, path)] = (lmap, [o for o, _ in known], [d for _, d in known])

    def shift_at(self, part, path, line):
        """変わったファイルで、索引の line 行が今どれだけずれているか(最も近い、対応のついた行から)。"""
        m = self._maps.get((part.name, path))
        if not m or not m[1]:
            return 0
        olds, shifts = m[1], m[2]
        i = bisect.bisect_left(olds, line)
        near = [k for k in (i - 1, i) if 0 <= k < len(olds)]
        return shifts[min(near, key=lambda k: abs(olds[k] - line))]

    def _relocate(self, part, path, line):
        """(今の行番号, 注記)。行が見つからなければ、近くの変わっていない行のずれから推定する。"""
        cur = self._current(path)
        if cur is None:
            return line, 'file deleted after indexing'
        new = self._maps[(part.name, path)][0].get(line)
        if new is not None:
            return new, ('' if new == line else f'moved from line {line}')
        shift = self.shift_at(part, path, line)
        est = min(max(line + shift, 1), max(len(cur), 1))
        return est, 'line changed or removed after indexing' + (f'; estimated from line {line}' if est != line else '')

    def rows(self, needles, keep):
        """needles(bytes)のどれかを含み、keep(bytes の行)が真の行だけを
        (sym, role, caller, path, line, col, src, note, configs) にして返す。
        note は行の付け直しの注記、configs はその行がある構成名の frozenset。
        索引の後に変わったファイルの行は、索引にあるソース行の文字列で今のファイルから探し直す。"""
        merged = {}
        for part in self.parts:
            stale = self._stale(part)
            sb = {p.encode('utf-8') for p in stale}
            anchors, picked = {}, []
            # 変わったファイルの行は、問い合わせに合わなくても行の付け直しの手がかりとして読む
            for raw in grep_lines(part.path, list(needles) + [b'\t' + p + b':' for p in sb]):
                hit = keep(raw)
                if not hit:
                    q = raw.split(b'\t', 4)
                    if len(q) < 5 or q[3].rsplit(b':', 2)[0] not in sb:
                        continue
                p = raw.split(b'\t', 4)
                if len(p) < 5:
                    continue
                loc = p[3].rsplit(b':', 2)
                path, src = loc[0].decode('utf-8', 'replace'), p[4].decode('utf-8', 'replace')
                if path in stale:
                    anchors.setdefault(path, []).append((int(loc[1]), src))
                if hit:
                    picked.append((p, path, int(loc[1]), int(loc[2]), src))
            for path, a in anchors.items():
                self._set_map(part, path, self._line_map(path, a))
            for p, path, line, col, src in picked:
                note = ''
                if path in stale:
                    line, note = self._relocate(part, path, line)
                k = (p[0].decode('utf-8', 'replace'), p[1].decode(), p[2].decode('utf-8', 'replace'), path, line, col)
                if k in merged:
                    merged[k][2].add(part.name)
                    if merged[k][1] and not note:  # 付け直しの要らない(新しい)構成のソース行を採る
                        merged[k][0:2] = [src, note]
                else:
                    merged[k] = [src, note, {part.name}]
        # 古い構成で行を推定した参照は、別の構成に同じ参照(行以外が同じ)の確かな行があれば捨てる
        exact = {}
        for k, v in merged.items():
            if not v[1].startswith('line changed'):
                exact.setdefault(k[:4] + (k[5],), set()).update(v[2])
        return [k + (v[0], v[1], frozenset(v[2])) for k, v in merged.items()
                if not (v[1].startswith('line changed') and exact.get(k[:4] + (k[5],), set()) - v[2])]

    def skips(self, paths):
        """paths のファイルについて、コメントと無効な #if の範囲を {path: [(kind, (l,c), (l,c), 構成名)]} で返す。
        索引の後に変わったファイルの範囲は、rows() が作った行の対応で近くのずれだけ動かす(rows() の後に呼ぶ)。"""
        res = {}
        want = {p.encode('utf-8') for p in paths}
        for part in self.parts:
            sp = part.path + '.skip.tsv'
            if not os.path.exists(sp):
                continue
            if len(want) > SKIP_SCAN_FILES:
                # ファイルが多いと、1 つずつ find するより、1 回通して読むほうが速い(コードベース C の 108 MB の .skip.tsv で、問い合わせ全体が 10 秒 -> 2.2 秒)
                with open(sp, 'rb') as f:
                    lines = [x.rstrip(b'\r') for x in f.read().split(b'\n') if x[:x.find(b'\t')] in want]
            else:
                lines = grep_lines(sp, [w + b'\t' for w in want])
            for raw in lines:
                    p = raw.split(b'\t')
                    if len(p) != 4 or p[0] not in want:
                        continue
                    b, e = (tuple(int(x) for x in s.split(b':')) for s in p[2:4])
                    path = p[0].decode('utf-8')
                    if (part.name, path) in self._maps:
                        b = (b[0] + self.shift_at(part, path, b[0]), b[1])
                        e = (e[0] + self.shift_at(part, path, e[0]), e[1])
                    res.setdefault(path, []).append((p[1].decode(), b, e, part.name))
        return res

    def seen(self):
        """{構成名: その構成のどれかの TU が読んだファイルの集合}。TU の主ファイル、TU が読んだファイルの記録(.deps.tsv)、
        コメントか無効な #if のあるファイル、索引に行のあるファイル。.deps.tsv の無い古い索引では、
        参照もコメントも無いヘッダーは数えられない。"""
        if self._seen is None:
            self._seen = {}
            for part in self.parts:
                s = set(self._tu(part)[2])
                raw_paths = set()
                dp = part.path + '.deps.tsv'
                if os.path.exists(dp):  # TU が読んだファイルの記録が、索引とコメントのあるファイルをすべて含む
                    s.update(p.decode('utf-8', 'replace') for p in deps_paths(dp))
                    self._seen[part.name] = s
                    continue
                with open(part.path, 'rb') as f:
                    last = None
                    for raw in f:
                        q = raw.split(b'\t', 4)
                        if len(q) < 5:
                            continue
                        loc = q[3]
                        if last is not None and loc.startswith(last) and loc[len(last):len(last) + 1] == b':':
                            continue  # 同じファイルの行が続く(ltidx は TU ごとにまとめて書く)
                        last = loc.rsplit(b':', 2)[0]
                        raw_paths.add(last)
                sp = part.path + '.skip.tsv'
                if os.path.exists(sp):
                    with open(sp, 'rb') as f:
                        next(f, None)
                        raw_paths.update(raw.split(b'\t', 1)[0] for raw in f)
                s.update(p.decode('utf-8', 'replace') for p in raw_paths)
                self._seen[part.name] = s
        return self._seen

    def partial(self, r):
        """その行が、ファイルを読んだ構成の一部にしか無ければ、ある構成名の並び。全部にあれば ''。"""
        if len(self.parts) < 2:
            return ''
        saw = {n for n, s in self.seen().items() if r[3] in s} | set(r[8])
        return '' if saw <= r[8] else ', '.join(sorted(r[8]))

    def _tu(self, part):
        """(TU 数, [(エラーのあった TU, 最初のエラー)], TU の主ファイルの集合, {コードページ: TU 数})"""
        if part.name not in self._tus:
            self._tus[part.name] = self._read_tu(part)
        return self._tus[part.name]

    def _read_tu(self, part):
        tp = part.path + '.tu.tsv'
        total, bad, files, cps = 0, [], set(), {}
        if os.path.exists(tp):
            with open(tp, encoding='utf-8', errors='replace') as f:
                next(f, None)
                for line in f:
                    c = line.rstrip('\n').split('\t')
                    if len(c) < 2:
                        continue
                    total += 1
                    files.add(self.rel(c[0]))
                    if c[1] != '1':
                        bad.append((self.rel(c[0]), c[5] if len(c) > 5 else ''))
                    if len(c) > 7 and c[7].isdigit():
                        cps[int(c[7])] = cps.get(int(c[7]), 0) + 1
        return total, bad, files, cps

    def codec(self):
        """UTF-8 でないソースを読む codec。ltidx が TU ごとに記録したコードページのうち、UTF-8(65001)以外で最も多いもの。
        /utf-8 の TU が多くても、一部の TU が Shift_JIS で読んだファイルは ltidx が直しているので、直す側に倒す
        (UTF-8 として正しいファイルは直さないので変わらない)。記録の無い古い索引では None(バイト列のまま突き合わせる)。"""
        if self._codec is False:
            cps = {}
            for part in self.parts:
                for cp, n in self._tu(part)[3].items():
                    if cp != 65001:
                        cps[cp] = cps.get(cp, 0) + n
            self._codec = cp_codec(max(cps, key=cps.get)) if cps else None
        return self._codec

    def tu_status(self):
        """(TU 数, [(エラーのあった TU, 最初のエラー, 構成名)], TU の主ファイルの集合)。構成をまたいで足す。"""
        total, bad, files = 0, [], set()
        for part in self.parts:
            t, b, f, _ = self._tu(part)
            total += t
            bad += [(p, e, part.name) for p, e in b]
            files |= f
        return total, bad, files

    def rel(self, p):
        """ルートからの相対パス。ルートは、渡された書き方と実体のパス(8.3 形式の短い名前を長い名前にしたもの)の両方で見る。"""
        a = os.path.abspath(p)
        if self._roots is None:
            self._roots = sorted({r.lower() for r in (self.root, os.path.realpath(self.root))}, key=len, reverse=True)
        low = a.lower()
        for r in self._roots:
            if low.startswith(r + os.sep):
                return a[len(r) + 1:].replace('\\', '/')
        return a.replace('\\', '/')

    def freshness(self):
        """索引の後に変わったソースと、どの構成でも読まれていない新しいソース(TU の候補)。"""
        if self._fresh is not None:
            return self._fresh
        changed, new = [], []
        seen = set().union(*self.seen().values())
        for rel, fp, m in walk_sources(self, set()):
            if m <= self.mtime:
                continue
            if rel in seen:
                changed.append(rel)
            elif os.path.splitext(fp)[1].lower() in TU_EXT:
                new.append(rel)
        src = self.sources()
        missing = sorted(p for p in self.tu_status()[2]
                         if p not in src and not os.path.exists(os.path.join(self.root, p)))
        self._fresh = (sorted(changed), sorted(new), missing)
        return self._fresh

    def sources(self):
        """{ルートからの相対パス: (絶対パス, 更新日時)}。ルート配下の C/C++ のソースすべて(. で始まるフォルダを除く)。
        1 回だけ走査する。Windows では scandir の項目が更新日時を持っているので、ファイルごとの stat が要らない。"""
        if self._sources is None:
            out, todo, n = {}, [self.root], len(self.root) + 1
            while todo:
                try:
                    it = os.scandir(todo.pop())
                except OSError:
                    continue
                with it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if not e.name.startswith('.'):
                                    todo.append(e.path)
                            elif os.path.splitext(e.name)[1].lower() in SRC_EXT:
                                out[e.path[n:].replace('\\', '/')] = (e.path, e.stat().st_mtime)
                        except OSError:
                            continue
            self._sources = out
        return self._sources

    def header(self):
        def built(part):
            return time.strftime('%Y-%m-%d %H:%M', time.localtime(part.mtime))

        multi = len(self.parts) > 1
        if not multi:
            part = self.parts[0]
            total, bad, _, _ = self._tu(part)
            print(f'index: {self.rel(part.path)} (built {built(part)}; TU {total - len(bad)}/{total} without errors)')
        else:
            print(f'index: {len(self.parts)} configurations, merged')
            for part in self.parts:
                total, b, _, _ = self._tu(part)
                print(f'  {part.name}: {self.rel(part.path)} (built {built(part)}; TU {total - len(b)}/{total} without errors)')
            print('  references found only in some of the configurations that read the file are marked [only in ...]')
        bad = {}
        for p, e, n in self.tu_status()[1]:
            bad.setdefault((p, e), []).append(n)
        if bad:
            # 大きなリポジトリでは失敗した TU が数百になり、並べるだけでクライアントの出力の上限を超える(O3DE で 180 TU・100 KB)。
            # 10 件までにし、残りは理由ごとの件数にまとめる
            print(f'  {len(bad)} TU had parse errors; references only in those TUs may be missing:')
            items = list(bad.items())
            for (p, e), ns in items[:10]:
                print(f'    {p}  ({e})' + (f'  [{", ".join(ns)}]' if multi else ''))
            if len(items) > 10:
                why = collections.Counter(e for (p, e), ns in items[10:])
                where = ', '.join(f'{self.rel(part.path)}.tu.tsv' for part in self.parts)  # 構成ごとに 1 つ
                print(f'    ... {len(items) - 10} more (all in {where}): '
                      + '; '.join(f'{n} x {e[:80]}' for e, n in why.most_common(3))
                      + ('; ...' if len(why) > 3 else ''))
        changed, new, missing = self.freshness()
        if changed:
            print(f'  {len(changed)} source files changed after indexing. Indexed lines in them are re-located by their'
                  ' source text ([moved from line N] / [line changed or removed after indexing]); rebuild the index for exact results:')
            for p in changed[:10]:
                print(f'    {p}')
            if len(changed) > 10:
                print(f'    ... {len(changed) - 10} more')
        if new:
            print(f'  {len(new)} source files modified after indexing are not read by any indexed configuration'
                  ' (new files? rebuild the index if they are compiled):')
            for p in new[:10]:
                print(f'    {p}')
            if len(new) > 10:
                print(f'    ... {len(new) - 10} more')
        if missing:
            print(f'  {len(missing)} indexed TU files no longer exist: ' + ', '.join(missing[:10]))


def in_range(pos, b, e):
    return b <= pos < e


def classify_ranges(ranges, line, col, readers):
    """(line, col) を含む範囲の (種類, 範囲の表示)。コメントを優先する。
    無効な #if は、そのファイルを読んだ構成(readers)のすべてで無効なときだけ inactive にする。"""
    hit = [(k, b, e, n) for k, b, e, n in ranges
           if in_range((line, col), b, e) or (k == 'inactive' and b[0] <= line <= e[0])]
    hit.sort(key=lambda x: x[0] != 'comment')
    if hit and hit[0][0] == 'inactive' and not readers <= {n for k, _, _, n in hit if k == 'inactive'}:
        hit = []  # 他の構成ではコンパイルされる
    if not hit:
        return None, ''
    k, b, e, _ = hit[0]
    return k, (f'  [lines {b[0]}-{e[0]}]' if b[0] != e[0] else '')


def walk_sources(idx, excludes):
    """ルート配下のソースを (相対パス, 絶対パス, 更新日時) で返す。順はフォルダごとに、ファイルを先、サブフォルダを後にする。
    excludes はディレクトリ名(どこにあっても除く)か、ルートからの相対パスの接頭辞。"""
    pre = tuple(e.strip('/') + '/' for e in excludes if '/' in e)
    names = {e for e in excludes if '/' not in e}

    def order(rel):
        *dirs, name = rel.lower().split('/')
        return tuple((1, d) for d in dirs) + ((0, name),)

    for rel, (fp, m) in sorted(idx.sources().items(), key=lambda kv: order(kv[0])):
        if pre and rel.startswith(pre):
            continue
        if names and not names.isdisjoint(rel.split('/')[:-1]):
            continue
        yield rel, fp, m


DEFINE = re.compile(rb'[ \t]*#[ \t]*define\b')


def lexical_hint(data, b, col):
    """索引に無い一致の見当: 文字列リテラルの中か、マクロ定義の中か。b は一致した行の先頭の位置。"""
    before = data[b:b + col - 1]
    if len(re.findall(rb'(?<!\\)"', before)) % 2 == 1:
        return 'string'
    while b > 0:  # 前の行が `\` で終わっていれば、続きの行なので遡る
        p = data.rfind(b'\n', 0, b - 1) + 1
        if not data[p:b].rstrip(b'\r\n').endswith(b'\\'):
            break
        b = p
    if DEFINE.match(data, b):
        return 'macro'
    return None


def text_hits(idx, short, excludes):
    """ソース中の単語一致を (path, line, col, src, stale, hint) で返す。col は clang と同じ 1 始まりのバイト位置。"""
    sb = short.encode('utf-8')
    pat = re.compile(rb'(?<![A-Za-z0-9_])' + re.escape(sb) + rb'(?![A-Za-z0-9_])')
    hits = []

    def load(chunk):  # 読むのは並列にする(大きなコードでは読み込みの待ちが大半)
        out = []
        for rel, fp, mtime in chunk:
            try:
                with open(fp, 'rb') as f:
                    data = f.read()
            except OSError:
                continue
            if sb in data:
                out.append((rel, mtime, data))
        return out

    # ファイルごとに仕事を渡すと、20 万ファイル(UE5)では受け渡しだけで数秒かかるので、続きのファイルをまとめて渡す
    items = list(walk_sources(idx, excludes))
    size = max(1, -(-len(items) // 256))
    with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as ex:
        loaded = [x for part in ex.map(load, (items[i:i + size] for i in range(0, len(items), size))) for x in part]
    for rel, mtime, data in loaded:
        data = to_utf8(data, idx.codec())  # Shift_JIS の 2 バイト目が英字になる偽の一致も、直してから探せば出ない
        stale = mtime > idx.mtime
        ln, last = 1, 0
        for m in pat.finditer(data):
            pos = m.start()
            ln += data.count(b'\n', last, pos)  # 行番号は直前の一致から数え足す
            last = pos
            b = data.rfind(b'\n', 0, pos) + 1
            e = data.find(b'\n', pos)
            src = data[b:e if e >= 0 else len(data)].strip().decode('utf-8', 'replace')[:200]
            col = pos - b + 1
            hits.append((rel, ln, col, src, stale, lexical_hint(data, b, col)))
    return hits


def norm_src(s):
    """ソース行の比べ方: 空白をまとめ、行末のコメントを除く(索引とテキスト検索で切り詰め方が違っても揃う)。"""
    return ' '.join(s.split('//', 1)[0].split())[:150]


def receiver(src, short):
    """行の中で short を呼んでいる受け手の式(`g->`・`device.`・`Foo::` など)と名前を合わせたもの。
    受け手の無い呼び出しと、行に short が複数あるものは None(どれのことか決まらない)。"""
    found = list(re.finditer(r'((?:[A-Za-z_]\w*\s*(?:->|\.|::)\s*)+)' + re.escape(short) + r'\s*[(<]', src))
    if len(found) != 1 or len(re.findall(r'(?<!\w)' + re.escape(short) + r'(?!\w)', src)) != 1:
        return None
    return re.sub(r'\s+', '', found[0].group(1)) + short


def by_dir(items):
    """[(path, 行の表示)] をディレクトリごとの件数にまとめる。"""
    agg = {}
    for path, _ in items:
        d = path.rsplit('/', 1)[0] if '/' in path else '.'
        n, files = agg.get(d, (0, set()))
        files.add(path)
        agg[d] = (n + 1, files)
    return [f'{d}/  {n} matches in {len(fs)} files' for d, (n, fs) in sorted(agg.items())]


def fmt(r, show_sym, idx):
    sym, role, caller, path, line, col, src, note, _ = r
    s = f'{role:<6} {path}:{line}'
    if show_sym:
        s += f'  {sym}'
    if caller:
        s += f'  [in {caller}]'
    if note:
        s += f'  [{note}]'
    only = idx.partial(r)
    if only:
        s += f'  [only in {only}]'
    return s + f'  | {src}'


def closure(start, edges):
    """start から edges({記号: {記号}})を辿って届く記号(start 自身は除く)。"""
    seen, todo = set(), list(start)
    while todo:
        for y in edges.get(todo.pop(), ()):
            if y not in seen:
                seen.add(y)
                todo.append(y)
    return seen - set(start)


def section(title, items, limit):
    print(f'\n== {title}: {len(items)}')
    for s in items[:limit]:
        print('  ' + s)
    if len(items) > limit:
        print(f'  ... {len(items) - limit} more (raise --limit)')


def cmd_refs(a, idx):
    name = a.name.lstrip(':')
    short = re.split(r'::', name)[-1]
    sb = short.encode('utf-8')

    def match(sym):
        return sym == name or sym.endswith('::' + name)

    # 記号の列の末尾が short の行だけを読む(`short<TAB>` を含む行。`Loader` などの部分一致の行は読まない)
    rows = idx.cached(('rows', short), lambda: [r for r in idx.rows([sb + b'\t'], lambda raw: True)
                                                 if r[0].rsplit('::', 1)[-1] == short])
    # 仮想関数の上書き(上書きするメソッド -> 上書きされる基底のメソッド)。同じ名前なので rows に入っている
    up, down, ov_at = {}, {}, {}
    for r in rows:
        if r[1] == OVERRIDE:
            up.setdefault(r[0], set()).add(r[2])
            down.setdefault(r[2], set()).add(r[0])
            ov_at.setdefault(r[0], (r[3], r[4]))
    rows = [r for r in rows if r[1] != OVERRIDE]
    target = [r for r in rows if match(r[0])]
    deps = [r for r in rows if r[0] == '?::' + short]
    syms = sorted({r[0] for r in target})
    show_sym = len(syms) > 1

    idx.header()
    print(f'query: {a.name}')
    if a.compact:
        print('  (compact output: comments, inactive #if, strings, and matches that resolve to other symbols are'
              ' summarized as counts; calls and likely calls are listed in full)')
    if not syms:
        print('  no symbol in the index matches this name.')
    elif show_sym:
        print('  matched symbols: ' + ', '.join(f'{s} ({sum(1 for r in target if r[0] == s)})' for s in syms))
    else:
        print(f'  symbol: {syms[0]}')

    key = lambda r: (r[3], r[4], r[5], r[1].endswith('-using'))
    # 同じ位置に直接の解決と using 越しの解決があれば、直接のほうだけ残す
    seen = set()
    uniq = []
    for r in sorted(target, key=key):
        if r[1].endswith('-using') and (r[0], r[3], r[4], r[5]) in seen:
            continue
        seen.add((r[0], r[3], r[4], r[5]))
        uniq.append(r)
    target = uniq
    calls = [r for r in target if r[1].startswith('call')]
    uses = sorted((r for r in target if not r[1].startswith(('call',) + DEFS)), key=key)
    defs = sorted((r for r in target if r[1].startswith(DEFS)), key=key)
    section('calls (resolved by clang)', [fmt(r, show_sym, idx) for r in calls], a.limit)
    if calls:
        print('  (exact: clang resolved each call from the real type of its receiver, whatever the variable is named)')
    if any(r[1].endswith('-using') for r in target):
        print('  (call-using: a call through a using-declaration inside a template, resolved from its instantiations)')
    section('other references (address taken, template arguments, writes, ...)',
            [fmt(r, show_sym, idx) for r in uses], a.limit)
    section('definitions and declarations', [fmt(r, show_sym, idx) for r in defs], a.limit)

    # 基底のポインタや参照越しの呼び出しは、clang が基底のメソッドに解決する。実行時に上書きへ届くかは型次第
    bases, derived = closure(syms, up), closure(syms, down)
    if bases or derived:
        print('\n== virtual dispatch')
    if bases:
        via = sorted((r for r in rows if r[0] in bases and r[1].startswith('call')), key=key)
        print(f'  the query overrides {", ".join(sorted(bases))}. Calls to those may run the query at runtime,'
              ' depending on the dynamic type of the receiver; they are not counted above:'
              f' {len(via)}')
        for s in [fmt(r, True, idx) for r in via][:a.limit]:
            print('    ' + s)
        if len(via) > a.limit:
            print(f'    ... {len(via) - a.limit} more (raise --limit)')
    if derived:
        n = sum(1 for r in rows if r[0] in derived and r[1].startswith('call'))
        print(f'  overridden by (calls through a pointer or reference to the query\'s class may run these instead;'
              f' calls resolved to these, {n}, call the override, not the query):')
        for d in sorted(derived):
            p, ln = ov_at.get(d, ('?', 0))
            print(f'    {d}  {p}:{ln}')

    # dep 行: テンプレート本体では未解決。実体化で解決された位置は、その解決先を添える
    at = {}
    for r in rows:
        at.setdefault((r[3], r[4]), []).append(r)
    target_pos = {(r[3], r[4]) for r in target}
    def_files = {r[3] for r in defs}
    dep_lines, dep_dup, dep_other = [], 0, {}
    for r in sorted(deps, key=key):
        if (r[3], r[4]) in target_pos:
            dep_dup += 1  # 実体化で対象に解決済み。上の calls に載っている
            continue
        resolved = sorted({x[0] for x in at[(r[3], r[4])] if x[0] != r[0] and not x[1].startswith(DEFS + (OVERRIDE,))})
        if resolved and a.compact:  # 短い出力では、別の記号に解決された行は件数だけにする
            for s in resolved:
                dep_other[s] = dep_other.get(s, 0) + 1
            continue
        if resolved:
            mark = '  (instantiations resolve it to ' + ', '.join(resolved) + ' -> probably not the query)'
        else:
            mark = '  (never instantiated; unknown target' + (', same file as the definition)' if r[3] in def_files else ')')
        dep_lines.append(fmt(r, False, idx) + mark)
    n_other = sum(dep_other.values())
    print(f'\n== unresolved template calls named "{short}" (dep): {len(dep_lines) + n_other}'
          + (f' (+{dep_dup} already listed in calls)' if dep_dup else ''))
    if dep_lines:
        print('  clang could not resolve these inside templates. Decide from the source line and the')
        print('  enclosing function whether each one calls the queried symbol; they are not counted above.')
    if dep_other:
        top = sorted(dep_other.items(), key=lambda kv: -kv[1])
        print(f'  {n_other} of them are resolved by instantiations to other symbols -> probably not the query: '
              + ', '.join(f'{s} ({n})' for s, n in top[:8]) + (' ...' if len(top) > 8 else ''))
    for s in dep_lines[:a.limit]:
        print('  ' + s)
    if len(dep_lines) > a.limit:
        print(f'  ... {len(dep_lines) - a.limit} more (raise --limit)')

    # テキスト一致のうち、索引に同名の行が無いものの理由
    hits = idx.cached(('text', short, tuple(sorted(a.exclude))), lambda: text_hits(idx, short, set(a.exclude)))
    known = {r[3] for r in rows}
    loose = [h for h in hits if (h[0], h[1]) not in at]
    want = frozenset(h[0] for h in loose)
    ranges = idx.cached(('skips', want), lambda: idx.skips(want))
    parsed = known | set(ranges) | idx.tu_status()[2]
    tagged = {r[0] for r in target} | {'?::' + short} | bases | derived
    others = {}
    for r in rows:
        if r[0] not in tagged:
            others[r[0]] = others.get(r[0], 0) + 1
    # 索引に無い一致の手がかり: 同じ文面の行か、同じ受け手の式の呼び出しが索引にあれば、clang がそれを何に解決したか
    # (別プラットフォーム向けの写しのようなファイルでは、同じ形の呼び出しが同じ先に解決されることが多い)
    same, recv = {}, {}
    for r in rows:
        if r[1].startswith('call') and r[0] != '?::' + short:
            same.setdefault(norm_src(r[6]), set()).add(r[0])
            k = receiver(r[6], short)
            if k:
                recv.setdefault(k, set()).add(r[0])
    query_syms = {r[0] for r in target} | bases

    def resolved_like(src):
        """(注記, 見立て)。見立ては 'call'(問い合わせの呼び出しらしい)・'not'(別の記号らしい)・''(手がかり無し)。
        解決先が 1 つに揃うときだけ出す(`Reset();` のような短い行は、クラスごとに別の先へ解決される)"""
        to = same.get(norm_src(src))
        if to and len(to) == 1:
            s = next(iter(to))
            if s in query_syms:
                return '  (the same line text elsewhere is a call to the query -> probably a call)', 'call'
            return f'  (the same line text elsewhere resolves to {s} -> probably not the query)', 'not'
        k = receiver(src, short)
        to = recv.get(k) if k else None
        if to and len(to) == 1:  # 受け手の名前だけで決めるので、弱い手がかりとして出す
            s = next(iter(to))
            if s in query_syms:
                return f'  (`{k}` elsewhere resolves to {s} -> may be a call to the query; check the receiver type)', 'call'
            return f'  (`{k}` elsewhere resolves to {s} -> probably not the query; check the receiver type)', 'not'
        return '', ''
    cats = {k: [] for k in ('comment', 'inactive', 'unparsed', 'string', 'macro', 'other')}
    for path, line, col, src, stale, hint in loose:
        readers = {n for n, s in idx.seen().items() if path in s}
        k, span = classify_ranges(ranges.get(path, []), line, col, readers)
        if k is None:
            k = (hint or 'other') if path in parsed else 'unparsed'
        like, verdict = resolved_like(src) if k in ('unparsed', 'other') else ('', '')
        cats[k].append((path, f'{path}:{line}{span}{"  [file changed after indexing]" if stale else ""}{like}  | {src}', verdict))

    print(f'\n== text matches of "{short}" not in the index: '
          f'{sum(len(v) for v in cats.values())} of {len(hits)} word matches')
    groups = [
        ('comment', 'inside comments -> not code; do not report these', False),
        ('inactive', 'inside inactive #if/#ifdef branches -> not compiled in the indexed configuration', False),
        ('macro', 'inside macro definitions -> expansions are indexed at the use site', False),
        ('string', 'inside string literals -> not code', True),
        ('unparsed', 'in files no indexed TU includes -> not analyzed; read them if they matter', True),
        ('other', 'other -> not explained by the index; read them if they matter', False),
    ]
    for k, title, fold in groups:
        items = cats[k]
        if not items:
            continue
        print(f'  {title}: {len(items)}')
        if a.compact and k in ('comment', 'inactive', 'macro', 'string'):
            # 短い出力: 報告しない分類は、件数の多いフォルダだけ
            dirs = sorted(by_dir([(p, s) for p, s, _ in items]), key=lambda x: -int(x.split()[1]))
            for s in dirs[:5]:
                print('    ' + s)
            if len(dirs) > 5:
                print(f'    ... {len(dirs) - 5} more folders')
            continue
        if a.compact:
            # 短い出力: 問い合わせの呼び出しらしい行だけを出し、別の記号らしい行は件数に、残りはフォルダごとにまとめる
            nots = sum(1 for _, _, v in items if v == 'not')
            lines = [s for _, s, v in items if v == 'call']
            if nots:
                lines.append(f'{nots} look like calls to other symbols (the same line or receiver resolves elsewhere)')
            lines += by_dir([(p, s) for p, s, v in items if not v]) if len(items) > 10 else [s for _, s, v in items if not v]
        elif fold and len(items) > 10:  # 手がかりの付いた行はまとめずに出す
            lines = [s for _, s, v in items if v] + by_dir([(p, s) for p, s, v in items if not v])
        else:
            lines = [s for _, s, _ in items]
        for s in lines[:a.limit]:
            print('    ' + s)
        if len(lines) > a.limit:
            print(f'    ... {len(lines) - a.limit} more (raise --limit)')
    if others:
        top = sorted(others.items(), key=lambda kv: -kv[1])
        print(f'\n== other symbols named "{short}" (not the query): ' +
              ', '.join(f'{s} ({n})' for s, n in top[:15]) + (' ...' if len(top) > 15 else ''))


def cmd_check(a, idx):
    locs = []
    for s in a.locs:
        m = re.match(r'^(.*?):(\d+)(?::(\d+))?$', s.replace('\\', '/'))
        if not m:
            print(f'{s}: expected path:line')
            continue
        p = m.group(1)[2:] if m.group(1).startswith('./') else m.group(1)
        locs.append((p, int(m.group(2)), int(m.group(3)) if m.group(3) else None))
    paths = {p for p, _, _ in locs}
    pb = [p.encode('utf-8') for p in paths]
    needles = [b'\t' + p + b':' for p in pb]
    rows = idx.rows(needles, lambda raw: any(n in raw for n in needles))
    ranges = idx.skips(paths)
    idx.header()
    for path, line, col in locs:
        here = [r for r in rows if r[3] == path and r[4] == line and (col is None or r[5] == col)]
        fp = os.path.join(idx.root, path)
        tag = ''
        if not os.path.exists(fp):
            tag = '  [file not found]'
        elif os.path.getmtime(fp) > idx.mtime:
            tag = '  [file changed after indexing]'
        print(f'\n{path}:{line}{":" + str(col) if col else ""}{tag}')
        multi = len(idx.parts) > 1
        for r in sorted(here, key=lambda r: r[5]):
            only = idx.partial(r)
            rel = f'  [overrides {r[2]}]' if r[1] == OVERRIDE else f'  [in {r[2]}]' if r[2] else ''
            print(f'  indexed: {r[1]} {r[0]} at col {r[5]}' + rel
                  + (f'  [{r[7]}]' if r[7] else '') + (f'  [only in {only}]' if only else ''))
        shown = set()
        for k, b, e, n in ranges.get(path, []):
            if (col is not None and in_range((line, col), b, e)) or (col is None and b[0] <= line <= e[0]):
                what = 'comment' if k == 'comment' else 'inactive #if branch'
                s = f'  inside {what} {b[0]}:{b[1]}-{e[0]}:{e[1]}'
                s += f'  [{n}]' if multi and k != 'comment' else ''
                if s not in shown:
                    shown.add(s)
                    print(s + ('  (shifted with the nearby indexed lines)' if tag.endswith('indexing]') else ''))
        if not here and path not in ranges and not any(r[3] == path for r in rows):
            print('  file not seen by any indexed TU')
        elif not here:
            print('  no indexed reference on this line')


def find_indexes(root):
    """<root>/.index の中の索引本体(.tu.tsv・.skip.tsv・.deps.tsv 以外の .tsv)すべて。構成ごとに 1 つ置く。
    index.tsv を先頭にし、残りは名前順。"""
    d = os.path.join(root, '.index')
    if not os.path.isdir(d):
        return []
    main = sorted((f for f in os.listdir(d) if f.endswith('.tsv') and not f.endswith(SIDE)),
                  key=lambda f: (f != 'index.tsv', f))
    return [os.path.join(d, f) for f in main]


def find_root(start):
    """start から親へさかのぼって、索引のある最初のフォルダ(.git と同じ探し方)。無ければ None。
    ソースのルートより下のフォルダでエージェントを起動しても、ルートの索引を引けるようにする。"""
    d = os.path.abspath(start)
    while True:
        if find_indexes(d):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def main(argv=None, idx=None):
    """戻り値は終了コード。結果は標準出力に書く(MCP サーバーはこれを取り込んで返す)。
    idx を渡すと、索引を探さずにそれを引く(MCP サーバーが鮮度を確かめるのに作ったものを使い回す)。"""
    ap = argparse.ArgumentParser(prog='ltq', description='C++ reference index query (ltidx TSV)')
    ap.add_argument('--index', action='append',
                    help='index TSV; repeat it for several configurations (default: every index in <root>/.index)')
    ap.add_argument('--root', help='source root the index paths are relative to '
                                   '(default: the nearest folder with .index, from . upward)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('refs', help='references, definitions, unresolved template calls, and why text matches are missing')
    r.add_argument('name', help='qualified name or its suffix, e.g. nox::io::File::Read or File::Read')
    r.add_argument('--limit', type=int, default=200, help='max lines per section (default 200)')
    r.add_argument('--exclude', action='append', default=[], help='directory name to skip in the text search')
    r.add_argument('--compact', action='store_true',
                   help='summarize low-value sections (comments, inactive #if, strings, template calls resolved to '
                        'other symbols, matches resolved elsewhere) into counts')
    c = sub.add_parser('check', help='how the index treats each path:line[:col]')
    c.add_argument('locs', nargs='+')
    a = ap.parse_args(argv)
    if a.root is None:
        a.root = (None if a.index else find_root('.')) or '.'
        if os.path.abspath(a.root) != os.path.abspath('.'):
            print(f'root: {os.path.abspath(a.root)} (the index is in this parent folder; paths below are relative to it)')
    if idx is None:
        indexes = a.index or find_indexes(a.root)
        missing = [p for p in indexes if not os.path.exists(p)]
        if not indexes or missing:
            where = missing[0] if missing else os.path.join(a.root, '.index', 'index.tsv')
            print(f'index not found: {os.path.abspath(where)}  (build it with ltidx, or pass --index)')
            return 2
        idx = Index(indexes, a.root)
    (cmd_refs if a.cmd == 'refs' else cmd_check)(a, idx)
    return 0


if __name__ == '__main__':
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding='utf-8', errors='replace')
    sys.exit(main())
