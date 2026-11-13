"""cxxref の MCP サーバー(stdio)。ltq の refs / check をツールとして公開する。

  python -I server.py [--root <ソースのルート>] [--index <索引.tsv>] [--auto-update-wait 秒 | --no-auto-update]

索引の後にソースが変わっていたら、裏で作り直して最大 --auto-update-wait 秒(既定 10)待つ(autoupdate.py)。
終わらなければ今の索引で答え、次の呼び出しから新しい索引を使う。

標準ライブラリだけで、MCP の stdio トランスポート(1 行 1 メッセージの JSON-RPC 2.0)を話す。
標準出力はプロトコル専用なので、ログは標準エラーに出す。
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'query'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I ではスクリプトのフォルダが入らない
import hashlib  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

import ltq  # noqa: E402
import autoupdate  # noqa: E402
import namedir  # noqa: E402
import srctree  # noqa: E402
import wordfilter  # noqa: E402

VERSION = '0.5.7'
PROTOCOL = '2025-06-18'
# 結果の大きさの上限の既定。Copilot CLI は 20,480 バイト(COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES)を超える
# ツールの結果をモデルに渡さず、ファイルに退かせて先頭だけを見せる【実測: 1.0.18 の app.js。1.0.105 は native に移り、
# 計測で 21.2 KB 以上が退かされ、18,000 バイト以下は退かされなかった】。少し下回らせる
MAX_BYTES = 18000
FIT_LIMITS = (60, 25, 10, 5, 2)  # 短い形でも上限を超えるときに、順に試す --limit(1 節に並べる行数)
FIT_FAMILY = (5, 2)  # 短い形でも上限を超えるときに、--limit の前に試す --family(並べる上書きの数、上書きへの呼び出しの数)
# 結果に 1 行ずつ並べきれなかったとき(上限に収めるために件数にまとめた行があるとき)は、結果に並べなかった行を 1 行ずつ並べた
# 一覧(残りの一覧。結果と合わせると、名前を含む行がどれも 1 回ずつ出る)をファイルに書き、結果の最後の行でその場所を知らせる。
# 名前やシグネチャを変えるときに、直す先を漏らさず、ソースを検索し直させないため
# 一覧は、エージェントのファイルを読む道具が 1 回で読める大きさのファイルに分ける。Copilot CLI の view は、20,460 バイトを
# 読み、24,555 バイトを「File too large to read at once」と断る【実測: 1.0.109】。断られると、エージェントは一覧を捨てて
# ソースを grep し直す(UE5 の FAnimNode_Base::Update_AnyThread で、一覧 37 KB が読めず、入力が cxxref なしより増えた)
PART_BYTES = 18000
PARTS_MAX = 3  # 問い合わせの記号の行(索引が結び付けた行)を分けるファイルの数。超えた分は、1 つの大きなファイルに回す
HEAD_ROOM = 120  # 分けたファイルの先頭に足す 1 行の分
LIST_DIR = 'refs'  # 一覧を置くフォルダの名前(索引のあるフォルダか、LIST_HOME の下)
LIST_HOME = '.cxxref'  # 索引がルートの隠しフォルダに無いときに、一覧を置くルートの下のフォルダ
LIST_KEEP = 7 * 86400  # 一覧のファイルを残す秒数。これより古いものは、次に書くときに消す
LIST_MAX = 200  # 1 つのフォルダに残す一覧の数。超えた分は、古いものから消す(1 つが数 MB になる名前がある)
LIST_NAME = re.compile(r'-[0-9a-f]{8}(\.\d+|\.more)?\.txt(\.\d+\.tmp)?$')

# 定義は、モデルを呼ぶたびに毎回送られる(C++ でないリポジトリでも)。言っていることは変えずに短く保つ
ROOT_ARG = {'type': 'string', 'description': 'Absolute path of your working folder (the server may run elsewhere)'}
INSTRUCTIONS = (
    'Clang-built C++ reference index. For callers, references or definitions of a C++ symbol, call `refs` before '
    'grepping; its `calls` section is the answer as is. Use `check` for lines found by other searches.')

TOOLS = [
    {
        'name': 'refs',
        'title': 'C++ references',
        'description': (
            'Every call site, reference, definition and declaration of a C++ symbol, resolved by clang over all '
            'translation units (template instantiations included). Use it instead of grep for C++ symbols. Sections: '
            'calls (with the enclosing function; no need to re-check them with grep), other references, definitions, '
            'virtual overrides (where each is declared and defined, and the calls to them; each virtual call is marked '
            '[dynamic] (no qualifier: the override of the receiver\'s runtime type runs), [static] (Base::f(): that '
            'function itself runs) or [super] (Super::f() inside an override, calling up)), unresolved template calls '
            '(decide from the line), and text matches of the name that '
            'are not in the index, with the reason (comment, inactive #if, dropped macro argument, local name, macro, '
            'string, file not analyzed). Code in #if branches the build does not compile is included, marked '
            '[inactive: #if ...]. Matches in non-C/C++ files (scripts, build files, docs) are listed too. Lines that do '
            'not fit are counted by folder; the last line then names files that list them, one per line (before a '
            'rename or a signature change, read those instead of grepping again), or pass path to list a folder. '
            'Macros (#define and expansions) are symbols too.'),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': 'Qualified name or its suffix, e.g. "ns::File::Read" or "File::Read"'},
                'limit': {'type': 'integer', 'description': 'Max lines per section (default 200)'},
                'exclude': {'type': 'array', 'items': {'type': 'string'},
                            'description': 'Folders or root-relative path prefixes to skip in the text search'},
                'path': {'type': 'array', 'items': {'type': 'string'},
                         'description': 'List only lines under these root-relative folders or files'},
                'root': ROOT_ARG,
            },
            'required': ['name'],
        },
        'annotations': {'readOnlyHint': True, 'openWorldHint': False},
    },
    {
        'name': 'check',
        'title': 'C++ index check',
        'description': (
            'For each path:line[:col], what the clang index has on that line, and whether it is in a comment or an '
            'inactive #if. Use it on lines found by grep before reporting them.'),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'locations': {'type': 'array', 'items': {'type': 'string'},
                              'description': 'Root-relative "path:line" or "path:line:col"'},
                'root': ROOT_ARG,
            },
            'required': ['locations'],
        },
        'annotations': {'readOnlyHint': True, 'openWorldHint': False},
    },
]


def yaml_scalar(v):
    """1 行の YAML のスカラー。'...' は '' を ' に、"..." は \\ の後の文字をそのまま、引用符の無いものはそのまま
    (パスに ' があると単一引用符で書かれる)。"""
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if len(v) >= 2 and v[0] == v[-1] == '"':
        return re.sub(r'\\(.)', r'\1', v[1:-1])
    return v


def session_cwd():
    """Copilot CLI のセッションの作業フォルダ。COPILOT_AGENT_SESSION_ID のセッションの記録
    (<COPILOT_HOME か ~/.copilot>/session-state/<id>/workspace.yaml の cwd)から読む。公開の仕様ではない
    【実測: Copilot CLI 1.0.105。セッションの始めに書かれ、MCP サーバーの起動より先にある】。読めなければ None。"""
    sid = os.environ.get('COPILOT_AGENT_SESSION_ID', '')
    if not re.fullmatch(r'[0-9A-Za-z-]+', sid):
        return None
    home = os.environ.get('COPILOT_HOME') or os.path.join(os.path.expanduser('~'), '.copilot')
    try:
        with open(os.path.join(home, 'session-state', sid, 'workspace.yaml'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('cwd:'):
                    v = yaml_scalar(line[4:].strip())
                    return v if os.path.isdir(v) else None
    except OSError:
        pass
    return None


def workspace():
    """ツールの引数で root が渡されなかったときの起点。プラグインとして起動されると、作業フォルダはプラグインの場所
    (PLUGIN_ROOT)になり、MCP の roots も空なので【実測: Copilot CLI 1.0.105】、セッションの記録から取る。"""
    cwd = os.getcwd()
    plugin = os.environ.get('PLUGIN_ROOT')
    if plugin and os.path.normcase(os.path.abspath(plugin)) == os.path.normcase(cwd):
        return session_cwd() or cwd
    return cwd


class Config:
    def __init__(self, root=None, index=None, wait=None, max_bytes=MAX_BYTES, resident=True, part_bytes=PART_BYTES):
        self.fixed = os.path.abspath(root) if root else None  # None なら、呼ばれるたびに索引のあるフォルダを探す
        self.index = os.path.abspath(index) if index else None
        self.wait = wait  # 自動更新で待つ秒数。None なら自動更新しない
        self.max_bytes = max_bytes  # 結果の大きさの上限(0 なら切らない)
        self.opened = None  # (鍵, 索引のファイルの状態, ltq.Index)。最後に引いた索引を 1 つだけ保つ
        self.told_ltidx = False  # 取ってきた ltidx が古いことを、もう知らせたか(1 回だけ)
        self.part_bytes = part_bytes  # 一覧のファイル 1 つの大きさの上限(エージェントが 1 回で読める大きさ)
        self.last_list = None  # 最後の呼び出しで書いた一覧のファイル(分けたときは 1 つ目。書かなかったら None)
        self.last_lists = []  # 同じく、書いたファイルの全部
        # 呼び出しをまたいで保つもの(False なら、呼ぶたびにソースと索引を全部読む)
        self.resident = resident
        self._trees = {}  # ルート -> (srctree.SourceTree, wordfilter.WordFilter)
        self._names = {}  # 索引のパス -> namedir.NameDir
        self._no_names = set()  # 目次を作れなかった索引の版

    def attach(self, idx, paths, root):
        """Index に、呼び出しをまたいで保つ部品を渡す。
        - ソースの一覧(変更の知らせで保つ。ソースの走査を毎回しない)
        - 単語のフィルタ(テキスト検索で、名前を含まないファイルを読まない)
        - 名前の目次(索引の本体を頭から探さない。索引が変わったら作り直す)"""
        if not self.resident:
            return idx
        key = os.path.normcase(os.path.abspath(root))
        if key not in self._trees:
            words = wordfilter.WordFilter(paths[0] + '.words.cache')
            words.load(idx.codec())
            other_words = wordfilter.WordFilter(paths[0] + '.words-other.cache', other=True)  # C/C++ 以外のファイル
            other_words.load(idx.codec())
            self._trees[key] = (srctree.SourceTree(root, ltq.SRC_EXT, other=ltq.is_other), words, other_words)
        idx.tree, idx.words, idx.other_words = self._trees[key]
        for p in paths:
            k = os.path.normcase(os.path.abspath(p))
            nd = self._names.get(k)
            try:
                version = (k, os.stat(p).st_size, os.stat(p).st_mtime_ns)
            except OSError:
                continue
            if version in self._no_names:
                continue  # この版の索引では目次を作れなかった。呼ぶたびに作り直さない
            if nd is None or not nd.current():
                if nd is not None:
                    nd.close()
                nd = namedir.NameDir(p)
                try:
                    if not nd.open():
                        nd.build()
                except Exception as e:  # 子のプロセスが落ちた・書けないなど。目次なしで、索引を頭から探す
                    print(f'code-ref-index: name directory unavailable for {p} ({type(e).__name__}: {e})', file=sys.stderr)
                    nd.close()
                    self._names.pop(k, None)
                    self._no_names.add(version)
                    continue
                self._names[k] = nd
            idx.names[p] = nd
        return idx

    def close(self):
        """保っている部品を手放す(監視しているフォルダと、目次の写しを開いたままにしない)。"""
        for tree, *_ in self._trees.values():
            tree.close()
        for nd in self._names.values():
            nd.close()
        self._trees, self._names = {}, {}

    def open_index(self, paths, root):
        """索引を開く。前の呼び出しと同じ索引で、索引のファイル(本体と隣のファイル)の大きさと更新日時が変わっていなければ、
        前に開いた Index を使い回す(TU の記録と、TU が読んだファイルの一覧を読み直さない。UE5 で 1 回 30 秒ほど)。
        鮮度の確認は、呼ぶたびにやり直す(ソースの一覧は attach() が渡す SourceTree が変更の知らせで保つ)。"""
        key = (tuple(os.path.normcase(os.path.abspath(p)) for p in paths), os.path.normcase(os.path.abspath(root)))
        sig = ltq.index_signature(paths)
        if self.opened and self.opened[0] == key and self.opened[1] == sig:
            idx = self.opened[2]
            idx.renew()
            return self.attach(idx, paths, root)
        idx = ltq.Index(paths, root)
        self.opened = (key, sig, idx)
        return self.attach(idx, paths, root)

    def keep(self, paths, root, idx):
        """自動更新で開き直した索引を、次の呼び出しで使い回せるように保つ。"""
        key = (tuple(os.path.normcase(os.path.abspath(p)) for p in paths), os.path.normcase(os.path.abspath(root)))
        self.opened = (key, ltq.index_signature(paths), self.attach(idx, paths, root))

    def resolve(self, start=None):
        """(ルート, 注記)。--root が無ければ、start(ツールの引数の root)か作業フォルダ(workspace())から
        親へさかのぼって索引のあるフォルダを探す。呼ばれるたびに探すので、サーバーの起動後に作った索引も拾う。"""
        # 相対の root は、サーバーの作業フォルダ(プラグインならプラグインの場所)ではなく、作業中のフォルダから解決する
        cwd = os.path.normpath(os.path.join(workspace(), start)) if start else workspace()
        root = self.fixed or (None if self.index else ltq.find_root(cwd)) or cwd
        note = ''
        if not self.fixed and os.path.normcase(root) != os.path.normcase(cwd):
            note = f'root: {root} (the index is in this parent folder; paths below are relative to it)\n'
        return root, note


def list_dirs(paths, root):
    """一覧を置くフォルダの候補を、試す順に [(フォルダ, git に無視させるフォルダか None)] で返す。
    エージェントが読める所(作業ツリーの中)で、テキスト検索に掛からない所(. で始まるフォルダの中。一覧が次の検索に
    当たらない)に置く: 索引がルートの下の隠しフォルダ(既定の .index)にあればその隣、無ければ(索引がルートの外や、
    検索に掛かるフォルダにある)<ルート>/.cxxref/refs。どちらにも書けないときだけ、一時フォルダ(作業ツリーの外)。"""
    index_dir = os.path.dirname(os.path.abspath(paths[0]))
    home = os.path.join(root, LIST_HOME)
    return (([(os.path.join(index_dir, LIST_DIR), None)] if ltq.tree_place(index_dir, root) == 'hidden' else [])
            + [(os.path.join(home, LIST_DIR), home), (os.path.join(tempfile.gettempdir(), 'cxxref-' + LIST_DIR), None)])


def same_text(path, text):
    try:
        with open(path, encoding='utf-8', newline='') as f:
            return f.read() == text
    except (OSError, UnicodeDecodeError):
        return False


def nbytes(text):
    return len(text.encode('utf-8'))


def split_lines(lines, size):
    """行の並びを、行の境目で size バイト以下の部分に分ける。[(頭に足す行, 始まり, 終わり)](始まりと終わりは lines の添え字)。
    節の途中で分けたら、次の部分の頭に、節の見出し(と、小見出しのある節ではその小見出し)を「(continued)」を付けて足す
    (分けたファイルを 1 つだけ読んでも、その行が何の行かが分かる)。1 行が size を超えるときは、その行だけの部分になる。"""
    parts, pre, start, used = [], [], 0, 0
    head = sub = None
    for i, line in enumerate(lines):
        n = nbytes(line)
        is_head = line.startswith('== ')
        is_sub = (not is_head and head is not None and head.startswith(ltq.NESTED)
                  and line.startswith('  ') and not line.startswith(('   ', '  (')))
        if i > start and used + n > size and line.strip():  # 空行では分けない(次の部分を空行で始めない)
            parts.append((pre, start, i))
            pre = [] if is_head else [x.rstrip('\n') + ' (continued)\n' for x in ((head,) if is_sub else (head, sub)) if x]
            start, used = i, sum(map(nbytes, pre))
        if is_head:
            head, sub = line, None
        elif is_sub:
            sub = line
        used += n
    if start < len(lines):
        parts.append((pre, start, len(lines)))
    return parts


def plan_list(text, size):
    """一覧の文面を、書くファイルに分ける。[(ファイル名の添え字, 文面, 1 回で読める大きさか)] と、大きなファイルが
    名前を含むだけの行(ltq.WORD_ONLY の節)だけか。
    - 全体が size に収まれば、1 つ
    - 収まらなければ、問い合わせの記号の行(索引が結び付けた行: 呼び出し・定義・上書きとその呼び出し・テンプレートの中)を
      size 以下のファイルに分ける(PARTS_MAX 個まで)。名前を含むだけの行(索引に無いテキストの一致、同じ名前の別の記号の行)は、
      size に収まれば 1 つの小さなファイル、収まらなければ 1 つの大きなファイル(ありふれた名前では数 MB。読ませず、検索させる)"""
    if nbytes(text) <= size:
        return [('', text, True)], False
    lines = text.splitlines(keepends=True)
    first = next((i for i, x in enumerate(lines) if x.startswith('== ')), len(lines))
    cut = next((i for i, x in enumerate(lines) if x.startswith(ltq.WORD_ONLY)), len(lines))
    intro, word = lines[:first], lines[cut:]
    if cut == first:  # 問い合わせの記号の行が無い。全体が、名前を含むだけの行
        return [('', text, False)], True
    own = lines[:cut]
    room = size - HEAD_ROOM
    parts = split_lines(own, room)
    small = [''.join(pre + own[i:j]) for pre, i, j in parts[:PARTS_MAX]]
    big, word_only = '', True
    if len(parts) > PARTS_MAX:  # 短い名前で、問い合わせの記号の行だけで数百 KB ある。残りは大きなファイルへ
        pre, i, _ = parts[PARTS_MAX]
        big, word_only = ''.join(intro + pre + own[i:]), False
    if word:
        if big:
            big += ''.join(word)
        elif nbytes(''.join(intro + word)) <= room:
            small.append(''.join(intro + word))
        else:
            big = ''.join(intro + word)
    files = [(f'.{k + 1}', t, True) for k, t in enumerate(small)] + ([('.more', big, False)] if big else [])
    n = len(files)
    return [(sfx, f'file {k + 1} of {n} of this list (the refs result names all of them)\n' + t.rstrip('\n') + '\n', ok)
            for k, (sfx, t, ok) in enumerate(files)], word_only


def list_size(n):
    return f'{n / 1048576:.1f} MB' if n >= 1048576 else f'{max(1, round(n / 1024))} KB'


def list_hint(files, word_only, left, far):
    """結果の最後の行(coverage)に足す案内。files は [(パス, バイト数, 1 回で読める大きさか)]、left は結果に並べなかった行の数。
    小さいファイルは「丸ごと読む」、大きいファイルは「検索する」と言い分ける(大きいファイルを読もうとして断られると、
    エージェントはソースを検索し直す)。"""
    small = [f for f in files if f[2]]
    large = [f for f in files if not f[2]]
    big = (f'"{large[0][0]}" ({list_size(large[0][1])}: too large to read whole; search it with grep, or pass path to list'
           ' one folder)') if large else ''
    what = 'lines the index does not resolve to the query (text matches, other symbols with the same name)'
    out = f'; the other {left} lines are listed, one per line, in '
    if not small:
        out = f'; the other {left} ' + (what if word_only else 'lines') + ' are listed, one per line, in ' + big
    else:
        if len(small) == 1:
            out += f'"{small[0][0]}" ({list_size(small[0][1])}: read it whole in one call)'
        else:
            out += (f'{len(small)} files to read whole, one call each, in parallel: '
                    + ', '.join(f'"{p}"' for p, _, _ in small))
        if large:
            out += ', and the ' + (what if word_only else 'remaining lines') + ' in ' + big
        # 大きなファイルがあるときは、読むのは小さいファイルだけだと分かるように言う
        which = ('it' if len(files) == 1 else 'them' if not large
                 else 'the first one' if len(small) == 1 else f'the first {len(small)}')
        out += f'. Before a rename or a signature change, read {which} instead of searching the sources again'
    if far:  # 一時フォルダに書いたとき(作業ツリーに書けなかった)は、エージェントが読めないことがあるので、別の手を添える
        out += ' (outside the workspace: if you cannot read ' + ('it' if len(files) == 1 else 'them') \
               + ', pass path to list one folder at a time)'
    return out


def write_list(paths, root, args, text):
    """一覧をファイル 1 つに書く。(パス, None) か (None, 誤り)。"""
    where, err = write_lists(paths, root, args, [('', text)])
    return (where[0] if where else None), err


def write_lists(paths, root, args, texts, shown=None):
    """一覧(ltq refs --all の文面か、その残り)をファイルに書く。texts は [(ファイル名の添え字, 文面)]。shown は、残りの一覧の
    とき、結果が 1 行ずつ並べた位置((パス, 行) の集合)。
    ([パス], None) か (None, 誤り)。置く所は list_dirs(全部を同じフォルダに置く)。
    ファイル名は「名前-ルート・索引・引数・shown のハッシュ」+ 添え字。同じ結果なら同じファイルを書き換えるので、増え続けない。
    結果の形が違えば(並べた行が違えば)別のファイルになる: 同じサーバーを使う別のエージェントが同じ名前を別の形で受け取っても、
    先に受け取った側の一覧を書き換えない(書き換えると、結果と一覧を合わせても全部の行にならない)
    (前に書いた同じ問い合わせのファイルで、今回書かなかったもの(分け方が変わった)は消す)。
    中身が前と同じなら書き直さない(更新日時だけ新しくする。読んでいる相手のファイルを差し替えない)。"""
    key = [os.path.normcase(os.path.abspath(root))] + [os.path.normcase(os.path.abspath(p)) for p in paths]
    skip = False
    for x in args[1:]:  # --limit・--other-limit は一覧に効かない(同じ一覧になる)ので、鍵に入れない
        if skip or x in ('--limit', '--other-limit'):
            skip = not skip
            continue
        key.append(x)
    if shown is not None:
        key.append(hashlib.sha1(chr(10).join(f'{p}:{n}' for p, n in sorted(shown)).encode('utf-8')).hexdigest())
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', args[1]).strip('_.')[:60] or 'name'
    stem = f'{safe}-{hashlib.sha1(chr(0).join(key).encode("utf-8")).hexdigest()[:8]}'
    err = None
    for d, home in list_dirs(paths, root):
        tmp, done = None, []
        try:
            os.makedirs(d, exist_ok=True)
            ignore = home and os.path.join(home, '.gitignore')
            if ignore and not os.path.exists(ignore):  # 作業ツリーに足したフォルダを、git の変更に見せない
                with open(ignore, 'w', encoding='utf-8', newline='\n') as f:
                    f.write('*\n')
            for suffix, text in texts:
                dst = os.path.join(d, f'{stem}{suffix}.txt')
                tmp = f'{dst}.{os.getpid()}.tmp'
                if same_text(dst, text):
                    os.utime(dst)
                else:
                    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                        f.write(text)
                    os.replace(tmp, dst)  # 読んでいる途中の相手に、書きかけを見せない
                done.append(dst)
        except OSError as e:
            err = e
            if tmp:
                with contextlib.suppress(OSError):
                    os.remove(tmp)
            continue
        old, found = time.time() - LIST_KEEP, []
        with contextlib.suppress(OSError):
            found = sorted(((e.stat().st_mtime, e.path) for e in os.scandir(d) if LIST_NAME.search(e.name)), reverse=True)
        keep = {os.path.normcase(p) for p in done}
        for i, (mtime, p) in enumerate(found):
            if os.path.normcase(p) in keep:
                continue
            # 古いもの、数の上限を超えた分(新しい順に LIST_MAX 個を残す)、同じ問い合わせの前の分け方のファイルを消す
            name = os.path.basename(p)
            stale = name.startswith(stem + '.') and name.endswith('.txt')
            if mtime < old or i >= LIST_MAX or stale:
                with contextlib.suppress(OSError):
                    os.remove(p)
        return done, None
    return None, err


def run_ltq(cfg, args, start=None):
    idx = None
    root, note = cfg.resolve(start)
    base = ['--root', root] + (['--index', cfg.index] if cfg.index else [])
    paths = [cfg.index] if cfg.index else ltq.find_indexes(root)
    if paths and all(os.path.exists(p) for p in paths):
        idx = cfg.open_index(paths, root)
        if cfg.wait is not None:
            new, up = autoupdate.refresh(idx, paths, root, cfg.wait)
            if new is not idx:
                cfg.keep(paths, root, new)
            idx = new
            note += up
        if not cfg.told_ltidx:
            cfg.told_ltidx = True
            old = autoupdate.stale_ltidx()
            if old:
                get = os.path.join(autoupdate.SCRIPTS, 'get-ltidx.bat')
                note += (f'ltidx {old[0]} is older than this plugin ({old[1]}): the index lacks newer kinds of rows. '
                         'Ask the user to run /cxxref-index in Copilot CLI (it fetches the new ltidx and rebuilds the index), '
                         f'or "{get}" and then rebuild the index.\n')
    elif not cfg.index:
        plugin = os.environ.get('PLUGIN_ROOT')
        if not start and plugin and os.path.normcase(root) == os.path.normcase(os.path.abspath(plugin)):
            # 作業中のフォルダが分からず、プラグインの場所を見ている。プラグイン自身の索引づくりを勧めない
            note += ('The server could not tell which folder you are working in (it runs in the plugin folder). '
                     'Call again with `root` set to the absolute path of your workspace.\n')
        else:
            # 索引の無いリポジトリ。プラグインとして入れたときは scripts が見つけにくいので、作り方を絶対パスで添える
            index_project = os.path.join(autoupdate.SCRIPTS, 'index-project.bat')
            note += ('No cxxref index in this repository; search with rg as usual. To build one (it takes minutes; ask '
                     f'the user first): "{index_project}" -Root "{root}"'
                     + ('' if autoupdate.find_ltidx() else
                        f' (first run "{os.path.join(autoupdate.SCRIPTS, "get-ltidx.bat")}" once to get ltidx.exe)')
                     + '\n')

    def run(extra, noted=True, skip=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                # idx が None なら ltq が探して、無ければ誤りを書く
                code = ltq.main(base + args + extra, idx=idx, skip=skip)
            except SystemExit as e:  # argparse の誤り
                code = e.code or 2
        return code, (note if noted else '') + out.getvalue()

    def over(t):
        return bool(cfg.max_bytes) and nbytes(t) > cfg.max_bytes

    def write(list_text, shown=None):
        """一覧の文面を、1 回で読める大きさのファイルに分けて書く。([(パス, バイト数, 1 回で読めるか)], 大きなファイルが
        名前を含むだけの行か, 誤り)。"""
        plan, word_only = plan_list(list_text, cfg.part_bytes)
        where, err = write_lists(paths, root, args, [(sfx, t) for sfx, t, _ in plan], shown)
        cfg.last_lists = where or []
        cfg.last_list = where[0] if where else None
        return [(p, nbytes(t), ok) for p, (_, t, ok) in zip(where or [], plan)], word_only, err

    def with_rest(t):
        """t(並べきれなかった行がある結果)に並べなかった行を一覧のファイルに書き、最後の行(coverage)の案内を
        その場所に置き換えたもの。"""
        shown, n_all, n_listed = idx.last_refs  # この結果が 1 行ずつ並べた位置と、名前を含む行の数
        # 一覧のファイルには、サーバーの注記を入れない(最初の 1 回だけ付く注記があり、同じ問い合わせでも中身が変わる)
        rest_code, rest_text = run(['--all'], noted=False, skip=shown)
        files, word_only, err = write(rest_text, shown) if rest_code == 0 else ([], False, 'listing the rest failed')
        if files:
            far = ltq.tree_place(os.path.dirname(files[0][0]), root) == 'out'
            hint = list_hint(files, word_only, n_all - n_listed, far)
        else:
            hint = f'; the rest are only counted (the list of the rest could not be written: {err}; pass path to list a folder)'
        return t[:-len(ltq.ALL_HINT) - 1] + hint + '\n'

    def cut(t, files=()):
        """t の末尾を上限で切り、切ったことを最後の行に書いたもの。files は、名前を含む行を全部並べた一覧のファイル
        [(パス, バイト数, 1 回で読めるか)]。"""
        small = [f'"{p}"' for p, _, ok in files if ok]
        large = [f'"{p}" ({list_size(n)}: search it with grep)' for p, n, ok in files if not ok]
        tail = (f'... output cut at {cfg.max_bytes} bytes (the client limit). '
                + ('Every line, one per line: ' + ', '.join(small + large)
                   + (' (read ' + ('it' if len(small) == 1 else 'each') + ' whole in one call)' if small and not large else '')
                   + '. ' if files else '')
                + 'Ask for a narrower name, or pass path / exclude / limit to see the rest.\n')
        head = t.encode('utf-8')[:max(0, cfg.max_bytes - nbytes(tail))].decode('utf-8', 'ignore')
        return head[:head.rfind('\n') + 1] + tail

    cfg.last_list, cfg.last_lists = None, []
    code, text = run([])
    if args[0] != 'refs' or code != 0 or idx is None:
        # 短い形があるのは refs だけ。ほかのコマンド(check に場所を数百渡したときなど)は、上限を超えたら末尾を切る
        return code, cut(text) if over(text) else text
    # クライアントは大きな結果をモデルに渡さない(Copilot CLI は 20,480 バイトを超えるとファイルに退かせ、先頭だけを見せる)。
    # 超えたら、短い形で出し直す。順に:
    # - 上書きの族(上書きと、上書きへの呼び出し)だけを 10 行ずつに絞る(他の節は全文のまま。大きな階層の virtual で効く)
    # - 報告しない節(コメント・文字列など)を件数にまとめる(--compact)
    # - 無効な #if の中やマクロの定義の中の一致もフォルダごとの件数にする(どのフォルダに何件あるかは残り、path で出し直せる)
    # - 上書きの族をさらに絞る(5、2。直接の呼び出しを減らす前に)
    # - 索引が問い合わせに解決しなかった行(解決先の決まらないテンプレートの呼び出し、索引に無い一致など)の節の行数を減らす
    #   (--other-limit。解決済みの呼び出しは答えそのものなので、先に減らさない)
    # - 節ごとの行数を減らす(解決済みの行だけで数百行ある名前。呼び出し側が渡した limit より小さい値だけを試す。後ろに足した
    #   --limit が勝つ)
    # どの形でも、1 行ずつ並べなかった行は、残りの一覧としてファイルに書き、最後の行(coverage)の案内をその場所に置き換える
    # (結果と一覧を合わせると、名前を含む行がどれも 1 回ずつ出る。一覧は、結果の形が決まってから作る)
    asked = int(args[args.index('--limit') + 1]) if '--limit' in args else 200
    virt = '\n== virtual dispatch\n' in text
    short = ['--compact'] * 2
    forms = ([['--family', str(min(10, asked))]] if virt else []) + [['--compact'], short]
    # 族をさらに絞る(直接の呼び出しを減らす前に。UE5 の FAnimInstanceProxy::Initialize は、族を 10 にした短い形が上限を
    # 少し超え、節ごとの行数を 5 まで下げて、16 ある直接の呼び出しを 5 つしか並べなかった)。絞った分は残りの一覧に出る
    forms += [short + ['--family', str(n)] for n in FIT_FAMILY if n < asked] if virt else []
    low = ['--family', str(min(FIT_FAMILY[-1], asked))] if virt else []
    # UE5 の FAnimInstanceProxy::Initialize は、差分パスを掛けた索引で無効な #if とマクロの引数の行が増え、節ごとの行数を
    # 2 まで下げて、16 ある直接の呼び出しを 2 つしか並べなかった
    forms += [short + low + ['--other-limit', str(n)] for n in FIT_LIMITS if n < asked]
    # 節ごとの行数を下げるときは、各行数で、まずどの節も同じ行数にした形を試し(並ぶ行がいちばん多い)、超えたら
    # 解決しなかった行の節だけを最小にした形を試す(呼び出しを同じ行数に保ったまま縮む)
    least = ['--other-limit', str(min(FIT_LIMITS[-1], asked))]
    forms += [short + low + more + ['--limit', str(n)] for n in FIT_LIMITS if n < asked
              for more in ([], least) if not more or n > FIT_LIMITS[-1]]
    for form in [None] + forms:
        if form is not None:
            code, text = run(form)
        if over(text):
            continue
        if text.endswith(ltq.ALL_HINT + '\n'):
            text = with_rest(text)
        if not over(text):
            return code, text
        for p in cfg.last_lists:  # 案内を足して上限を超えた。次の形へ(この形の一覧は、結果で知らせないので消す)
            with contextlib.suppress(OSError):
                os.remove(p)
        cfg.last_list, cfg.last_lists = None, []
    # どの形でも上限を超える。末尾を切り、名前を含む行を全部並べた一覧(ltq refs --all)の場所を添える
    all_code, all_text = run(['--all'], noted=False)
    return code, cut(text, write(all_text)[0] if all_code == 0 else [])


class UnknownTool(Exception):
    pass


def call_tool(cfg, name, a):
    if name == 'refs':
        if not a.get('name'):
            return 2, 'refs needs "name" (a qualified name or a suffix of it)'
        args = ['refs', str(a['name'])]
        if a.get('limit'):
            args += ['--limit', str(int(a['limit']))]
        for x in a.get('exclude') or []:
            args += ['--exclude', str(x)]
        paths = a.get('path') or []
        for x in [paths] if isinstance(paths, str) else paths:
            args += ['--path', str(x)]
    elif name == 'check':
        locs = a.get('locations') or []
        if isinstance(locs, str):
            locs = locs.split()
        if not locs:
            return 2, 'no locations'
        args = ['check'] + [str(x) for x in locs]
    else:
        raise UnknownTool(name)
    root = a.get('root')
    return run_ltq(cfg, args, str(root) if root else None)


def handle(msg, cfg):
    method, mid = msg.get('method'), msg.get('id')
    if method == 'initialize':
        ver = (msg.get('params') or {}).get('protocolVersion') or PROTOCOL
        return {'protocolVersion': ver, 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'code-ref-index', 'version': VERSION}, 'instructions': INSTRUCTIONS}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        p = msg.get('params') or {}
        try:
            code, text = call_tool(cfg, p.get('name'), p.get('arguments') or {})
        except UnknownTool:
            raise RpcError(-32602, f'unknown tool: {p.get("name")}')
        except Exception as e:  # 索引の読み込み失敗など。ツールの誤りとして返す
            code, text = 1, f'{type(e).__name__}: {e}'
        return {'content': [{'type': 'text', 'text': text}], 'isError': code != 0}
    if mid is None:
        return None  # 通知(notifications/initialized など)には返さない
    raise RpcError(-32601, f'method not found: {method}')


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def main():
    ap = argparse.ArgumentParser(prog='code-ref-index-mcp')
    ap.add_argument('--root', default=os.environ.get('CRI_ROOT'),
                    help='source root (default: the nearest folder with .index, from the cwd upward)')
    ap.add_argument('--index', default=os.environ.get('CRI_INDEX'), help='index TSV (default <root>/.index/...)')
    ap.add_argument('--auto-update-wait', type=float, default=float(os.environ.get('CRI_AUTO_UPDATE_WAIT', 10)),
                    help='seconds to wait for an automatic rebuild of a stale index (default 10; 0 = do not wait)')
    ap.add_argument('--no-auto-update', action='store_true', default=os.environ.get('CRI_AUTO_UPDATE') == '0',
                    help='never rebuild the index automatically (or set CRI_AUTO_UPDATE=0)')
    ap.add_argument('--max-output-bytes', type=int, default=int(os.environ.get('CRI_MAX_OUTPUT_BYTES', MAX_BYTES)),
                    help=f'shorten results larger than this (default {MAX_BYTES}; 0 = never)')
    ap.add_argument('--no-resident', action='store_true', default=os.environ.get('CRI_RESIDENT') == '0',
                    help='do not keep the source list, word filter and name directory between calls (or set CRI_RESIDENT=0)')
    a = ap.parse_args()
    cfg = Config(a.root, a.index, None if a.no_auto_update else max(0.0, a.auto_update_wait), max(0, a.max_output_bytes),
                 resident=not a.no_resident)
    print(f'code-ref-index mcp {VERSION}: root={cfg.fixed or "auto (cwd upward)"} auto-update={"off" if cfg.wait is None else f"wait {cfg.wait:g} s"}',
          file=sys.stderr)

    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8')
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', newline='\n')
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            if 'method' not in msg:
                continue  # 応答(このサーバーは要求を出さない)
            try:
                result = handle(msg, cfg)
                if result is None:
                    continue
                reply = {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': result}
            except RpcError as e:
                reply = {'jsonrpc': '2.0', 'id': msg.get('id'), 'error': {'code': e.code, 'message': str(e)}}
        stdout.write(json.dumps(reply, ensure_ascii=False) + '\n')
        stdout.flush()


if __name__ == '__main__':
    main()
