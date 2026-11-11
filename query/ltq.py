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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I ではスクリプトのフォルダが検索パスに入らない
import ppcond  # noqa: E402

SRC_EXT = {'.h', '.hh', '.hpp', '.hxx', '.c', '.cc', '.cpp', '.cxx', '.inl', '.ipp', '.inc', '.ixx', '.cppm', '.tpp', '.txx',
           '.m', '.mm', '.def'}  # .def: X マクロの一覧(LLVM の *.def など)。DLL の EXPORTS の一覧も、どの TU も読まないファイルとして出る
TU_EXT = {'.c', '.cc', '.cpp', '.cxx', '.cppm', '.ixx', '.m', '.mm'}  # 単独でコンパイルされうるもの
DEFS = ('def', 'decl')
# マクロそのものの行(ltidx の役割): #define・展開(名前がファイルに書かれた場所)・#ifdef と defined()・#undef
MACRO_ROLES = ('macro', 'expand', 'ifdef', 'undef')
OVERRIDE = 'override'  # 3 列目は呼び出し元ではなく、上書きされる基底のメソッド
CP_CODECS = {65001: None, 51932: 'euc_jp', 20932: 'euc_jp', 28591: 'latin-1'}  # ほかは cp<番号>
SKIP_SCAN_FILES = 64  # 範囲を引くファイルがこれより多ければ、.skip.tsv を 1 つずつ find せず、1 回通して読む
ALT = '.alt.tsv'  # 無効な #if の中を仮に有効にして解析した行(scripts/altpass.py が書く。索引本体と同じ形)
SIDE = ('.tu.tsv', '.skip.tsv', '.deps.tsv', ALT)  # 索引本体の隣に ltidx と altpass が書くファイル
# 索引のファイルの名前の、索引のパスより後ろの部分: 本体(空)・隣のファイル・写し(.seen.cache など)と、それを書いている途中の
# ファイル(.tmp・.<プロセス番号>.tmp)。索引の隣に書くファイルを増やしたら、ここにも足す(足さないと、テキスト検索がそれを読む)
OWN_TAIL = re.compile(r'(\.(tu|skip|deps|alt)\.tsv|\.own\.bin|\.(alt|recipe)\.json'
                      r'|\.(seen|words|words-other|names)\.cache)?(\.\d+)?(\.tmp)?\Z')
# C/C++ のソースでないファイルも、名前の一致だけは探す(rg なら出る行を落とさない: C# の生成器・ビルドの定義・文書など)。
# テキストかどうかは、rg と同じく中身で見分ける(other_chunks)。拡張子や大きさでは除かない: .obj には Wavefront の 3D モデルの
# ようなテキストがあり、Git LFS の実体を取っていない .png は中身がテキスト。拡張子で除くと、rg なら出る行が出ないことがある
OTHER_HEAD = 8192  # バイナリかを見分けるために読む、先頭の大きさ
LONG_LINE = 1 << 20  # 行の区切りが無いままこれ(と 1 回に読む大きさ)を超えたら、行の途中で分ける
WORD_TAIL = re.compile(rb'[A-Za-z0-9_\x80-\xff]*\Z')  # 末尾の、単語か複数バイトの文字の途中かもしれない部分
OTHER_LIST = 10  # 短い出力(--compact 1 つ)で、ソースでないファイルの一致を 1 行ずつ並べる数
UNREAD_LIST = 20  # 読めなかったファイルを並べる数(--all では全部)
# 件数にまとめた行があるときに、出力の最後の行(coverage)の末尾に付ける案内(MCP サーバーは、一覧のファイルの場所に置き換える)
ALL_HINT = '; the rest are only counted (refs --all lists every line)'


def is_other(ext, size):
    """テキストとして名前を探す、C/C++ のソースでないファイルか(拡張子は小文字、. 付き)。空のファイルだけ除く。"""
    return ext not in SRC_EXT and size > 0


def tree_place(d, root):
    """フォルダ d が、ルートから見てどこにあるか: 'out'(ルートの外)、'hidden'(ルートの下の、. で始まるフォルダの中。
    テキスト検索は読まない)、'open'(ルートの下の、テキスト検索が読む所)。"""
    try:
        parts = os.path.relpath(d, root).replace('\\', '/').split('/')
    except ValueError:  # 別のドライブ
        return 'out'
    if parts[0] == '..':
        return 'out'
    return 'hidden' if any(p.startswith('.') and p != '.' for p in parts) else 'open'


def other_chunks(fp, chunk=None):
    """C/C++ のソースでないファイルの、テキストとして探す部分を (先頭の行番号, bytes) で順に返す。
    - 先頭 OTHER_HEAD バイトに NUL があればバイナリ。最初の NUL より前だけを返す(rg は NUL に当たるまでの一致を出す。
      ふつうのバイナリでは数バイトで、何も一致しない)
    - UTF-16(BOM 付き)は UTF-8 に直して返す(直せないバイトは U+FFFD にする)
    - 大きなファイルは chunk バイトごとに、行の区切りで分けて返す(全部を一度にメモリに載せない。UTF-16 も同じ)。
      行の区切りが無いまま大きくなったとき(1 行だけの大きなファイル)は、単語の途中でない所で分ける。行番号は変わらない"""
    chunk = chunk or READ_CHUNK
    with open(fp, 'rb') as f:
        head = f.read(OTHER_HEAD)
        dec = None
        if head[:2] in (b'\xff\xfe', b'\xfe\xff'):
            dec = codecs.getincrementaldecoder('utf-16')('replace')
        else:
            z = head.find(b'\0')
            if z >= 0:
                if z:
                    yield 1, head[:z]
                return
        ln, buf = 1, dec.decode(head).encode('utf-8') if dec else head
        left = os.fstat(f.fileno()).st_size - len(head)
        while True:
            # 残りより大きくは頼まない(read は頼んだ大きさのバッファを取る。小さなファイルごとに chunk の分を取ると遅い)。
            # 大きさを見た後に伸びたファイル(書いている途中のログなど)は、伸びた分を chunk ずつ読む
            want = min(chunk, left + 1) if left >= 0 else chunk
            more = f.read(want)
            left -= len(more)
            end = len(more) < want
            buf += dec.decode(more, end).encode('utf-8') if dec else more
            if end:
                yield ln, buf
                return
            cut = buf.rfind(b'\n') + 1
            if not cut and len(buf) >= max(chunk, LONG_LINE):
                keep = len(WORD_TAIL.search(buf, max(0, len(buf) - 4096)).group())
                cut = len(buf) - (keep if keep < 4096 else 0)  # 末尾がずっと単語の文字なら、そこで分けるしかない
            if cut:  # 行の区切りが無ければ、次の分と合わせてから分ける
                yield ln, buf[:cut]
                ln += buf.count(b'\n', 0, cut)
                buf = buf[cut:]


