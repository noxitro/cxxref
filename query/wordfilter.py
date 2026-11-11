"""ソースのファイルごとに、そこに現れる単語(英数字と _ の並び)の Bloom フィルタを持ち、テキスト検索で読むファイルを絞る。

ltq の text_hits() は、問い合わせのたびにルート配下のソースをすべて読む(UE5 の 2.4 GB で 1 回 10 秒ほど)。
単語の一致(`(?<![A-Za-z0-9_])名前(?![A-Za-z0-9_])`)は「その名前が、ファイルの英数字と _ の並びのどれかと等しい」ことと
同じなので、ファイルごとの単語の集合に名前が無ければ、そのファイルに一致は無い。集合の代わりに Bloom フィルタを持つので、
「無い」は確かで、「あるかもしれない」は数 % 外れる(外れたファイルは読んで調べるだけで、結果は変わらない)。

- 単語は、ltq と同じく UTF-8 に直した後のバイト列から取る(Shift_JIS などの 2 バイト目の英字を単語に数えない)
- UTF-16 のファイルと読めなかったファイルは絞らない(いつも読む)。ltq の扱いをそのまま通すため
- 英数字と _ だけでない名前(`operator==`・`~Foo`)では絞らない
- ファイルの更新日時が変わったものだけを作り直す。一覧を <索引>.words.cache に書き残し、次の起動で読む
"""
import os
import re
import hashlib
import struct
from concurrent.futures import ProcessPoolExecutor

WORD = re.compile(rb'[A-Za-z0-9_]+')
IDENT = re.compile(r'[A-Za-z0-9_]+')
K = 3  # 1 つの単語で立てるビットの数
BITS_PER_WORD = 8  # 単語 1 つあたりのビット数の目安(K=3 で外れは 3% ほど)
ALWAYS = -1  # 絞らないファイル(UTF-16 のソース・読めない・単語の種類が多すぎる)の印
MAX_WORDS = 1000000  # C/C++ 以外のファイルで、単語の種類がこれを超えたら絞らない(集合を貯め続けない。フィルタも 1 MB を超える)
MAGIC = b'cxxref-words 4\n'  # 作り方を変えたら上げる(古い一覧を読まない)


def _positions(word, m):
    # CRC32 は線形で、同じ長さの単語では下位のビットがそろいやすい(Bloom フィルタの外れが増える)ので、BLAKE2b の 64 ビットを使う
    h = int.from_bytes(hashlib.blake2b(word, digest_size=8).digest(), 'little')
    h1, h2 = h & 0xFFFFFFFF, (h >> 32) | 1
    return [(h1 + i * h2) & (m - 1) for i in range(K)]


def bloom_of(data):
    """(ビット数, フィルタの int)。data は UTF-8 に直した後のバイト列。"""
    return bloom_of_words(set(WORD.findall(data)))


def bloom_of_words(words):
    m = 64
    while m < BITS_PER_WORD * len(words):
        m <<= 1
    bits = bytearray(m >> 3)
    for w in words:
        for p in _positions(w, m):
            bits[p >> 3] |= 1 << (p & 7)
    return m, int.from_bytes(bits, 'little')


def _work(args):
    """別のプロセスで動く。[(相対パス, 絶対パス)] の各ファイルの (相対パス, ビット数, フィルタ)。
    other が真なら、C/C++ 以外のファイルとして読む(ltq.other_chunks: バイナリは最初の NUL より前だけ、大きなファイルは分けて、
    UTF-16 は UTF-8 に直して)。"""
    items, codec, other = args
    import ltq  # 子のプロセスでは、親の sys.path が引き継がれる
    out = []
    for rel, fp in items:
        if other:
            words, always = set(), False
            try:
                for _, data in ltq.other_chunks(fp):
                    words.update(WORD.findall(ltq.to_utf8(data, codec)))
                    if len(words) > MAX_WORDS:  # 大きなデータの書き出しなど。残りを読まずに、毎回探すファイルにする
                        always = True
                        break
            except OSError:
                always = True  # 読めない。絞らない
            out.append((rel, ALWAYS, 0) if always else (rel,) + bloom_of_words(words))
            continue
        try:
            with open(fp, 'rb') as f:
                data = f.read()
        except OSError:
            out.append((rel, ALWAYS, 0))
            continue
        if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
            out.append((rel, ALWAYS, 0))
            continue
        out.append((rel,) + bloom_of(ltq.to_utf8(data, codec)))
    return out


