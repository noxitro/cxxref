"""ルート配下の C/C++ のソースの一覧を、ファイルの変更の知らせ(Windows の ReadDirectoryChangesW)で保ち続ける。

MCP サーバーのように長く動くプロセスが、問い合わせのたびにソースを全部走査し直さないためのもの
(UE5 の 20 万ファイルで 1 回 8 秒ほど)。最初に 1 回だけ走査し、その後は知らせのあったパスだけを調べ直す。

- 知らせを取りこぼしうるとき(バッファがあふれた・監視が止まった・Windows 以外)は、次の問い合わせで全体を走査し直す
- フォルダの名前の変更・削除は、そのフォルダの下を調べ直す
- ltq.Index.sources() と同じ形({ルートからの相対パス: (絶対パス, 更新日時)})を返す。走査の規則(. で始まるフォルダを
  除く、拡張子で選ぶ)も同じ
- other(拡張子, 大きさ)を渡すと、それが真のファイル(C/C++ 以外の、テキストで探すファイル。ltq.Index.others())も
  同じ規則で保つ(others())
"""
import ctypes
import os
import re
import threading

from ctypes import wintypes

FILE_LIST_DIRECTORY = 0x0001
SHARE_ALL = 0x7  # FILE_SHARE_READ | WRITE | DELETE
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE = wintypes.HANDLE(-1).value
# ファイル名・フォルダ名・大きさ・最終書き込み日時の変更
NOTIFY = 0x1 | 0x2 | 0x8 | 0x10
SHORT_NAME = re.compile(r'(?:^|\\)[^\\~]{1,6}~[0-9][^\\]*(?:\\|$)')  # 8.3 形式の短い名前の部分(PROGRA~1 など)
MARK_PREFIX = os.path.join('.index', '.cxxref-watch-')  # current() が作る印のファイル(ルートからの相対パス)
ACTION_MODIFIED = 3  # FILE_ACTION_MODIFIED(ほかは 1 追加・2 削除・4 改名の前・5 改名の後)
BUF_BYTES = 1 << 20  # ローカルのドライブでは 64 KB を超えてよい【公式: ReadDirectoryChangesW の注記はネットワーク越しのみ 64 KB】


def scan(root, sub, exts, out, other=None, out_other=None):
    """root\\sub の下を走査して out に足す(ltq.Index.sources() と同じ規則)。other を渡すと、拡張子が exts に無く
    other(拡張子, 大きさ)が真のファイルを out_other に足す。"""
    n = len(root) + 1
    todo = [os.path.join(root, sub) if sub else root]
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
                        if ext in exts:
                            out[e.path[n:].replace('\\', '/')] = (e.path, e.stat().st_mtime)
                        elif other is not None:
                            st = e.stat()
                            if other(ext, st.st_size):
                                out_other[e.path[n:].replace('\\', '/')] = (e.path, st.st_mtime)
                except OSError:
                    continue


