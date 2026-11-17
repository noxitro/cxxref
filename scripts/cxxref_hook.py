"""Copilot CLI の preToolUse hook(プラグインの com.github.copilot/hooks/hooks.json から呼ばれる)。

skill の cxxref は、プラグインの mcp/cxxref_cli.py を shell で呼ぶ。プラグインは作業フォルダの外
(~/.copilot/installed-plugins)にあるので、Copilot CLI は呼ぶたびに許可を求め、確認できない場面(copilot -p)では断る
【実測: 1.0.109。断られたモデルは rg で探し直した】。この hook は、このプラグイン自身の cxxref_cli.py を refs か check で
呼ぶだけのコマンドに限って、許可を返す。hook が allow を返すと、Copilot はその呼び出しの許可の確認(作業フォルダの外の
パスの確認も)を飛ばす【実測: 1.0.109 の本体】。そのため、形を狭く決めて、それ以外には何も返さない(ふだんの確認に任せる)。

許す形(PowerShell の 1 行だけ)
  python[.exe] -I "<このプラグインの mcp\\cxxref_cli.py の絶対パス>" refs|check <引数>...
- 語は ASCII の空白 1 つ以上で区切る。語は次のどれか(ここでの分け方と、PowerShell・python の分け方が食い違わない形)
  - 裸の語: 英数字と _ . : \\ / = + ~ - と、ASCII でない文字・数字(Unicode の L・M・N)だけ。PowerShell は NBSP・全角空白
    などの非 ASCII の空白を区切りに、全角の引用符(‘ ’ “ ” など)を引用符に、– — を - に使うので、それらは受けない。
    ~ で始まる語と、- 1 つで始まる語(-I を除く)も受けない。PowerShell が書き換えるため【実測: 5.1.26100 と 7.6 で
    -a.b は -a と .b に分かれ、-x: は次の語とつながる。7.6 は ~ と ~\\x をホームフォルダに展開する。-- で始まる語と、
    語の途中の ~(Foo::~Foo)はそのまま渡る】
  - "…": 中に " ` $ を含まない(式の展開をさせない)。'…': 中に ' と " を含まない。どちらも、中は ASCII の印字可能文字と
    上の非 ASCII の文字・数字だけ。空でなく、\\ で終わらない(Windows PowerShell 5.1 などの古い渡し方では、
    中の \\" や "" で語が分かれたり、空の語が消えたりする【実測: 5.1.26100 で "X"" --root C:\\evil" が 3 つの引数に、
    '--exclude=\\"a --root C:\\evil --exclude=b\\"' が 4 つの引数になった】)。閉じた引用符のすぐ後に文字を続けない
  - どの語にも & | < > ^ % を含まない(引用符の中でも)。python がバッチの shim(pyenv-win の python.bat など)だと
    PowerShell は cmd.exe を通して呼び、引用符の中でも cmd がこれらを解釈する【実測: 5.1.26100 と 7.6 で、python.bat を
    PATH の先頭に置くと "a>made.txt" はファイルを作り、"%OS%" は展開され、"a^b" は ab になった】
  これで、関数名の operator() は引用符の中で、日本語のフォルダ名やプラグインの場所は裸でも渡せる。operator< や
  Vec<int> のように < > などを含む名前は、ふだんの確認になる
- 先頭は裸の python / python.exe / py(フォルダ付きは許さない。.\\python などは別の実行ファイルを動かせる)
- 次は -I(必須。ほかのフラグは受けない)、その次は実体がこのプラグインの cxxref_cli.py である絶対パス
- --root と、その省略形(--roo・--r= など。argparse の省略は CLI の側でも切ってある)、// や \\\\ を含む引数(UNC)を含まない
  (--root で別の場所の索引と作り直しの記録を読ませない。UNC は開くだけで資格情報を送りうる)
- 道具は powershell / pwsh だけ(ほかの shell は字句の規則が違う)

自動で許可した呼び出しでは、CLI は、この PC で index-project(/cxxref-index も同じ)が作った索引だけを自動で作り直す
(mcp/autoupdate.py の trusted_recipe。細工したリポジトリに置かれた作り直しの記録で ltidx や make-cdb を動かさない)。
"""
import hashlib
import json
import os
import re
import string
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.realpath(os.path.join(HERE, '..', 'mcp', 'cxxref_cli.py'))
CLI = os.path.normcase(CLI_PATH)
SHELLS = {'powershell', 'pwsh'}
PYTHON = re.compile(r'(?i)(?:python3?|py)(?:\.exe)?')
_ASCII = {chr(c) for c in range(0x20, 0x7f)}
BARE = set(string.ascii_letters + string.digits + '_.:\\/=+~-')  # 裸の語に使える ASCII
_CMD = set('&|<>^%')  # python がバッチの shim だと cmd.exe を通り、引用符の中でも cmd が解釈する
# 引用符の中に使える ASCII。' の中の " も受けない(5.1 は、空白がすべて " の対の内側にある引数を引用符で包まずに渡す)
QUOTED = {'"': _ASCII - set('"`$') - _CMD, "'": _ASCII - set('\'"') - _CMD}


