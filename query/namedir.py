"""索引(TSV)の「名前ごとの目次」: 記号の最後の部分(`A::B::f` の `f`)から、その行の位置を引く。

ltq の refs は、索引の本体を mmap して `名前<TAB>` を頭から探す(UE5 の 10 GB で 1 回 8 秒ほど)。目次があれば、
名前のハッシュで行の位置だけを引いて、その行だけを読む。

- 目次は <索引>.names.cache。索引の大きさと更新日時が同じ間だけ使う。作るのは 1 回で、プロセスを分けて読む
- 1 つの項目は 64 ビット: 上位 28 ビットが名前の CRC32 の下位 28 ビット、下位 36 ビットが行の先頭の位置(64 GB まで)
- 区間ごとに並べた列(区間の数だけ)をそのまま書く。引くときは列ごとに二分探索する
- ハッシュの衝突で別の名前の行も返るので、呼ぶ側が行の中身で確かめる(ltq は記号の列を見て捨てる)
"""
import bisect
import mmap
import os
import re
import struct
import zlib
from array import array
from concurrent.futures import ProcessPoolExecutor

MAGIC = b'cxxref-names 1\n'
OFF_BITS = 36
OFF_MASK = (1 << OFF_BITS) - 1
HASH_MASK = (1 << 28) - 1
SYM = re.compile(rb'(?m)^([^\t\n]*)\t')
STEP = 64 << 20


def short_of(sym):
    """記号の列の最後の部分(ltq の `sym.rsplit('::', 1)[-1]` と同じ)。"""
    i = sym.rfind(b'::')
    return sym if i < 0 else sym[i + 2:]


def key_of(short):
    return (zlib.crc32(short) & HASH_MASK) << OFF_BITS


def _work(args):
    """索引の [s, e) の区間にある行の、並べた項目の列(bytes)。"""
    path, s, e = args
    out = array('Q')
    with open(path, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        size = len(m)
        if s > 0:  # 区間の最初の行の先頭へ(前の区間の最後の行は、前の区間が読む)
            s = m.find(b'\n', s - 1) + 1 or size
        e = size if e >= size else (m.find(b'\n', e - 1) + 1 or size)
        pos = s
        while pos < e:
            stop = min(e, pos + STEP)
            if stop < e:
                stop = m.find(b'\n', stop - 1) + 1 or e
            chunk = m[pos:stop]
            for mt in SYM.finditer(chunk):
                out.append(key_of(short_of(mt.group(1))) | (pos + mt.start()))
            pos = stop
    return array('Q', sorted(out)).tobytes()


class NameDir:
    def __init__(self, index_path):
        self.index_path = index_path
        self.cache_path = index_path + '.names.cache'
        self._runs = None
        self._mv = None
        self._mm = None
        self._made = None  # 目次を作ったときの索引の鍵

    def _key(self):
        st = os.stat(self.index_path)
        return MAGIC + f'{st.st_size} {st.st_mtime_ns}\n'.encode()

    def ready(self):
        return self._runs is not None

    def current(self):
        """開いた目次が、今の索引のものか(索引の大きさと更新日時で見る)。"""
        try:
            return self.ready() and self._made == self._key()
        except OSError:
            return False

    def open(self):
        """目次を開く。無いか古ければ False。"""
        try:
            key = self._key()
            f = open(self.cache_path, 'rb')
        except OSError:
            return False
        with f:
            head = f.read(len(key) + 4)
            if not head.startswith(key):
                return False
            try:  # 書きかけ・壊れた目次は「無い」として作り直させる(例外にすると、その版の索引では二度と使われない)
                (n,) = struct.unpack('<I', head[len(key):])
                lens = struct.unpack(f'<{n}Q', f.read(8 * n))
                if len(key) + 4 + 8 * n + 8 * sum(lens) > os.fstat(f.fileno()).st_size:
                    return False
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            except (struct.error, ValueError, OverflowError, OSError):
                return False
        base = len(key) + 4 + 8 * n
        mv = memoryview(mm)
        runs = []
        for ln in lens:
            runs.append(mv[base:base + 8 * ln].cast('Q'))
            base += 8 * ln
        self._mm, self._mv, self._runs, self._made = mm, mv, runs, key
        return True

    def build(self, workers=None):
        """目次を作って書き、開く。書けなければメモリに持つだけにする。"""
        size = os.path.getsize(self.index_path)
        if size > OFF_MASK:
            return False
        self.close()
        key = self._key()
        n = workers or min(16, os.cpu_count() or 4)
        step = -(-size // n) or 1
        with ProcessPoolExecutor(max_workers=n) as ex:
            runs = list(ex.map(_work, [(self.index_path, i, min(size, i + step)) for i in range(0, size, step)]))
        tmp = f'{self.cache_path}.{os.getpid()}.tmp'
        try:
            with open(tmp, 'wb') as f:
                f.write(key + struct.pack('<I', len(runs)) + struct.pack(f'<{len(runs)}Q', *(len(r) // 8 for r in runs)))
                for r in runs:
                    f.write(r)
            os.replace(tmp, self.cache_path)
            if self.open():
                return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self._runs, self._made = [memoryview(r).cast('Q') for r in runs], key
        return True

    def offsets(self, short):
        """名前(bytes)のハッシュに当たる行の先頭の位置。索引の中の順に並べる。"""
        lo = key_of(short)
        hi = lo + (1 << OFF_BITS)
        out = []
        for r in self._runs:
            i = bisect.bisect_left(r, lo)
            j = bisect.bisect_left(r, hi, i)
            out.extend(v & OFF_MASK for v in r[i:j])
        out.sort()
        return out

    def close(self):
        """目次の写しを閉じる(Windows では、開いたままだと作り直した目次で置き換えられない)。"""
        for r in self._runs or ():
            r.release()
        self._runs = None
        if self._mv is not None:
            self._mv.release()
            self._mv = None
        if self._mm is not None:
            self._mm.close()
            self._mm = None