class SourceTree:
    """root の下のソースの一覧。current() を呼ぶと、前回からの変更を当てた一覧を返す(呼んだスレッドで当てる)。

    知らせは監視のスレッドに少し遅れて届くので、current() は、<root>/.index に印のファイルを作り、その知らせが届くのを
    待ってから当てる(知らせは起きた順に届くので、印より前の変更はすべて届いている)。<root>/.index が無い・書けない・
    待っても届かないときは、全体を走査する。"""

    def __init__(self, root, exts, watch=True, sync_timeout=2.0, other=None):
        self.root = os.path.abspath(root)
        self.exts = exts
        self.other = other
        self._others = {}
        self.sync_timeout = sync_timeout
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending = {}  # パス -> 知らせの種類の集合
        self._overflow = True  # 最初の current() で全体を走査する
        self._files = {}
        self._thread = None
        self._h = None
        self._closed = False
        self._marks = set()  # 届いた印のファイルの名前
        self._seq = 0
        self.watching = False
        self.rescans = 0  # 全体を走査した回数(計測と試験のため)
        self.mark_dir = os.path.join(self.root, '.index')
        if watch and os.name == 'nt' and os.path.isdir(self.mark_dir):
            self._start()

    # --- 監視のスレッド ---
    def _start(self):
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.ReadDirectoryChangesW.restype = wintypes.BOOL
        k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
                                              wintypes.LPVOID]
        k32.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateFileW(self.root, FILE_LIST_DIRECTORY, SHARE_ALL, None, OPEN_EXISTING,
                            FILE_FLAG_BACKUP_SEMANTICS, None)
        if h == INVALID_HANDLE or h is None:
            return
        self._k32, self._h = k32, h
        self.watching = True
        self._thread = threading.Thread(target=self._run, name='srctree-watch', daemon=True)
        self._thread.start()

    def close(self):
        """監視をやめる(ルートのフォルダを開いたままにしない)。以後の current() は呼ぶたびに全体を走査する。"""
        with self._lock:
            self._closed = True
            self.watching = False
            self._overflow = True
            h, self._h = self._h, None
            self._cond.notify_all()
        if h is not None:
            self._k32.CancelIoEx(h, None)  # 止まっている ReadDirectoryChangesW を返させる
            if self._thread is not None:
                self._thread.join(5)
            self._k32.CloseHandle(h)

    def _run(self):
        buf = ctypes.create_string_buffer(BUF_BYTES)
        got = wintypes.DWORD()
        h = self._h
        while True:
            # 同期の呼び出し。ctypes は呼び出しの間 GIL を放すので、問い合わせを止めない
            ok = self._k32.ReadDirectoryChangesW(h, buf, BUF_BYTES, True, NOTIFY, ctypes.byref(got), None, None)
            if not ok or self._closed:
                with self._lock:
                    self._overflow = True
                    self.watching = False  # 以後は呼ばれるたびに全体を走査する
                    self._cond.notify_all()
                return
            if got.value == 0:  # バッファがあふれた: どのパスが変わったか分からない
                with self._lock:
                    self._overflow = True
                    self._cond.notify_all()
                continue
            names, off, raw = [], 0, buf.raw[:got.value]
            while True:
                nxt, action, ln = (int.from_bytes(raw[off + i:off + i + 4], 'little') for i in (0, 4, 8))
                names.append((raw[off + 12:off + 12 + ln].decode('utf-16-le', 'replace'), action))
                if not nxt:
                    break
                off += nxt
            with self._lock:
                for n, act in names:
                    if n.startswith(MARK_PREFIX):
                        self._marks.add(n)
                        continue
                    if SHORT_NAME.search(n):  # 8.3 形式の短い名前で知らされることがある【公式】。長い名前に直せないので全体を走査する
                        self._overflow = True
                    self._pending.setdefault(n, set()).add(act)
                self._cond.notify_all()

    def _sync(self):
        """印のファイルを作り、その知らせが届くまで待つ。届けば True。"""
        self._seq += 1
        name = f'.cxxref-watch-{os.getpid()}-{self._seq}'
        rel = os.path.join('.index', name)
        path = os.path.join(self.root, rel)
        try:
            with open(path, 'wb'):
                pass
        except OSError:
            return False
        try:
            with self._lock:
                self._cond.wait_for(lambda: rel in self._marks or not self.watching, self.sync_timeout)
                return rel in self._marks
        finally:
            with self._lock:
                self._marks.discard(rel)
            try:
                os.remove(path)
            except OSError:
                pass

    # --- 一覧 ---
    def current(self):
        synced = self.watching and self._sync()
        with self._lock:
            pending, self._pending = self._pending, {}
            overflow, self._overflow = self._overflow or not synced, not self.watching
            self._marks.clear()  # 前の印を消した知らせなど
        if overflow:
            files, others = {}, {}
            scan(self.root, '', self.exts, files, self.other, others)
            self._files, self._others = files, others
            self.rescans += 1
            return self._files
        for p, acts in pending.items():
            self._apply(p, acts)
        return self._files

    def others(self):
        """C/C++ 以外のファイルの一覧(other を渡したときだけ)。current() の後に呼ぶ(current() が知らせを当てる)。"""
        return self._others

    def _apply(self, name, acts):
        rel = name.replace('\\', '/').strip('/')
        parts = rel.split('/')
        if any(d.startswith('.') for d in parts[:-1]) or not rel:
            return  # 走査で除くフォルダの下
        fp = os.path.join(self.root, name)
        try:
            st = os.stat(fp, follow_symlinks=False)
        except OSError:
            st = None
        if st is not None and os.path.isdir(fp) and not os.path.islink(fp):
            if parts[-1].startswith('.') or acts <= {ACTION_MODIFIED}:
                return  # 中身が変わっただけのフォルダ。中のファイルの知らせは別に来る
            # 名前を変えて来たフォルダ・作られたフォルダ: 下を調べ直す(中のファイルの知らせは来ないことがある)
            self._drop_under(rel)
            scan(self.root, name, self.exts, self._files, self.other, self._others)
            return
        if st is None:  # 消えた・名前を変えて去った(ファイルかフォルダか、もう分からない)
            self._files.pop(rel, None)
            self._others.pop(rel, None)
            self._drop_under(rel)
            return
        ext = os.path.splitext(parts[-1])[1].lower()
        if ext in self.exts:
            try:
                self._files[rel] = (fp, os.stat(fp).st_mtime)
            except OSError:
                self._files.pop(rel, None)
        elif self.other is not None:
            if self.other(ext, st.st_size):
                self._others[rel] = (fp, st.st_mtime)
            else:
                self._others.pop(rel, None)  # 大きくなりすぎた

    def _drop_under(self, rel):
        pre = rel + '/'
        low = pre.lower()
        for d in (self._files, self._others):
            for k in [k for k in d if k.lower().startswith(low)]:
                del d[k]