def _ok(ch, ascii_ok):
    """ch を語に入れてよいか。ASCII は ascii_ok の中だけ、ほかは文字・結合文字・数字だけ(空白・引用符・ダッシュの類を除く)。"""
    return ch in ascii_ok if ch < '\x80' else unicodedata.category(ch)[0] in 'LMN'


def split_words(command):
    """PowerShell の 1 行を語に分ける(引用符は外す)。分け方がここと PowerShell・python で食い違いうる形なら None。"""
    words, i, n = [], 0, len(command)
    while i < n:
        if command[i] == ' ':
            i += 1
            continue
        q = command[i]
        if q in QUOTED:
            j = command.find(q, i + 1)
            if j < 0:
                return None
            body = command[i + 1:j]
            if not body or body.endswith('\\') or not all(_ok(c, QUOTED[q]) for c in body):
                return None
            if j + 1 < n and command[j + 1] != ' ':  # "a""b"・"a"b のように続ける形
                return None
            words.append(body)
            i = j + 1
        else:
            j = command.find(' ', i)
            j = n if j < 0 else j
            body = command[i:j]
            if not all(_ok(c, BARE) for c in body):
                return None
            # PowerShell が書き換える裸の語: ~ で始まる(ホームフォルダに展開)、- 1 つで始まる(. で分ける・: で次とつなぐ)
            if body.startswith('~') or (body.startswith('-') and not body.startswith('--') and body != '-I'):
                return None
            words.append(body)
            i = j
    return words


def _root_option(word):
    """word が --root(とその省略形 --r・--ro・--roo、-r など。= 付きも)なら True。"""
    opt = word.split('=', 1)[0]
    name = opt.lstrip('-')
    return opt.startswith('-') and bool(name) and 'root'.startswith(name)


def allowed(command):
    """command が、このプラグインの cxxref_cli.py を refs / check で呼ぶだけなら True。"""
    words = split_words(command) if isinstance(command, str) else None
    if not words or len(words) < 4 or not PYTHON.fullmatch(words[0]) or words[1] != '-I':
        return False
    script, sub, rest = words[2].replace('\\', '/'), words[3], words[4:]
    if not os.path.isabs(script) or script.startswith('//'):
        return False
    if os.path.normcase(os.path.realpath(script)) != CLI or sub not in ('refs', 'check'):
        return False
    return not any(_root_option(w) or '//' in w.replace('\\', '/') for w in rest)


SESSION_NOTE = ('This repository has a cxxref index (.index, built by clang over every C++ translation unit). '
                'When the question is where a C++ symbol is used or called, or what a change to it would affect, '
                'load the cxxref skill before grepping. To make the change yourself, grep first; load it only if the '
                'callers are in 5+ files. Answer in the language of the question.')