WORD_BYTE = frozenset(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_')


def word_starts(data, sb):
    """data の中で、sb が単語として(前後が英数字と _ でなく)現れる位置を、前から順に返す。
    正規表現 `(?<![A-Za-z0-9_])sb(?![A-Za-z0-9_])` の finditer と同じ位置になる。正規表現は前を見る条件が頭にあると
    1 バイトずつ試すので、大きなファイル(UE5 の 69 MB の .po など)では遅い(69 MB で 0.6 秒。スレッドでも並ばない)。
    find で候補へ飛べば、同じファイルが 0.02 秒で済む。"""
    if not sb:  # 空の名前はどの位置にも一致し、find が進まない
        return
    n, size = len(sb), len(data)
    pos = data.find(sb)
    while pos >= 0:
        end = pos + n
        if (not pos or data[pos - 1] not in WORD_BYTE) and (end == size or data[end] not in WORD_BYTE):
            yield pos
            pos = data.find(sb, end)
        else:
            pos = data.find(sb, pos + 1)


SYM_LIST = 20  # matched symbols: に並べる記号の数(残りは件数)
COMPACT_LIST = 30  # 短い出力(--compact 1 つ)で、分類ごとに 1 行ずつ並べる数。残りはフォルダごとの件数にする
COND_MAX = 100  # 無効な行に添える #if の条件の長さ(1 つあたり)
SEEN_CACHE = '.seen.cache'  # .deps.tsv に載るパスの一覧の写し(ltq が書く)
READ_CHUNK = 64 << 20
PROC_BYTES = 1 << 30  # C/C++ 以外のファイルで、読む合計がこれ以上なら、プロセスを分けて探す(大きさが分かるときだけ)


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
    found = grep_found(path, needles)
    return [found[b] for b in sorted(found)]


def grep_found(path, needles, found=None):
    """grep_lines() の中身。{行の先頭の位置: 行} を返す(found を渡すと、それに足す)。"""
    found = {} if found is None else found
    needles = [n for n in needles if n]
    try:
        size = os.path.getsize(path)
    except OSError:
        return found
    if not needles or size == 0:
        return found
    with open(path, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        if len(needles) > 2 and not any(n[:1] == b'\n' for n in needles):
            # 針ごとに全体を 1 周すると、索引の後に変わったファイルが N 個あれば N 周になる(O3DE の 900 MB で 1 周 0.5 秒)
            for mt in re.finditer(b'|'.join(re.escape(n) for n in sorted(set(needles), key=len, reverse=True)), m):
                i = mt.start()
                b = m.rfind(b'\n', 0, i) + 1
                if b not in found:
                    e = m.find(b'\n', i)
                    found[b] = m[b:size if e < 0 else e].rstrip(b'\r')
            return found
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
    return found


def name_lines(path, nd, short, found):
    """名前の目次(namedir.NameDir)で、記号の最後の部分が short(bytes)の行を found({位置: 行})に足す。
    ハッシュの衝突で混じる別の名前の行は、記号の列を見て捨てる。"""
    with open(path, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        size = len(m)
        for b in nd.offsets(short):
            if b in found:
                continue
            e = m.find(b'\n', b)
            line = m[b:size if e < 0 else e].rstrip(b'\r')
            t = line.find(b'\t')
            if t >= 0 and sym_short(line[:t]) == short:
                found[b] = line
    return found


def sym_short(sym):
    """記号の列(bytes)の最後の部分(`sym.rsplit(b'::', 1)[-1]` と同じ)。namedir.short_of と同じもの
    (ltq は python -I で単独に起動されるので、隣のモジュールを読み込まない)。"""
    i = sym.rfind(b'::')
    return sym if i < 0 else sym[i + 2:]


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
        # 差分(.alt.tsv)は本体の後に altpass が書く。本体より古ければ、本体を作り直した後(自動更新・-Update・altpass の
        # 失敗)で行番号が合わないので使わない(無効な行は文字列の一致として出る)
        try:
            self.alt_ok = os.path.getmtime(path + ALT) >= self.mtime
        except OSError:
            self.alt_ok = False


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
        self._others = None
        self._own = None
        self._lines = {}
        self._maps = {}  # (構成名, path) -> 行の付け直しの対応。rows() が作り、skips() も使う
        self._tus = {}
        self._memo = {}
        self._groups = {}  # path -> ソースの #if の構造(ppcond)。無効な行に、囲む条件を添えるのに使う
        # 長く動くプロセス(MCP サーバー)が渡す部品。無ければ、問い合わせのたびに全部を読む
        self.tree = None  # srctree.SourceTree: ソースの一覧(C/C++ 以外のファイルも)を変更の知らせで保つ
        self.words = None  # wordfilter.WordFilter: テキスト検索で読むファイルを絞る
        self.other_words = None  # wordfilter.WordFilter: C/C++ 以外のファイルの検索で読むファイルを絞る
        self.names = {}  # {索引のパス: namedir.NameDir}: 名前から索引の行を引く

    def renew(self):
        """ソースから読んだもの(走査・鮮度・ファイルの行・行の付け直し・問い合わせの途中の結果)を捨てる。
        索引のファイルから読んだもの(seen・TU の記録・codec)は残す。MCP サーバーが、索引が変わっていない間に
        Index を使い回すときに呼ぶ。"""
        self._fresh = None
        self._sources = None
        self._others = None
        self._lines = {}
        self._maps = {}
        self._memo = {}
        self._groups = {}

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

    def rows(self, needles, keep, name=None):
        """needles(bytes)のどれかを含み、keep(bytes の行)が真の行だけを
        (sym, role, caller, path, line, col, src, note, configs, alt) にして返す。
        note は行の付け直しの注記、configs はその行がある構成名の frozenset。
        alt は、その構成ではコンパイルされない #if の中の行(<索引>.alt.tsv。altpass が、条件を仮に有効にして解析したもの)なら True。
        索引の後に変わったファイルの行は、索引にあるソース行の文字列で今のファイルから探し直す。
        name(bytes)を渡すと、名前の目次がある索引では、needles の代わりに「記号の最後の部分が name の行」を目次で引く
        (needles は name + TAB のはずで、それを含む行のうち記号の列が name で終わらない行は、呼ぶ側が捨てる)。
        差分(.alt.tsv)は小さいので、目次を使わずに頭から探す。"""
        merged = {}
        for part in self.parts:
            stale = self._stale(part)
            sb = {p.encode('utf-8') for p in stale}
            anchors, picked = {}, []
            stale_needles = [b'\t' + p + b':' for p in sb]
            # 変わったファイルの行は、問い合わせに合わなくても行の付け直しの手がかりとして読む
            for alt in ((False, True) if part.alt_ok else (False,)):
                nd = self.names.get(part.path) if name is not None and not alt else None
                if nd is not None and nd.ready():
                    found = grep_found(part.path, stale_needles)
                    lines = [v for _, v in sorted(name_lines(part.path, nd, name, found).items())]
                else:
                    lines = grep_lines(part.path + (ALT if alt else ''), list(needles) + stale_needles)
                for raw in lines:
                    hit = keep(raw) and (name is None or sym_short(raw.split(b'\t', 1)[0]) == name)
                    if not hit:
                        q = raw.split(b'\t', 4)
                        if len(q) < 5 or q[3].rsplit(b':', 2)[0] not in sb:
                            continue
                    p = raw.split(b'\t', 4)
                    if len(p) < 5:
                        continue
                    loc = p[3].rsplit(b':', 2)
                    if alt and not (len(loc) == 3 and loc[1].isdigit() and loc[2].isdigit()):
                        continue  # 書きかけの行
                    path, src = loc[0].decode('utf-8', 'replace'), p[4].decode('utf-8', 'replace')
                    if path in stale:
                        anchors.setdefault(path, []).append((int(loc[1]), src))
                    if hit:
                        picked.append((p, path, int(loc[1]), int(loc[2]), src, alt))
            for path, a in anchors.items():
                self._set_map(part, path, self._line_map(path, a))
            for p, path, line, col, src, alt in picked:
                note = ''
                if path in stale:
                    line, note = self._relocate(part, path, line)
                k = (p[0].decode('utf-8', 'replace'), p[1].decode(), p[2].decode('utf-8', 'replace'), path, line, col)
                if k not in merged or (merged[k][3] and not alt):  # 同じ行がコンパイルされる構成にあれば、そちらを採る
                    merged[k] = [src, note, {part.name}, alt]
                elif alt and not merged[k][3]:
                    continue
                else:
                    merged[k][2].add(part.name)
                    if merged[k][1] and not note:  # 付け直しの要らない(新しい)構成のソース行を採る
                        merged[k][0:2] = [src, note]
        # 古い構成で行を推定した参照は、別の構成に同じ参照(行以外が同じ)の確かな行があれば捨てる
        exact = {}
        for k, v in merged.items():
            if not v[1].startswith('line changed'):
                exact.setdefault(k[:4] + (k[5],), set()).update(v[2])
        # 無効な #if の中として解析した行は、その行にどれかの構成でコンパイルされた行があれば捨てる(そちらが確か)
        live = {(k[3], k[4]) for k, v in merged.items() if not v[3]}
        return [k + (v[0], v[1], frozenset(v[2]), v[3]) for k, v in merged.items()
                if not (v[1].startswith('line changed') and exact.get(k[:4] + (k[5],), set()) - v[2])
                and not (v[3] and (k[3], k[4]) in live)]

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
        if len(self.parts) < 2 or r[9]:  # 無効な #if の中の行は、どの構成でもコンパイルされていない
            return ''
        saw = {n for n, s in self.seen().items() if r[3] in s} | set(r[8])
        return '' if saw <= r[8] else ', '.join(sorted(r[8]))

    def inactive_cond(self, path, line, ranges):
        """無効な #if の中の行に添える、囲む条件(`#if A > #if B`。外側から)。ranges は skips() のそのファイルの分。
        条件は索引に持たず、今のソースから読む。有効な分岐(インクルードガードなど)は外側から除き、
        無効な分岐から内側だけを並べる。囲む #if が無ければ ''。"""
        if path not in self._groups:
            try:
                with open(os.path.join(self.root, path), 'rb') as f:
                    data = to_utf8(f.read(), self.codec())
            except OSError:
                data = b''
            comment = set()  # コメントの中の行(コメントの始まりの行は、前にコードがありうるので除かない)
            for k, b, e, _ in ranges:
                if k == 'comment':
                    comment.update(range(b[0] + 1, e[0] + 1))
            self._groups[path] = ppcond.parse(data, comment)
        brs = ppcond.enclosing(self._groups[path], line)
        if not brs:
            return ''
        off = [(b[0], e[0]) for k, b, e, _ in ranges if k == 'inactive']
        first = next((i for i, br in enumerate(brs) if any(b <= br.line_end + 1 <= e for b, e in off)), len(brs) - 1)

        def cut(s):
            return s if len(s) <= COND_MAX else s[:COND_MAX - 3] + '...'
        return ' > '.join('#if ' + cut(ppcond.cond_text(br)) for br in brs[first:])

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
        if self._sources is None and self.tree is not None:
            self._sources = self.tree.current()  # 変更の知らせを当てた一覧。問い合わせの間は変えない
            self._others = self._not_own(self.tree.others())
        if self._sources is None:
            out, other, todo, n = {}, {}, [self.root], len(self.root) + 1
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
                            else:
                                ext = os.path.splitext(e.name)[1].lower()
                                if ext in SRC_EXT:
                                    out[e.path[n:].replace('\\', '/')] = (e.path, e.stat().st_mtime)
                                else:
                                    st = e.stat()
                                    if is_other(ext, st.st_size):
                                        other[e.path[n:].replace('\\', '/')] = (e.path, st.st_mtime)
                        except OSError:
                            continue
            self._sources, self._others = out, self._not_own(other)
        return self._sources

    def others(self):
        """{ルートからの相対パス: (絶対パス, 更新日時)}。ルート配下の、C/C++ のソースでないファイル(空のものと、
        索引のファイル自体を除く)。"""
        self.sources()
        return self._others

    def own(self):
        """索引のファイル(本体・隣のファイル・写し。どれも「索引のパス + 接尾辞」)のうち、テキスト検索が読む所(ルートの下の、
        . で始まらないフォルダ)にあるものの、ルートからの相対パスの接頭辞。ふつうは空(索引は .index かルートの外にある)。
        索引の行はソースの行の写しなので、探すと同じ一致が 2 度出て、名前を含む行の数が増える。"""
        if self._own is None:
            self._own = tuple(os.path.normcase(os.path.relpath(os.path.abspath(p.path), self.root)) for p in self.parts
                              if tree_place(os.path.dirname(os.path.abspath(p.path)), self.root) == 'open')
        return self._own

    def _not_own(self, others):
        own = self.own()
        if not own:
            return others

        def mine(rel):  # 索引のパスで始まるだけの別のファイル(index.tsv.md など)は外さない
            rel = os.path.normcase(rel)
            return any(rel.startswith(o) and OWN_TAIL.match(rel, len(o)) for o in own)
        return {k: v for k, v in others.items() if not mine(k)}

    def header(self, focus=None):
        """索引の状態。focus(問い合わせの名前を含むファイルの集合)を渡すと、失敗した TU・変わったファイル・新しいファイルは
        focus に入るものだけを並べ、残りは件数にする。refs のたびに出るので、関係の無いファイルまで並べると結果の多くを占める
        (コードベース A で 4.6 KB のうち 1.9 KB)。結果の行はどれも focus のファイルにあるので、残りは結果に関わらない。"""
        def split(items, path_of):
            if focus is None:
                return items, []
            return [x for x in items if path_of(x) in focus], [x for x in items if path_of(x) not in focus]

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
        no_name = ' that do not contain the name'
        if bad:
            # 大きなリポジトリでは失敗した TU が数百になり、並べるだけでクライアントの出力の上限を超える(O3DE で 180 TU・100 KB)。
            # 10 件までにし、残りは理由ごとの件数にまとめる
            items, rest = split(list(bad.items()), lambda x: x[0][0])
            where = ', '.join(f'{self.rel(part.path)}.tu.tsv' for part in self.parts)  # 構成ごとに 1 つ

            def reasons(xs):
                why = collections.Counter(e for (p, e), ns in xs)
                return '; '.join(f'{n} x {e[:80]}' for e, n in why.most_common(3)) + ('; ...' if len(why) > 3 else '')
            if focus is not None and not items:
                print(f'  {len(bad)} TU had parse errors, none in a file that contains the name (all in {where}): '
                      + reasons(rest))
            else:
                print(f'  {len(bad)} TU had parse errors; references only in those TUs may be missing'
                      + (':' if focus is None else f'. In files that contain the name ({len(items)}):'))
                for (p, e), ns in items[:10]:
                    print(f'    {p}  ({e})' + (f'  [{", ".join(ns)}]' if multi else ''))
                more = items[10:] + rest
                if more:
                    print(f'    ... {len(more)} more{no_name if not items[10:] and rest else ""} (all in {where}): '
                          + reasons(more))
        changed, new, missing = self.freshness()
        if changed:
            shown, rest = split(changed, lambda p: p)
            if focus is not None and not shown:
                print(f'  {len(changed)} source files changed after indexing, none of them contains the name'
                      ' (this result is not affected)')
            else:
                print(f'  {len(changed)} source files changed after indexing. Indexed lines in them are re-located by their'
                      ' source text ([moved from line N] / [line changed or removed after indexing]); rebuild the index'
                      ' for exact results' + (':' if focus is None else f'. Those that contain the name ({len(shown)}):'))
                for p in shown[:10]:
                    print(f'    {p}')
                if len(shown) > 10 or rest:
                    print(f'    ... {len(shown[10:]) + len(rest)} more' + (no_name if rest and not shown[10:] else ''))
        if new:
            shown, rest = split(new, lambda p: p)
            if focus is not None and not shown:
                print(f'  {len(new)} source files modified after indexing are not read by any indexed configuration;'
                      ' none of them contains the name')
            else:
                print(f'  {len(new)} source files modified after indexing are not read by any indexed configuration'
                      ' (new files? rebuild the index if they are compiled)'
                      + (':' if focus is None else f'. Those that contain the name ({len(shown)}):'))
                for p in shown[:10]:
                    print(f'    {p}')
                if len(shown) > 10 or rest:
                    print(f'    ... {len(shown[10:]) + len(rest)} more' + (no_name if rest and not shown[10:] else ''))
        if missing:
            print(f'  {len(missing)} indexed TU files no longer exist: ' + ', '.join(missing[:10]))


def in_range(pos, b, e):
    return b <= pos < e


def classify_ranges(ranges, line, col, readers):
    """(line, col) を含む範囲の (種類, 範囲の表示)。コメントを優先する。
    無効な #if は、そのファイルを読んだ構成(readers)のすべてで無効なときだけ inactive にする。"""
    hit = [(k, b, e, n) for k, b, e, n in ranges
           if in_range((line, col), b, e) or (k == 'inactive' and b[0] <= line <= e[0])]
    hit.sort(key=lambda x: (x[0] != 'comment', x[0] != 'inactive'))
    # 無効な #if と捨てられた引数は、そのファイルを読んだすべての構成で、どちらかの理由でコンパイルされないときだけ
    # (Release でだけ消える assert などは他の構成でコンパイルされる)。そうでなければ両方を外し、残りの種類(local など)で決める
    off = {n for k, _, _, n in hit if k in ('inactive', 'dropped')}
    if off and not readers <= off:
        hit = [h for h in hit if h[0] not in ('inactive', 'dropped')]
    if not hit:
        return None, ''
    k, b, e, _ = hit[0]
    return k, (f'  [lines {b[0]}-{e[0]}]' if b[0] != e[0] else '')


def walk_sources(idx, excludes, files=None):
    """ルート配下のソース(files を渡せばそのファイル)を (相対パス, 絶対パス, 更新日時) で返す。順はフォルダごとに、ファイルを先、サブフォルダを後にする。
    excludes はディレクトリ名(どこにあっても除く)か、ルートからの相対パスの接頭辞。"""
    pre = tuple(e.strip('/') + '/' for e in excludes if '/' in e)
    names = {e for e in excludes if '/' not in e}

    def order(rel):
        *dirs, name = rel.lower().split('/')
        return tuple((1, d) for d in dirs) + ((0, name),)

    for rel, (fp, m) in sorted((idx.sources() if files is None else files).items(), key=lambda kv: order(kv[0])):
        if pre and rel.startswith(pre):
            continue
        if names and not names.isdisjoint(rel.split('/')[:-1]):
            continue
        yield rel, fp, m


DEFINE = re.compile(rb'[ \t]*#[ \t]*define\b')
DIRECTIVE = re.compile(rb'[ \t]*#[ \t]*(?:pragma|include|include_next|import|error|warning|line|region|endregion)\b')


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
    if DIRECTIVE.match(data, b):  # #pragma region Foo・#include "foo.h"・#error の文など
        return 'directive'
    return None


def text_hits(idx, short, excludes, bad=None):
    """ソース中の単語一致を (path, line, col, src, stale, hint) で返す。col は clang と同じ 1 始まりのバイト位置。
    bad(集合)を渡すと、読めなかったファイルの相対パスを足す(その中の一致は出ないので、呼び出し側が伝える)。"""
    sb = short.encode('utf-8')
    hits = []

    def load(chunk):  # 読むのは並列にする(大きなコードでは読み込みの待ちが大半)
        out, skipped = [], []
        for rel, fp, mtime in chunk:
            try:
                with open(fp, 'rb') as f:
                    data = f.read()
            except FileNotFoundError:  # 一覧を作った後に消えた(rg でも出ない)
                continue
            except OSError:  # ほかのプログラムが開いている・権限が無いなど
                skipped.append(rel)
                continue
            if sb in data or data[:2] in (b'\xff\xfe', b'\xfe\xff'):  # UTF-16 は下で UTF-8 に直してから探す
                out.append((rel, mtime, data))
        return out, skipped

    files, test = idx.sources(), None
    if idx.words is not None:
        try:
            sizes = getattr(idx.tree, 'sizes', None)
            idx.words.update(files, idx.codec(), sizes)
            test = idx.words.may_contain(short, sizes)
        except Exception as e:  # 子のプロセスが落ちたなど。絞らずに全部読む
            print(f'ltq: word filter unavailable ({type(e).__name__}: {e}); reading every source', file=sys.stderr)
    if test is not None:  # 名前の単語が無いと分かっているファイルは読まない。並べる前に絞る(20 万ファイルを並べるだけで 0.6 秒かかる)
        files = {rel: v for rel, v in files.items() if test(rel, v[1])}
    # ファイルごとに仕事を渡すと、20 万ファイル(UE5)では受け渡しだけで数秒かかるので、続きのファイルをまとめて渡す
    items = list(walk_sources(idx, excludes, files))
    size = max(1, -(-len(items) // 256))
    with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as ex:
        parts = list(ex.map(load, (items[i:i + size] for i in range(0, len(items), size))))
    if bad is not None:
        bad.update(rel for _, skipped in parts for rel in skipped)
    for rel, mtime, data in (x for out, _ in parts for x in out):
        data = to_utf8(data, idx.codec())  # Shift_JIS の 2 バイト目が英字になる偽の一致も、直してから探せば出ない
        stale = mtime > idx.mtime
        ln, last = 1, 0
        for pos in word_starts(data, sb):
            ln += data.count(b'\n', last, pos)  # 行番号は直前の一致から数え足す
            last = pos
            b = data.rfind(b'\n', 0, pos) + 1
            e = data.find(b'\n', pos)
            src = data[b:e if e >= 0 else len(data)].strip().decode('utf-8', 'replace')[:200]
            col = pos - b + 1
            hits.append((rel, ln, col, src, stale, lexical_hint(data, b, col)))
    return hits


def other_scan(args):
    """[(相対パス, 絶対パス)] の各ファイルから、sb を単語として含む行を (path, line, src) で探す。別のプロセスでも動く。
    戻り値は (一致, 読めなかったファイルの相対パス)。"""
    items, sb, codec = args
    out, skipped = [], []
    for rel, fp in items:
        # 行の途中で分けた続き(1 行だけの大きなファイル)は、分け目の前後を合わせて見る。分け目は単語の途中でないだけなので、
        # 記号や空白を含む名前(operator==・~Foo)は分け目をまたぐことがあり、分け目で終わる一致は次のバイトがまだ見えない
        # - tail: 前の分の末尾(名前の長さまで)。次の分の頭に足す。またぐ一致と、その 1 つ前のバイトが見える
        # - held: 分の末尾で終わった一致。次の分の最初のバイトが単語の文字でなければ(か、次の分が無ければ)一致
        tail, held = b'', None
        try:
            for first, data in other_chunks(fp):
                data = to_utf8(data, codec)
                if held and data:
                    if data[0] not in WORD_BYTE:
                        out.append(held)
                    held = None
                seen = len(tail)
                if tail:  # ふつうは空(行の区切りで分かれる)。大きな分を写さない
                    data = tail + data
                tail =data[-len(sb):].rpartition(b'\n')[2]  # 行の区切りで終わっていれば空
                if sb not in data:
                    continue
                ln, last = first, 0
                for pos in word_starts(data, sb):
                    if pos + len(sb) <= seen:
                        continue  # 足した部分に収まる一致は、前の分で見た
                    ln += data.count(b'\n', last, pos)
                    last = pos
                    if out and out[-1][:2] == (rel, ln):
                        continue  # 同じ行の 2 つ目の一致
                    b = data.rfind(b'\n', 0, pos) + 1
                    e = data.find(b'\n', pos, b + 800)  # 見せるのは行の頭の 200 字まで。長い行を全部は写さない
                    hit = (rel, ln, data[b:e if e >= 0 else b + 800].strip().decode('utf-8', 'replace')[:200])
                    if pos + len(sb) == len(data):
                        held = hit
                    else:
                        out.append(hit)
        except FileNotFoundError:  # 一覧を作った後に消えた(rg でも出ない)
            pass
        except OSError:  # ほかのプログラムが開いている・権限が無いなど。途中まで読めた分の一致は残す
            skipped.append(rel)
        if held:
            out.append(held)
    return out, skipped


def other_hits(idx, short, excludes, bad=None):
    """C/C++ のソースでないファイルの単語一致を (path, line, src) で返す。中身は解析しない(rg と同じく、行を見せるだけ)。
    bad(集合)を渡すと、読めなかったファイルの相対パスを足す。"""
    sb = short.encode('utf-8')

    def gather(parts):
        parts = list(parts)  # 全部そろってから bad に足す(プロセスが落ちてスレッドで読み直すときに、2 度足さない)
        if bad is not None:
            bad.update(rel for _, skipped in parts for rel in skipped)
        return [x for out, _ in parts for x in out]
    files, sizes = idx.others(), None
    if idx.other_words is not None:  # 長く動くプロセスでは、名前の単語が無いと分かっているファイルを読まない(ソースと同じ)
        try:
            sizes = getattr(idx.tree, 'sizes', None)
            idx.other_words.update(files, idx.codec(), sizes)
            test = idx.other_words.may_contain(short, sizes)
        except Exception as e:  # 子のプロセスが落ちたなど。絞らずに全部読む
            print(f'ltq: word filter unavailable ({type(e).__name__}: {e}); reading every file', file=sys.stderr)
            test = None
        if test is not None:  # 並べる前に絞る(ソースと同じ)
            files = {rel: v for rel, v in files.items() if test(rel, v[1])}
    codec = idx.codec()
    pairs = [(rel, fp) for rel, fp, _ in walk_sources(idx, excludes, files)]
    total = sum(sizes.get(rel, 0) for rel, _ in pairs) if sizes else 0
    if total >= PROC_BYTES:
        # 名前を含む大きなファイルが多い(UE5 では、翻訳のファイル 149 個・5.3 GB が Initialize を含む)。探すのは CPU の仕事で、
        # スレッドでは並ばないので、プロセスに分ける。順番を保つように、続きのファイルを大きさでまとめて渡す
        n = min(8, os.cpu_count() or 4)
        groups, room = [[]], total // (n * 8)
        for rel, fp in pairs:
            if room <= 0 and groups[-1]:
                groups.append([])
                room = total // (n * 8)
            groups[-1].append((rel, fp))
            room -= sizes.get(rel, 0)
        try:
            with ProcessPoolExecutor(max_workers=n) as ex:
                return gather(ex.map(other_scan, ((g, sb, codec) for g in groups)))
        # 子のプロセスを起こせない・子が落ちた。下のスレッドで読み直す。子の中の検索が上げた例外(メモリ不足など)は、
        # 読み直しても同じことになるので、ここでは受けない
        except (OSError, ImportError, BrokenProcessPool) as e:
            print(f'ltq: worker processes unavailable ({type(e).__name__}: {e}); reading in threads', file=sys.stderr)
    size = max(1, -(-len(pairs) // 256))
    with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as ex:
        return gather(ex.map(other_scan, ((pairs[i:i + size], sb, codec) for i in range(0, len(pairs), size))))


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


def areas(dirs):
    """[(フォルダ, 件数)] を、上の階層でまとめた件数にして、多い順に 1 行に並べる。数は切らない(切った分はどこにも名前が
    出なくなる。いちばん上の階層のフォルダの数で抑えられる)。全部が同じ階層の下に
    あるときは、分かれるところまで階層を下りる(--path で絞り直したときに、同じ 1 つの名前に戻らないように)。"""
    deepest = max(d.count('/') + 1 for d, _ in dirs)
    depth = 1
    while True:
        groups = {}
        for d, c in dirs:
            key = '/'.join(d.split('/')[:depth])
            groups[key] = groups.get(key, 0) + c
        if len(groups) > 1 or depth >= deepest:
            break
        depth += 1
    top = sorted(groups.items(), key=lambda kv: (-kv[1], kv[0]))
    return ', '.join(f'{k}/ {c}' for k, c in top)


def top_dirs(paths, n):
    """一致のあったファイルのパスを、ディレクトリごとの件数にまとめ、多い順に n 個まで。残りは 1 行の件数にする。
    名前だけの一致は大きなコードでは数百のフォルダに散り、全部並べると結果の大半を占める(O3DE の Create で 180 フォルダ)。
    残りのフォルダも、上の階層ごとの件数は出す(どこにも名前の出ない行を作らない。--path にその階層を渡せば続きが出る)。"""
    agg = {}
    for p in paths:
        d = p.rsplit('/', 1)[0] if '/' in p else '.'
        c, files = agg.setdefault(d, [0, set()])
        agg[d][0] = c + 1
        files.add(p)
    if len(agg) == 1 and len(next(iter(agg.values()))[1]) > 1:
        # 1 つのフォルダに収まるときは、ファイルごとの件数にする(--path でそのフォルダを渡すと、同じ件数に戻って先へ
        # 進めなくなる。O3DE の Code/Framework/AzCore/Tests/ で 231 行・24 ファイル)。ファイルは --path に渡せる
        per = collections.Counter(paths)
        files = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))
        out = [f'{p}  {c} matches' for p, c in files[:n]]
        if len(files) > n:
            out.append(f'... {len(files) - n} more files ({sum(c for _, c in files[n:])} matches): '
                       + ', '.join(f'{p.rsplit("/", 1)[-1]} {c}' for p, c in files[n:]))
        return out
    top = sorted(agg.items(), key=lambda kv: (-kv[1][0], kv[0]))
    out = [f'{d}/  {c} matches in {len(fs)} files' for d, (c, fs) in top[:n]]
    if len(top) > n:
        out.append(f'... {len(top) - n} more folders ({sum(c for _, (c, _) in top[n:])} matches) under: '
                   + areas([(d, c) for d, (c, _) in top[n:]]))
    return out


def path_filter(prefixes, root=None):
    """--path の指定(ルートからの相対の、フォルダかファイル)に入るパスかを見る関数。指定が無ければ None。
    ルートの下の絶対パスも受ける(MCP のクライアントは root を絶対パスで渡すので、path も絶対で来ることがある)。"""
    base = (root or '').replace('\\', '/').rstrip('/').lower()
    pre = set()
    for p in prefixes:
        p = p.replace('\\', '/')
        if base and (p.lower() == base or p.lower().startswith(base + '/')):
            p = p[len(base):] or '.'
        while p.startswith('./'):
            p = p[2:]
        p = p.strip('/').lower()
        if p and p != '.':
            pre.add(p)
    if not pre:
        return None
    dirs = tuple(p + '/' for p in pre)
    return lambda path: path.lower() in pre or path.lower().startswith(dirs)


def fmt(r, show_sym, idx, ranges=None):
    sym, role, caller, path, line, col, src, note, _, alt = r
    s = f'{role:<6} {path}:{line}'
    if show_sym:
        s += f'  {sym}'
    if caller and role in ('expand', 'macro'):
        s += f'  [-> {caller}]'  # 名前 1 つに置き換えるマクロの、置き換え後の名前
    elif caller:
        s += f'  [in {caller}]'
    if alt:  # その構成ではコンパイルされない #if の中。条件を仮に有効にして解析した行
        cond = idx.inactive_cond(path, line, (ranges or {}).get(path, ()))
        s += f'  [inactive: {cond}]' if cond else '  [inactive]'
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


def more_by_folder(paths, indent):
    """上限(--limit)で省いた行を、フォルダごとの件数で出す。件数だけにして、どこにあるか分からない行を作らない
    (rg なら 1 行ずつ出る行。フォルダが分かれば --path で 1 行ずつ出し直せる)。"""
    print(f'{indent}... {len(paths)} more (raise --limit, or ask with --path <folder>), by folder:')
    for s in top_dirs(paths, 10):
        print(indent + s)


def section(title, items, limit, pos, listed):
    """pos は items と同じ順の、行の位置 (パス, 行)。1 行ずつ出した位置を listed に足す。"""
    print(f'\n== {title}: {len(items)}')
    for s in items[:limit]:
        print('  ' + s)
    listed.update(pos[:limit])
    if len(items) > limit:
        more_by_folder([p for p, _ in pos[limit:]], '  ')


def cmd_refs(a, idx):
    name = a.name.lstrip(':')
    short = re.split(r'::', name)[-1]
    sb = short.encode('utf-8')
    if a.all:  # 1 行も件数にまとめない(名前やシグネチャを変えるときに、直す先を漏らさないための一覧)
        a.limit, a.compact = sys.maxsize, 0
    listed = set()  # 1 行ずつ出した行の位置 (パス, 行)。最後に、名前を含む行のうち何行を並べたかを出す

    def match(sym):
        return sym == name or sym.endswith('::' + name)

    # 記号の列の末尾が short の行だけを読む(`short<TAB>` を含む行。`Loader` などの部分一致の行は読まない)
    rows = idx.cached(('rows', short), lambda: [r for r in idx.rows([sb + b'\t'], lambda raw: True, name=sb)
                                                 if r[0].rsplit('::', 1)[-1] == short])
    # テキストの一致(下の「索引に無い一致」で使う)。名前を含むファイルを先に知り、索引の状態はそのファイルの分だけ並べる
    # 読めなかったファイル(text_hits と other_hits が足す。一致の写しと同じ間だけ保つ)
    bad = idx.cached(('unread', short, tuple(sorted(a.exclude))), set)
    hits = idx.cached(('text', short, tuple(sorted(a.exclude))), lambda: text_hits(idx, short, set(a.exclude), bad))
    focus = {h[0] for h in hits} | {r[3] for r in rows}
    # 仮想関数の上書き(上書きするメソッド -> 上書きされる基底のメソッド)。同じ名前なので rows に入っている
    up, down, ov_at = {}, {}, {}
    for r in rows:
        if r[1] == OVERRIDE:
            up.setdefault(r[0], set()).add(r[2])
            down.setdefault(r[2], set()).add(r[0])
            ov_at.setdefault(r[0], (r[3], r[4]))
    rows = [r for r in rows if r[1] != OVERRIDE]
    parts = [p for p in name.split('::') if p]
    # 名前を置き換えるマクロ(windows.h の GetMessage -> GetMessageW など)。マクロ short の展開の位置にある、別の名前の行を引く。
    # 修飾名で問われてその記号が無ければ、同じ修飾の置き換え後の名前の記号を答えにする(Mail::GetMessage -> Mail::GetMessageW)
    # ltidx は、名前 1 つに置き換えるオブジェクト形式のマクロの展開の行の 3 列目に、置き換え後の名前を書く。その名前で 1 回だけ引き、
    # 展開の位置にある行を突き合わせる(関数形式のマクロの本体の呼び出しは置き換えではないので見ない)
    exp_pos = {(r[3], r[4], r[5]) for r in rows if r[0] == short and r[1] == 'expand' and r[2]}
    to = sorted({r[2] for r in rows if r[0] == short and r[1] == 'expand' and r[2]} - {short})
    aliased, renamed = [], set()
    for s in to:
        b = s.encode('utf-8')
        got = idx.cached(('rows', s), lambda: [r for r in idx.rows([b + b'\t'], lambda raw: True, name=b)
                                               if r[0].rsplit('::', 1)[-1] == s])
        aliased += [r for r in got if (r[3], r[4], r[5]) in exp_pos and r[1] not in MACRO_ROLES + (OVERRIDE,)]
        if len(parts) > 1 and not any(match(r[0]) for r in rows if r[1] not in MACRO_ROLES):
            scope = '::'.join(parts[:-1])
            hit = {r[0] for r in got if r[0].rsplit('::', 1)[0] == scope or r[0].rsplit('::', 1)[0].endswith('::' + scope)}
            renamed |= hit
            rows += [r for r in got if r[0] in hit and r[1] != OVERRIDE]
            for r in got:
                if r[0] in hit and r[1] == OVERRIDE:
                    up.setdefault(r[0], set()).add(r[2])
                    down.setdefault(r[2], set()).add(r[0])
                    ov_at.setdefault(r[0], (r[3], r[4]))
    target = [r for r in rows if match(r[0]) or r[0] in renamed]
    deps = [r for r in rows if r[0] == '?::' + short]
    syms = sorted({r[0] for r in target})
    show_sym = len(syms) > 1
    is_macro = all(r[1] in MACRO_ROLES for r in target)  # --path で絞る前に決める(絞って 0 行になっても変わらないように)
    # --path: 記号は索引の全体から決め、並べる行(索引の行とテキストの一致)だけを、指定のフォルダ・ファイルに絞る。
    # 短い出力でフォルダごとの件数にまとめた行を、1 行ずつ出し直すのに使う
    inpath = path_filter(a.path, idx.root)
    if inpath:
        target = [r for r in target if inpath(r[3])]
        deps = [r for r in deps if inpath(r[3])]
        hits = [h for h in hits if inpath(h[0])]
    # 修飾名で問われたら、`Scope::Name` と書かれた行は短い形でも省かない(無効な #if の中の呼び出しなど。O3DE の
    # DynamicModuleHandle::Create では、正解 17 のうち 2 つが無効な #if の中にあり、件数にまとめると消えていた)
    tail = re.compile(r'(?<![\w])' + r'\s*::\s*'.join(map(re.escape, parts[-2:])) + r'(?![\w])') if len(parts) > 1 else None

    def named(src):
        return bool(tail and tail.search(src))

    if tail:  # 修飾名なら、末尾の名前だけの一致(O3DE の Create で数千ファイル)ではなく、修飾名を書いたファイルと結果のファイル
        focus = {h[0] for h in hits if named(h[3])} | {r[3] for r in target + deps}
    idx.header(focus)
    print(f'query: {a.name}')
    if inpath:
        print('  (--path: only lines in ' + ', '.join(a.path) + ' are listed and counted)')
    if a.all:
        print('  (--all: every line that contains the name is listed, one per line; nothing is folded into counts)')
    if a.compact:
        # 1 段目は、コードでありうる分類(無効な #if・マクロの定義・読まれていないファイル・その他)を 1 行ずつ並べる。
        # 2 段目(--compact を 2 つ)は、それもフォルダごとの件数にする
        print('  (compact output: comments, strings, local names'
              + (', inactive #if, macro bodies' if a.compact > 1 else '')
              + ' and matches that resolve to other symbols are summarized as counts; '
              + (f'calls, likely calls and lines that spell {"::".join(parts[-2:])}' if tail else 'calls and likely calls')
              + ' are listed in full'
              + (f', other text matches up to {COMPACT_LIST} per group' if a.compact == 1 else '') + ')')
    if not syms:
        print('  no symbol in the index matches this name.')
    elif show_sym:
        # 短い名前は数百の記号に当たる(O3DE の Activate で 47 KB の 1 行)。行の多い順に上限まで並べ、残りは件数にする。
        # 行そのものは下の節に記号つきで出るので、ここで省いても見えなくなる行は無い
        per = collections.Counter(r[0] for r in target)
        top = sorted(syms, key=lambda s: (-per[s], s))
        cap = len(top) if len(top) <= SYM_LIST + 5 else SYM_LIST
        print('  matched symbols: ' + ', '.join(f'{s} ({per[s]})' for s in top[:cap])
              + (f', ... {len(top) - cap} more symbols ({sum(per[s] for s in top[cap:])} rows; each row below names'
                 ' its symbol. Ask with a qualified name to narrow)' if len(top) > cap else ''))
    else:
        print(f'  symbol: {syms[0]}' + ('  (a macro)' if is_macro else ''))
    if renamed:
        print(f'  the macro {short} renames it to {", ".join(sorted(renamed))}; the results below are for that name')
    if aliased:
        cnt = collections.Counter((r[0], r[1]) for r in aliased)
        per = collections.Counter(r[0] for r in aliased)
        by = {}
        for (s, role), n in cnt.items():
            by.setdefault(s, []).append(f'{role} {n}')
        top = sorted(by.items(), key=lambda kv: -per[kv[0]])
        print(f'  where the macro {short} is expanded, clang sees: '
              + ', '.join(f'{s} ({", ".join(v)})' for s, v in top[:10]) + (' ...' if len(top) > 10 else ''))

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
    n_alt = sum(1 for r in target + deps if r[9])
    if n_alt:
        print(f'  {n_alt} lines below are marked [inactive: #if ...]: code the indexed configuration does not compile, parsed'
              ' with that condition forced on (best effort). Still code: include them when renaming or changing a signature')
    # 行ごとの索引の行(at)。テキストの一致のうち索引に行の無いもの(loose)のファイルと、無効な #if の中として解析した行の
    # ファイルについて、コメント・無効な #if の範囲を引く(下の「索引に無い一致」の分類と、無効な行に添える条件に使う)
    at = {}
    for r in rows:
        at.setdefault((r[3], r[4]), []).append(r)
    loose = [h for h in hits if (h[0], h[1]) not in at]
    want = frozenset({h[0] for h in loose} | {r[3] for r in rows if r[9]})
    ranges = idx.cached(('skips', want), lambda: idx.skips(want))

    def show(r, with_sym):
        return fmt(r, with_sym, idx, ranges)

    calls = [r for r in target if r[1].startswith('call')]
    expands = [r for r in target if r[1] == 'expand']
    defs = sorted((r for r in target if r[1].startswith(DEFS) or r[1] == 'macro'), key=key)
    uses = sorted((r for r in target if not r[1].startswith(('call',) + DEFS) and r[1] not in ('expand', 'macro')), key=key)
    section('calls (resolved by clang)', [show(r, show_sym) for r in calls], a.limit, [(r[3], r[4]) for r in calls], listed)
    if calls:
        print('  (exact: clang resolved each call from the real type of its receiver, whatever the variable is named)')
    if any(r[1].endswith('-using') for r in target):
        print('  (call-using: a call through a using-declaration inside a template, resolved from its instantiations)')
    if expands:
        section('macro expansions (where the macro name is written in the code)', [show(r, show_sym) for r in expands],
                a.limit, [(r[3], r[4]) for r in expands], listed)
    section('other references (address taken, template arguments, writes, #ifdef, ...)',
            [show(r, show_sym) for r in uses], a.limit, [(r[3], r[4]) for r in uses], listed)
    section('definitions and declarations', [show(r, show_sym) for r in defs], a.limit, [(r[3], r[4]) for r in defs], listed)

    # 基底のポインタや参照越しの呼び出しは、clang が基底のメソッドに解決する。実行時に上書きへ届くかは型次第
    bases, derived = closure(syms, up), closure(syms, down)
    if bases or derived:
        print('\n== virtual dispatch')
    if bases:
        via = sorted((r for r in rows if r[0] in bases and r[1].startswith('call') and (not inpath or inpath(r[3]))), key=key)
        print(f'  the query overrides {", ".join(sorted(bases))}. Calls to those may run the query at runtime,'
              ' depending on the dynamic type of the receiver; they are not counted above:'
              f' {len(via)}')
        for s in [show(r, True) for r in via][:a.limit]:
            print('    ' + s)
        listed.update((r[3], r[4]) for r in via[:a.limit])
        if len(via) > a.limit:
            more_by_folder([r[3] for r in via[a.limit:]], '    ')
    if derived:
        n = sum(1 for r in rows if r[0] in derived and r[1].startswith('call'))
        print(f'  overridden by (calls through a pointer or reference to the query\'s class may run these instead;'
              f' calls resolved to these, {n}, call the override, not the query):')
        # 上書きの一覧は、呼び出し元を聞かれたときには要らないことが多く、大きな階層では結果の大半を占める
        # (コードベース A の Object::GetType で 74 行・結果 22 KB の半分)。既定では 10 まで、--limit を下げればそれまで
        cap = len(derived) if a.all else min(a.limit, 10)
        for d in sorted(derived)[:cap]:
            p, ln = ov_at.get(d, ('?', 0))
            print(f'    {d}  {p}:{ln}')
            listed.add((p, ln))
        if len(derived) > cap:
            print(f'    ... {len(derived) - cap} more overrides (refs <Class>::{short} for one of them)')

    # dep 行: テンプレート本体では未解決。実体化で解決された位置は、その解決先を添える
    target_pos = {(r[3], r[4]) for r in target}
    def_files = {r[3] for r in defs}
    dep_lines, dep_paths, dep_dup, dep_other, other_paths = [], [], 0, {}, []
    for r in sorted(deps, key=key):
        if (r[3], r[4]) in target_pos:
            dep_dup += 1  # 実体化で対象に解決済み。上の calls に載っている
            continue
        resolved = sorted({x[0] for x in at[(r[3], r[4])]
                           if x[0] != r[0] and not x[1].startswith(DEFS + (OVERRIDE,)) and x[1] not in MACRO_ROLES})
        if resolved and a.compact:  # 短い出力では、別の記号に解決された行は件数とフォルダごとの件数にする
            for s in resolved:
                dep_other[s] = dep_other.get(s, 0) + 1
            other_paths.append(r[3])
            continue
        if resolved:
            mark = '  (instantiations resolve it to ' + ', '.join(resolved) + ' -> probably not the query)'
        else:
            mark = '  (never instantiated; unknown target' + (', same file as the definition)' if r[3] in def_files else ')')
        dep_lines.append(show(r, False) + mark)
        dep_paths.append((r[3], r[4]))
    n_other = len(other_paths)  # 1 行が複数の記号に解決されることがあるので、記号ごとの件数の和ではなく行の数
    print(f'\n== unresolved template calls named "{short}" (dep): {len(dep_lines) + n_other}'
          + (f' (+{dep_dup} already listed in calls)' if dep_dup else ''))
    if dep_lines:
        print('  clang could not resolve these inside templates. Decide from the source line and the')
        print('  enclosing function whether each one calls the queried symbol; they are not counted above.')
    if dep_other:
        top = sorted(dep_other.items(), key=lambda kv: -kv[1])
        print(f'  {n_other} of them are resolved by instantiations to other symbols -> probably not the query: '
              + ', '.join(f'{s} ({n})' for s, n in top[:8]) + (f', ... {len(top) - 8} more symbols' if len(top) > 8 else ''))
        for s in top_dirs(other_paths, 5):  # rg なら出る行。どこにあるかは残す(--path で 1 行ずつ出る)
            print('    ' + s)
    for s in dep_lines[:a.limit]:
        print('  ' + s)
    listed.update(dep_paths[:a.limit])
    if len(dep_lines) > a.limit:
        more_by_folder([p for p, _ in dep_paths[a.limit:]], '  ')

    # テキスト一致のうち、索引に同名の行が無いものの理由
    known = {r[3] for r in rows}
    parsed = known | set(ranges) | idx.tu_status()[2]
    tagged = set(syms) | {'?::' + short} | bases | derived | ({short} if renamed else set())
    others = {}
    for r in rows:
        if r[0] not in tagged and (not inpath or inpath(r[3])):
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
    query_syms = set(syms) | bases

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

    def classify():
        cats = {k: [] for k in ('comment', 'inactive', 'dropped', 'local', 'unparsed', 'string', 'macro', 'directive', 'other')}
        for path, line, col, src, stale, hint in loose:
            readers = {n for n, s in idx.seen().items() if path in s}
            k, span = classify_ranges(ranges.get(path, []), line, col, readers)
            if k is None:
                k = (hint or 'other') if path in parsed else 'unparsed'
            elif k not in cats:  # この ltq が知らない種類(新しい ltidx が書いた範囲)。落とさずに「その他」で並べる
                k, span = 'other', ''
            like, verdict = resolved_like(src) if k in ('unparsed', 'other') else ('', '')
            if k in ('inactive', 'dropped') and named(src):
                # 短い形でも並べる(修飾名がそのまま書かれている)。読まれていないファイルの呼び出しには上の手がかりが付くので、
                # 手がかりの無い無効な #if だけにする(生成物の u8"Scope::Name" のような文字列の行まで並べない)
                verdict = 'named'
            if k == 'inactive':  # 行の範囲の代わりに、囲む条件を添える(どの構成で有効になるコードかが分かる)
                cond = idx.inactive_cond(path, line, ranges.get(path, ()))
                span = f'  [{cond}]' if cond else span
            cats[k].append((path, f'{path}:{line}{span}{"  [file changed after indexing]" if stale else ""}{like}  | {src}',
                            verdict, line))
        return cats

    # 分類は、出力の形(--compact・--limit・--all)では変わらない。MCP サーバーが短い形で出し直す間は使い回す
    # (UE5 の Initialize では 1 万行あり、出し直しのたびに 0.7 秒かかっていた)
    cats = idx.cached(('cats', a.name, tuple(sorted(a.exclude)), tuple(a.path)), classify)

    print(f'\n== text matches of "{short}" not in the index: '
          f'{sum(len(v) for v in cats.values())} of {len(hits)} word matches')
    no_alt = [p for p in idx.parts if not p.alt_ok]
    groups = [
        ('comment', 'inside comments -> not code; do not report these', False),
        ('inactive', 'inside inactive #if/#ifdef branches -> not compiled in the indexed configuration, not resolved;'
                     ' still code to update when renaming or changing a signature', False),
        ('dropped', 'inside macro arguments the expansion does not keep as code (a disabled assert or log, an attribute'
                    ' marker, # or ##) -> not compiled here, but still written in the source: report them if the question'
                    ' is about where the name is written', False),
        ('local', 'local variables, parameters or template parameters with this name -> other symbols, not the query', False),
        ('macro', 'inside macro definitions -> expansions are indexed at the use site', False),
        ('string', 'inside string literals -> not code', True),
        ('directive', 'in #pragma / #include / #error lines -> not code', True),
        ('unparsed', 'in files no indexed TU includes -> not analyzed; read them if they matter', False),
        ('other', 'other -> not explained by the index; read them if they matter', False),
    ]
    folded = bool(other_paths)  # 上の dep 行をフォルダごとの件数にしたか
    for k, title, fold in groups:
        items = cats[k]
        if not items:
            continue
        # one: 1 行ずつ並べる行、rest: フォルダごとの件数にする行のパス。上限(--limit)で並べきれない行も rest に回す
        # (件数だけにして、どこにあるか分からない行を作らない。フォルダが分かれば --path で 1 行ずつ出し直せる)
        one, notes, rest, ndirs = [(p, s, ln) for p, s, _, ln in items], [], [], 10
        print(f'  {title}: {len(items)}')
        if k == 'inactive' and no_alt:  # 古い索引か、-NoAlt で作った索引
            print(f'    ({os.path.basename(no_alt[0].path)}{ALT} is missing or older than the index, so these branches'
                  ' were not parsed;'
                  ' rebuild the index with index-project to resolve the calls in them)')
        if a.compact and k != 'dropped':  # 捨てられた引数の行は答えに入りうるので、短い形でもまとめない(下の elif)
            # 短い出力: 問い合わせの呼び出しらしい行と修飾名の行は並べ、別の記号らしい行は件数にする。残りは、
            # コードではない分類(コメント・文字列・指令・ローカルの名前)ならフォルダごとの件数に、コードでありうる分類
            # (無効な #if・マクロの定義・読まれていないファイル・その他)なら 1 段目では COMPACT_LIST 行まで並べる
            # (rg なら 1 行ずつ出る行を、件数だけにしない)。2 段目ではそれもフォルダごとの件数にする
            low = k in ('comment', 'local', 'string', 'directive') or (a.compact > 1 and k in ('inactive', 'macro'))
            nots = sum(1 for _, _, v, _ in items if v == 'not')
            one = [(p, s, ln) for p, s, v, ln in items if v in ('call', 'named')]
            if nots:
                notes.append(f'{nots} look like calls to other symbols (the same line or receiver resolves elsewhere)')
            plain = [(p, s, ln) for p, s, v, ln in items if not v]
            keep = 0 if low else COMPACT_LIST if a.compact == 1 else len(plain) if len(items) <= 10 else 0
            one += plain[:keep]
            rest, ndirs = [p for p, _, _ in plain[keep:]] + [p for p, _, v, _ in items if v == 'not'], 5 if low else 10
        elif a.compact:  # 短い形の dropped: 答えに入りうるので並べるが、上限に収まるよう 50 行まで
            one, rest = one[:50], [p for p, _, _ in one[50:]]
        elif fold and len(items) > 10 and not a.all:
            # 全文でも、コードでない分類は 10 行を超えたらまとめる。手がかりの付いた行はまとめない(--all ではまとめない)
            one = [(p, s, ln) for p, s, v, ln in items if v]
            rest = [p for p, _, v, _ in items if not v]
            ndirs = len(rest)
        rest = [p for p, _, _ in one[a.limit:]] + rest
        for p, s, ln in one[:a.limit]:
            print('    ' + s)
            listed.add((p, ln))
        for s in notes + (top_dirs(rest, ndirs) if rest else []):
            print('    ' + s)
        folded = folded or bool(rest)
    # C/C++ のソースでないファイルの一致。索引も ltq も中身を解析しないが、rg なら出る行なので、あることは必ず伝える
    # (名前を変えるなら、生成器・ビルドの定義・スクリプトも直す先になる)
    extra = idx.cached(('other', short, tuple(sorted(a.exclude))), lambda: other_hits(idx, short, set(a.exclude), bad))
    if inpath:
        extra = [h for h in extra if inpath(h[0])]
    if extra:
        print(f'\n== matches of "{short}" in files that are not C/C++ sources (scripts, build files, docs, other languages'
              f' -> not analyzed; update them too when renaming): {len(extra)}')
        keep = min(a.limit, len(extra) if not a.compact else OTHER_LIST if a.compact == 1 else 0)
        for p, ln, src in extra[:keep]:
            print(f'  {p}:{ln}  | {src}')
            listed.add((p, ln))
        if extra[keep:]:
            for s in top_dirs([p for p, _, _ in extra[keep:]], 10 if not a.compact else 5):
                print('  ' + s)
            folded = True
    if folded:
        print('  (to list the lines of a folder that is shown as a count, ask again with --path <folder>)')
    if others:
        top = sorted(others.items(), key=lambda kv: -kv[1])
        print(f'\n== other symbols named "{short}" (not the query): ' +
              ', '.join(f'{s} ({n})' for s, n in top[:15]) + (' ...' if len(top) > 15 else ''))
    # 名前を含む行(rg -w なら出る行)のうち、上に 1 行ずつ出した行の数。残りは、フォルダ・ファイル・記号ごとの件数にしか出ていない
    hit_pos = {(h[0], h[1]) for h in hits} | {(p, ln) for p, ln, _ in extra}
    if a.all:
        # まだ並べていない行: 索引が同じ名前の別の記号に解決した行(修飾名で問うたとき)と、上書き・基底の記号の行。
        # 問い合わせの記号ではないが、rg なら出る行なので 1 行ずつ出す(無効な #if の中を仮に有効にした解析は、型を取り違えて
        # 別の記号に解決することがある。件数にまとめると、その行がどこにあるか分からなくなる)
        left = sorted(hit_pos - listed)
        if left:
            src_of = {(h[0], h[1]): h[3] for h in hits}
            print(f'\n== lines the index resolves to other symbols named "{short}" (not the query; listed so that no line'
                  f' that contains the name is left out): {len(left)}')
            for p, ln in left:
                rs = at.get((p, ln), [])
                cond = ''
                if any(r[9] for r in rs):
                    c = idx.inactive_cond(p, ln, ranges.get(p, ()))
                    cond = f'  [inactive: {c}]' if c else '  [inactive]'
                print(f'  {p}:{ln}  {", ".join(sorted({r[0] for r in rs})) or "(not classified)"}{cond}'
                      f'  | {src_of.get((p, ln), rs[0][6] if rs else "")}')
            listed.update(left)
    n_all, n_listed = len(hit_pos), len(hit_pos & listed)
    # 読めなかったファイルの中の行は、上のどこにも出ず、下の数にも入らない。黙って「全部」と言わない
    unread = sorted(p for p in bad if not inpath or inpath(p))
    if unread:
        keep = len(unread) if a.all else UNREAD_LIST
        print(f'\n== not searched: {len(unread)} file{"s" if len(unread) > 1 else ""} could not be read (in use by another'
              ' program, or no permission); lines in them may be missing from the list and from the count below: '
              + ', '.join(unread[:keep]) + (f' ... ({len(unread) - keep} more; refs --all lists them)' if unread[keep:] else ''))
    # 「全部」は、探した範囲の中での全部。探していない所(rg --hidden や、--exclude を付けない rg なら出る行)を言う
    scope = (f' (searched: {"--path " + ", ".join(a.path) if inpath else "the root"}, except folders that start with "."'
             + (', the index files' if idx.own() else '')
             + (', --exclude ' + ', '.join(sorted(a.exclude)) if a.exclude else '')
             + (f', {len(unread)} file{"s" if len(unread) > 1 else ""} that could not be read' if unread else '') + ')')
    print(f'\n== coverage: {"all " if n_listed == n_all else f"{n_listed} of "}{n_all} lines that contain the word'
          f' "{short}" are listed above, one per line' + scope + ('' if n_listed == n_all else ALL_HINT))


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
    idx.header(paths)  # 問われたファイルの分だけ並べる
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
            rel = (f'  [overrides {r[2]}]' if r[1] == OVERRIDE else f'  [-> {r[2]}]' if r[2] and r[1] in ('expand', 'macro')
                   else f'  [in {r[2]}]' if r[2] else '')
            off = ''
            if r[9]:
                cond = idx.inactive_cond(path, line, ranges.get(path, ()))
                off = '  [inside an inactive #if, parsed with the condition forced on' + (f': {cond}]' if cond else ']')
            print(f'  indexed: {r[1]} {r[0]} at col {r[5]}' + rel + off
                  + (f'  [{r[7]}]' if r[7] else '') + (f'  [only in {only}]' if only else ''))
        shown = set()
        for k, b, e, n in ranges.get(path, []):
            if (col is not None and in_range((line, col), b, e)) or (col is None and b[0] <= line <= e[0]):
                what = {'comment': 'comment', 'dropped': 'macro argument the expansion does not keep',
                        'local': 'name of a local variable, parameter or template parameter'}.get(k, 'inactive #if branch')
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
    """<root>/.index の中の索引本体(.tu.tsv・.skip.tsv・.deps.tsv・.alt.tsv 以外の .tsv)すべて。構成ごとに 1 つ置く。
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
    r.add_argument('name', help='qualified name or its suffix, e.g. a::io::File::Read or File::Read')
    r.add_argument('--limit', type=int, default=200, help='max lines per section (default 200)')
    r.add_argument('--exclude', action='append', default=[], help='directory name to skip in the text search')
    r.add_argument('--path', action='append', default=[],
                   help='list only lines in this folder or file (relative to the root; repeatable). The symbol is still '
                        'resolved from the whole index. Use it to list a folder that a compact result shows as a count')
    r.add_argument('--all', action='store_true',
                   help='list every line that contains the name, one per line (no --limit, no folder counts, lines that '
                        'resolve to other symbols included): the complete list for a rename or a signature change')
    r.add_argument('--compact', action='count', default=0,
                   help='summarize low-value sections (comments, strings, local names, template calls resolved to '
                        'other symbols, matches resolved elsewhere) into counts per folder. Give it twice to also '
                        'summarize text matches in inactive #if branches, macro bodies and unparsed files')
    c = sub.add_parser('check', help='how the index treats each path:line[:col]')
    c.add_argument('locs', nargs='+')
    a = ap.parse_args(argv)
    if a.cmd == 'refs' and not re.split(r'::', a.name.lstrip(':'))[-1]:  # ""・"::"・"Foo::"。探す単語が無い
        print(f'refs needs a name after the last "::" (got "{a.name}")')
        return 2
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