class WordFilter:
    def __init__(self, cache_path=None, other=False):
        self.cache_path = cache_path
        self.other = other  # C/C++ 以外のファイルのフィルタか(読み方を ltq.other_hits に合わせる)
        self.codec = None
        self._f = {}  # 相対パス -> ((更新日時, 大きさ), ビット数, フィルタ)。大きさが分からなければ -1
        self._masks = {}
        self._dirty = False

    def load(self, codec):
        self.codec = codec
        if not self.cache_path:
            return
        try:
            with open(self.cache_path, 'rb') as f:
                data = f.read()
        except OSError:
            return
        head = MAGIC + f'{codec}\n'.encode()
        if not data.startswith(head):
            return
        pos, out = len(head), {}
        try:
            while pos < len(data):
                n, m, nb = struct.unpack_from('<Iqi', data, pos)
                pos += 16
                rel = data[pos:pos + n].decode('utf-8')
                pos += n
                mt, sz = struct.unpack_from('<dq', data, pos)
                pos += 16
                out[rel] = ((mt, sz), m, int.from_bytes(data[pos:pos + nb], 'little'))
                pos += nb
        except (struct.error, UnicodeDecodeError):
            return
        self._f = out

    def save(self):
        if not self.cache_path or not self._dirty:
            return
        parts = [MAGIC + f'{self.codec}\n'.encode()]
        for rel, ((mt, sz), m, b) in self._f.items():
            r = rel.encode('utf-8')
            nb = 0 if m == ALWAYS else m >> 3
            parts.append(struct.pack('<Iqi', len(r), m, nb) + r + struct.pack('<dq', mt, sz)
                         + (b.to_bytes(nb, 'little') if nb else b''))
        tmp = f'{self.cache_path}.{os.getpid()}.tmp'
        try:
            with open(tmp, 'wb') as f:
                f.write(b''.join(parts))
            os.replace(tmp, self.cache_path)
            self._dirty = False
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def update(self, files, codec, sizes=None, workers=None, pool_min=200, save_min=500):
        """files({相対パス: (絶対パス, 更新日時)})に合わせる。新しい・変わったファイルだけを読む。
        sizes({相対パス: 大きさ})を渡すと、更新日時に加えて大きさも変更の判定に使う(更新日時を保ったまま中身を
        入れ替えるコピー・同期のツールがある)。戻り値は読んだファイルの数。多いときはプロセスを分けて読む。"""
        if codec != self.codec:
            self._f, self.codec, self._dirty = {}, codec, True
        for rel in [r for r in self._f if r not in files]:
            del self._f[rel]
            self._dirty = True
        sz = sizes.get if sizes is not None else (lambda rel, d=-1: -1)
        todo = [(rel, fp) for rel, (fp, mt) in files.items()
                if rel not in self._f or self._f[rel][0] != (mt, sz(rel, -1))]
        if not todo:
            return 0
        if len(todo) < pool_min:
            res = _work((todo, codec, self.other))
        else:
            n = workers or min(16, os.cpu_count() or 4)
            size = max(1, -(-len(todo) // (n * 8)))
            with ProcessPoolExecutor(max_workers=n) as ex:
                res = [x for part in ex.map(_work, ((todo[i:i + size], codec, self.other) for i in range(0, len(todo), size)))
                       for x in part]
        for rel, m, b in res:
            self._f[rel] = ((files[rel][1], sz(rel, -1)), m, b)
        self._dirty = True
        # 少しの変更では書かない(次の起動で、変わったファイルだけ読み直す)。まだ無ければ、小さなリポジトリでも書く
        if len(todo) >= save_min or (self.cache_path and not os.path.exists(self.cache_path)):
            self.save()
        return len(todo)

    def may_contain(self, word, sizes=None):
        """(相対パス, 更新日時) を受けて「word の一致がありうるか」を返す関数。word で絞れなければ None。
        フィルタを作ったときと更新日時(sizes を渡せば大きさも)が違うファイルは、絞らない。"""
        if not IDENT.fullmatch(word):
            return None
        wb = word.encode('ascii')
        masks = {}
        f = self._f

        sz = sizes.get if sizes is not None else (lambda rel, d=-1: -1)

        def test(rel, mtime):
            e = f.get(rel)
            if e is None or e[1] == ALWAYS or e[0] != (mtime, sz(rel, -1)):
                return True
            mask = masks.get(e[1])
            if mask is None:
                mask = 0
                for p in _positions(wb, e[1]):
                    mask |= 1 << p
                masks[e[1]] = mask
            return e[2] & mask == mask
        return test