# 実行するコマンドまで書いた形。skill を読む 1 往復と、モデルが CLI の場所を組み立て損ねて断られる回を省く
# 【実測: v15m。skill 版の 33 回中 13 回、SKILL.md の「..\..」を縮め損ねた場所で最初のコマンドが断られ、やり直していた】
SESSION_NOTE_CMD = ('This repository has a cxxref index (.index, built by clang over every C++ translation unit). '
                    'When the question is where a C++ symbol is used or called, or what a change to it would affect, '
                    'run this in PowerShell before grepping, exactly as written and as a command of its own (nothing chained): '
                    '{cmd} <name> (<name>: the qualified name or its suffix). Its `calls` are resolved by clang; do not grep '
                    'to confirm them. Its matches in "files no indexed TU includes" (tests, other platforms) are not '
                    'analyzed: judge them from the line, or view the file, and include the real ones. '
                    'The cxxref skill has the options; you do not need to load it first. '
                    'When asked to make the change yourself, grep as usual. Answer in the language of the question.')
# 直す作業には、コマンドの使い方(--context)を書かない。書くと、呼び出しの数を見る前に索引を引くようになり、呼び出しの
# 少ない問題で 1 往復ぶん高くなった【実測: edit2。直す 3 問の 15 回すべてで grep より先に索引を引き、呼び出しの少ない
# 問題で 1.36 倍。「いつもどおり grep」の文では、呼び出しの多い・少ない問題で 0/5 回】。--context は skill の本文にだけ書く
# 最後の 1 文: hook の文が英語なので、日本語の問いに英語で答える回が増えていた【実測: v16m。各 60 回で、rg だけ 10 回、
# skill 版 23〜27 回】
# 「`calls` が答え」とだけ書いていた前の文では、索引の構成に入らないファイル(テスト、別のプラットフォーム)の一致を、
# モデルが答えに入れなかった【実測: big1。O3DE の 3 問 15 回で、テストの呼び出しを全部挙げたのは 3 回。MCP 版は 11 回】


def cli_command():
    """このプラグインの CLI を refs で呼ぶ PowerShell の 1 行(名前の手前まで)。allowed() が通す形で書けなければ None
    (場所に、引用符の中に置けない文字がある。そのときは skill を読ませる文に戻す)。"""
    for q in QUOTED:
        cmd = f'python -I {q}{CLI_PATH}{q} refs'
        if allowed(cmd + ' x'):
            return cmd
    return None


def index_root(cwd):
    """cwd かその親で、索引(.index の中の .tsv)のあるフォルダ。無ければ None(ltq.find_root と同じ探し方)。"""
    d = os.path.abspath(cwd) if isinstance(cwd, str) and cwd else None
    while d:
        idx = os.path.join(d, '.index')
        if os.path.isdir(idx):
            try:
                if any(f.endswith('.tsv') for f in os.listdir(idx)):
                    return d
            except OSError:
                return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def has_index(cwd):
    return index_root(cwd) is not None


# --- grep を refs に振り向ける(試作。既定では切ってある) -------------------------------------------------------
# 呼び出し側を直す作業では、呼び出しが多くのファイルに散るときだけ、索引を引くと rg だけより安い【実測: 呼び出し 16 の
# 問題で、refs を使った 3 つの時期を合わせて 0.82 倍(0.75〜0.89)。2〜3 ファイルの問題は 1.06〜1.32 倍】。
# 環境変数 CXXREF_GREP_REDIRECT=1 のときだけ、C++ の名前の grep を見て索引で呼び出しのファイル数を数え、MANY_FILES 以上
# なら、その grep を断り、理由に refs のコマンドを書く。振り向けはセッションに 1 回だけで、編集の後はしない。
# 既定で切ってある理由【実測: v14i と独立レビュー】: 直そうとしている記号と関係のない grep(ありふれた名前、別の作業の途中)
# を断ることがあり、費用は振り向けだけでは減らなかった(0.95 倍)。説明文で「先に grep、多ければ skill」と案内する形を先に試す
MANY_FILES = 5
MAX_SYMBOLS = 3  # 同じ名前で、呼び出しの多い記号がこれより多ければ、ありふれた名前とみなして振り向けない
MAX_ROWS = 20000  # 1 つの名前について索引から読む行の上限(超えたら、ありふれた名前とみなして振り向けない)
SCAN_SECONDS = 2.5  # 索引を数えるのに使う時間の上限(hook の制限時間は 10 秒。超えたら振り向けない)
SCAN_LIMIT = 100 << 20  # 目次(.names.cache)が無いとき、頭から読むのはこの大きさまで
EDIT_TOOLS = {'edit', 'create', 'write', 'str_replace', 'str_replace_editor', 'apply_patch', 'multiedit', 'insert'}
SIDE = ('.tu.tsv', '.skip.tsv', '.deps.tsv', '.alt.tsv')  # 索引本体の隣のファイル(ltq.SIDE と同じ)
CXX_EXT = {'c', 'cc', 'cpp', 'cxx', 'c++', 'h', 'hh', 'hpp', 'hxx', 'h++', 'inl', 'ipp', 'tpp', 'ixx', 'cppm'}
NAME = re.compile(r'[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*')
SYMBOL = re.compile(r'(?:[A-Za-z_]\w*::)*[A-Za-z_]\w*')  # 理由とコマンドに書いてよい記号(英数字と _ と :: だけ)
ESCAPES = re.compile(r'\\[bBsSwWdDnrtAZz<>]|\\.|\[[^\]]*\]|\(\?[^)]*\)')
SKIP_WORDS = {'class', 'struct', 'enum', 'union', 'void', 'const', 'static', 'virtual', 'override', 'inline',
              'return', 'template', 'typename', 'auto', 'int', 'bool', 'char', 'float', 'double', 'unsigned',
              'define', 'include', 'if', 'else', 'for', 'while', 'switch', 'case', 'new', 'delete'}


def redirect_enabled():
    return os.environ.get('CXXREF_GREP_REDIRECT') == '1'


def pattern_names(pattern):
    """grep の pattern に、そのままの形で書かれた C++ の名前(`\\bWait\\s*\\(` なら Wait、`A::B|class C` なら A::B と C)。
    名前の一部だけを書いた形(`Get\\w+`・`Get.*`・`Foo+`、`(?:A|B)::Init` の Init)は拾わない。"""
    if not isinstance(pattern, str) or len(pattern) > 300:
        return []

    def mask(m):  # 位置を変えずに隠す。名前の続きに当たりうるもの(\w・\d・\S・文字クラス)は \x01 にする
        t = m.group(0)
        return ('\x01' if t in ('\\w', '\\d', '\\S') or t.startswith('[') else ' ') * len(t)
    masked = ESCAPES.sub(mask, pattern)
    out = []
    for m in NAME.finditer(masked):
        n = m.group(0)
        before, after = masked[max(0, m.start() - 2):m.start()], masked[m.end():m.end() + 1]
        if before.endswith(('::', '\x01', '.')) or after in ('\x01', '*', '+', '?', '{', '.'):
            continue
        if n not in out and n not in SKIP_WORDS and len(n.rsplit('::', 1)[-1]) >= 3:
            out.append(n)
    return out[:4]


def cxx_search(args):
    """grep の glob・type・path が、C/C++ のソースを探しているか(拡張子が書かれていて、C/C++ のものが無ければ False)。"""
    exts = set()
    for key in ('glob', 'type'):
        v = args.get(key)
        if isinstance(v, str) and v:
            found = re.findall(r'[A-Za-z0-9+]+', v.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]) if key == 'glob' else [v]
            exts |= {x.lower() for x in found}
            if key == 'glob' and '.' not in v:
                exts = set()
    path = args.get('path')
    if isinstance(path, str) and os.path.splitext(path)[1]:
        exts.add(os.path.splitext(path)[1][1:].lower())
    return not exts or bool(exts & CXX_EXT)


def index_files(root):
    """<root>/.index の索引本体(ltq.find_indexes と同じ選び方)。"""
    d = os.path.join(root, '.index')
    try:
        return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith('.tsv') and not f.endswith(SIDE)]
    except OSError:
        return []


def _index_lines(path, short):
    """索引 path の、記号の最後の部分が short(bytes)である行の候補(ハッシュの衝突を含む)。多すぎる・読めないときは None。"""
    sys.path.insert(0, os.path.join(HERE, '..', 'query'))
    import namedir  # noqa: E402  (hook は呼ぶたびに別のプロセス。grep を見るときだけ読む)
    nd = namedir.NameDir(path)
    try:
        if nd.open():
            offs = nd.offsets(short)
            if len(offs) > MAX_ROWS:
                return None
            out = []
            with open(path, 'rb') as f:
                for off in offs:
                    f.seek(off)
                    out.append(f.readline())
            return out
    finally:
        nd.close()
    if os.path.getsize(path) > SCAN_LIMIT:
        return None
    with open(path, 'rb') as f:
        data = f.read()
    out, key = [], short + b'\tcall'
    i = data.find(key)
    while i >= 0:
        if len(out) >= MAX_ROWS:
            return None
        s0 = data.rfind(b'\n', 0, i) + 1
        e = data.find(b'\n', i)
        e = len(data) if e < 0 else e
        out.append(data[s0:e])
        i = data.find(key, e)
    return out


def call_files(root, name, deadline):
    """{記号: 呼び出しのあるファイルの集合}。name は修飾名かその後ろの部分(ltq の refs と同じ合わせ方)。
    行が多すぎる(ありふれた名前)・時間切れ・読めないときは None(振り向けない)。"""
    short = name.rsplit('::', 1)[-1].encode()
    tail = b'::' + name.encode()
    out = {}
    for path in index_files(root):
        if time.monotonic() > deadline:
            return None
        lines = _index_lines(path, short)
        if lines is None:
            return None
        for line in lines:
            cols = line.rstrip(b'\r\n').split(b'\t')
            if len(cols) < 4 or not cols[1].startswith(b'call'):
                continue
            sym = cols[0]
            if sym != name.encode() and not sym.endswith(tail):
                continue
            if sym.startswith((b'std::', b'::std::', b'__')) or b'::__' in sym:
                continue  # 標準ライブラリとコンパイラの名前(直す先はこのリポジトリに無い)
            parts = sym.split(b'::')
            if '::' not in name and len(parts) >= 2 and parts[-1] == parts[-2]:
                continue  # 型の名前(`class Foo` の Foo)で、コンストラクタ Foo::Foo の呼び出しを数えない
            out.setdefault(sym.decode('utf-8', 'replace'), set()).add(cols[3].rsplit(b':', 2)[0])
    return out


# セッションの間の記録は、印のファイル 2 つ(told: もう振り向けた。edited: 編集が始まった)。作るのは排他(O_EXCL)なので、
# 並列に走る hook のうち 1 つだけが振り向ける【実測: 独立レビュー。読んでから書く形では、並列の grep 6 件が全部断られた】。
# 鍵は索引のあるフォルダ。同じフォルダで 2 つのセッションを同時に使うと、記録は混ざる(振り向けが 1 回増えるか減るだけ)
def _mark_path(root, kind):
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or HERE
    key = hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode('utf-8')).hexdigest()[:16]
    return os.path.join(base, 'cxxref', 'hook-state', f'{key}.{kind}')


def _marked(root, kind):
    return os.path.exists(_mark_path(root, kind))


def _mark(root, kind):
    """印を付ける。いま付けたなら True、もう付いていた・付けられなかったなら False。"""
    p = _mark_path(root, kind)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        os.close(os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except OSError:
        return False


def _unmark(root):
    for kind in ('told', 'edited'):
        try:
            os.remove(_mark_path(root, kind))
        except OSError:
            pass


def redirect_grep(data):
    """grep の pattern の名前が、索引で MANY_FILES 以上のファイルから呼ばれていれば、断る理由(refs の案内)。"""
    root = index_root(data.get('cwd'))
    args = data.get('toolArgs')
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if root is None or not isinstance(args, dict) or not cxx_search(args):
        return None
    names = pattern_names(args.get('pattern'))
    # 振り向けはセッションに 1 回だけ。編集の後もしない(索引が古くなり、refs は作り直しを待つ。確かめの grep を塞がない)
    # 【実測: 試しの 1 回で、編集の後の確かめの grep に std::forward_like(37 ファイル)を案内していた】
    if not names or _marked(root, 'told') or _marked(root, 'edited'):
        return None
    deadline = time.monotonic() + SCAN_SECONDS
    hits = []
    for n in names:
        found = call_files(root, n, deadline)
        if found is None:
            return None
        # 索引は作業中のリポジトリの中にあり、細工されうる。理由とコマンドに書くのは、英数字と _ と :: だけの記号に限る
        # (ほかの文字を含む記号は、モデルへの指示やコマンドの引数として読まれうる【実測: 独立レビュー】)
        hits = sorted(((len(fs), sym) for sym, fs in found.items()
                       if len(fs) >= MANY_FILES and len(sym) <= 200 and SYMBOL.fullmatch(sym)), reverse=True)
        if hits:
            break  # pattern に先に書かれた名前を使う
    if not hits or len(hits) > MAX_SYMBOLS or not _mark(root, 'told'):
        return None  # 同じ名前の記号が多い(Get・size など)なら、どれを直すのか決められない
    cli = os.path.join(os.path.dirname(HERE), 'mcp', 'cxxref_cli.py')
    said = '; '.join(f'{sym} is called from {k} files' for k, sym in hits)
    other = ' (or with another name listed above, if that is the one you are changing)' if len(hits) > 1 else ''
    return (f'cxxref (clang index of every translation unit): {said}. If you are changing its signature and fixing the '
            'callers, run this once in PowerShell, as a command of its own, instead of this grep' + other + ':\n'
            f'python -I "{cli}" refs {hits[0][1]} --context 3\n'
            'It lists the calls, references, declarations and definitions, with the source lines around them when the '
            'result is small enough; write the edits from those lines and view only small line ranges. For anything '
            'else, or for the text matches, run the same grep again (this note is shown once per session).')


def session_start(data):
    """sessionStart: 索引のあるリポジトリでだけ、索引を引く場面と、そのコマンドを知らせる。索引が無ければ何も足さない
    (skill の説明文だけでは、使わないモデルがいた【実測: Claude Haiku 5.5 は grep から始めた。gpt-6-luna は読んだ】)。
    grep の振り向けを入れているときは、その記録もここで消す(続きから再開したセッションでは消さない)。"""
    root = index_root(data.get('cwd'))
    if root is not None:
        if redirect_enabled() and data.get('source') != 'resume':
            _unmark(root)
        cmd = cli_command()
        sys.stdout.write(json.dumps({'additionalContext': SESSION_NOTE_CMD.format(cmd=cmd) if cmd else SESSION_NOTE}))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace') or '{}')
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    if argv[:1] == ['session']:
        return session_start(data)
    tool = str(data.get('toolName', '')).lower()
    if redirect_enabled() and tool == 'grep':
        try:
            reason = redirect_grep(data)
        except Exception:  # 振り向けは補助。何があっても grep は止めない
            reason = None
        if reason:
            sys.stdout.write(json.dumps({'permissionDecision': 'deny', 'permissionDecisionReason': reason}))
        return 0
    if redirect_enabled() and tool in EDIT_TOOLS:  # 編集が始まったら、振り向けをやめる(許可には何も返さない)
        root = index_root(data.get('cwd'))
        if root is not None:
            _mark(root, 'edited')
        return 0
    if tool not in SHELLS:
        return 0
    args = data.get('toolArgs')
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return 0
    command = args.get('command') if isinstance(args, dict) else None
    if allowed(command):
        sys.stdout.write(json.dumps({'permissionDecision': 'allow'}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
