"""索引の自動更新。MCP サーバーが、索引の後にソースが変わったのを見つけたら、裏で作り直して少しだけ待つ。

  python -I autoupdate.py --dir <.index のフォルダ> [--full]   (サーバーが最低優先度の別プロセスで起動する)

作り方は scripts/index-project.ps1 が残す <索引>.recipe.json を使う。ふだんは ltidx -update だけを流し、
新しいソース・消えた TU があるとき(--full)は、compile_commands.json も make-cdb.ps1 で作り直す。
同時に 1 つだけ走らせる。ロックは update.lock を開いたままにして示す(Windows では開かれたファイルを
消せないので、消せたら持ち主はもういない)。結果は update.json、ログは update.log に書く。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

HERE =os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, '..', 'scripts'))
MAKE_CDB = os.path.join(SCRIPTS, 'make-cdb.ps1')
ALTPASS = os.path.join(SCRIPTS, 'altpass.py')
RECIPE = '.recipe.json'


def find_ltidx():
    """ltidx.exe の場所。環境変数 LTIDX(あればそのまま)、このリポジトリでビルドしたもの(indexer\\build)、
    配布物を取ってきたもの(scripts\\get-ltidx.ps1 が %LOCALAPPDATA%\\cxxref\\ltidx に置く)の順。無ければ None。
    プラグインとして入れた写しにはビルドしたものが無いので、配布物を使う。"""
    if os.environ.get('LTIDX'):
        return os.environ['LTIDX']
    cands = [os.path.join(HERE, '..', 'indexer', 'build', 'ltidx.exe')]
    if os.environ.get('LOCALAPPDATA'):  # 無ければ探さない(作業フォルダからの相対パスにしない)
        cands.append(os.path.join(os.environ['LOCALAPPDATA'], 'cxxref', 'ltidx', 'ltidx.exe'))
    for p in cands:
        if os.path.exists(p):
            return os.path.normpath(p)
    return None


def plugin_version():
    """このプラグイン(リポジトリ)の版(plugin.json の version)。読めなければ空"""
    try:
        with open(os.path.join(HERE, '..', 'plugin.json'), encoding='utf-8-sig') as f:
            return str(json.load(f).get('version') or '')
    except (OSError, ValueError):
        return ''


def stale_ltidx():
    """使う ltidx が取ってきたもの(%LOCALAPPDATA%/cxxref/ltidx)で、その版(隣の version.txt)がプラグインと違えば
    (その版, プラグインの版)。ビルドしたもの・環境変数 LTIDX のもの・見つからないときは None。
    プラグインを更新しても取ってきた ltidx は入れ替わらないので、/cxxref-index で取り直すよう知らせるのに使う"""
    p = find_ltidx()
    if not p or os.environ.get('LTIDX') or not os.environ.get('LOCALAPPDATA'):
        return None
    got = os.path.normcase(os.path.normpath(os.path.join(os.environ['LOCALAPPDATA'], 'cxxref', 'ltidx', 'ltidx.exe')))
    if os.path.normcase(os.path.normpath(os.path.abspath(p))) != got:
        return None
    want = plugin_version()
    try:
        with open(os.path.join(os.path.dirname(p), 'version.txt'), encoding='utf-8-sig') as f:
            have = f.read().strip()
    except OSError:
        have = ''
    return (have or 'unknown', want) if want and have != want else None


def no_ltidx():
    if os.environ.get('LTIDX'):  # 取ってきても直らない
        return f'ltidx.exe not found at LTIDX={os.environ["LTIDX"]}; fix or unset the LTIDX environment variable'
    return f'ltidx.exe not found; run {os.path.join(SCRIPTS, "get-ltidx.bat")} once (or build it with indexer\\build.bat)'


def recipe_of(index):
    try:
        with open(index + RECIPE, encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# この PC で index-project(/cxxref-index も同じもの)が書いた作り直しの記録の指紋。リポジトリの外に置く。
# skill から自動で許可された CLI の呼び出しは、ここに載った記録の索引だけを作り直す。リポジトリに置かれた記録
# (compile_commands.json の場所と make-cdb の引数)を信じると、細工したリポジトリを開いただけで、作り直しが
# 細工した compile_commands.json で ltidx を動かすため(コンパイラのプラグインを読ませる引数は、別に unsafe_cdb で止める)。
# 記録 1 つごとに 1 ファイル(trusted\<鍵のハッシュ>.json)。1 つのファイルを読んで足して書き戻すと、索引づくりを
# 同時に走らせたときに片方の登録が消えるため。0.5.10 の trusted-recipes.json(全部を 1 つに書いたもの)も読む
TRUST_DIR = 'trusted'
TRUST_FILE = 'trusted-recipes.json'


def _trust_base():
    base = os.environ.get('LOCALAPPDATA')
    return os.path.join(base, 'cxxref') if base else None


def _recipe_key(index):
    return os.path.normcase(os.path.realpath(index + RECIPE))


def _trust_path(index):
    base = _trust_base()
    if not base:
        return None
    name = hashlib.sha256(_recipe_key(index).encode('utf-8')).hexdigest()[:32] + '.json'
    return os.path.join(base, TRUST_DIR, name)


def _recipe_digest(index):
    try:
        with open(index + RECIPE, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _read_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def trust_available():
    """登録を置く場所(%LOCALAPPDATA%\\cxxref)があるか。"""
    return _trust_base() is not None


def trust_recipe(index):
    """index の作り直しの記録を、この PC で作ったものとして登録する(index-project.ps1 が記録を書いた直後に呼ぶ)。"""
    p, digest = _trust_path(index), _recipe_digest(index)
    if not p or not digest:
        return False
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f'{p}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'recipe': _recipe_key(index), 'digest': digest}, f, ensure_ascii=False)
    os.replace(tmp, p)
    return True


def trusted_recipe(index):
    """index の作り直しの記録が、登録したときから変わっていなければ True。"""
    digest, p = _recipe_digest(index), _trust_path(index)
    if not digest or not p:
        return False
    d = _read_json(p)
    if d.get('recipe') == _recipe_key(index):
        return d.get('digest') == digest
    return _read_json(os.path.join(_trust_base(), TRUST_FILE)).get(_recipe_key(index)) == digest


# 作り直しの案内に "…" で埋めてよい値: 英数字・空白と _ . : \ / = + - ( ) , ~ @ #、ASCII でない文字・数字(Unicode の L・M・N)。
# 記録は登録の無い(信頼していない)もので、案内は PowerShell か cmd に貼られる。" の中でも PowerShell は $ ` と全角の引用符
# (“ ” など)を、cmd は % を解釈する【実測: 5.1.26100 と 7.6 で、"a”; Set-Content INJ.txt x; “b" がファイルを作った】
_HINT_ASCII = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.:\\/=+-(),~@#')
_HINT_NAME = re.compile(r'[A-Za-z0-9_-]+')  # index-project.ps1 の -Name と同じ


def _hint_value(v):
    """v を案内のコマンドに "…" で埋めてよければ True。\\ で終わるもの(5.1 では閉じる引用符を消す)と UNC も埋めない。"""
    if not isinstance(v, str) or not v or v.endswith('\\') or v.replace('\\', '/').startswith('//'):
        return False
    return all(c in _HINT_ASCII if c < '\x80' else unicodedata.category(c)[0] in 'LMN' for c in v)


def _same_path(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _rebase(p, old, new):
    """old の下の絶対パス p を、new の下の同じ場所にする(索引のフォルダを移した・写したとき)。old の外ならそのまま。"""
    if not (isinstance(p, str) and isinstance(old, str) and os.path.isabs(p) and os.path.isabs(old)):
        return p
    try:
        rel = os.path.relpath(p, old)
    except ValueError:  # 別のドライブ
        return p
    return p if rel == os.pardir or rel.startswith(os.pardir + os.sep) else os.path.normpath(os.path.join(new, rel))


def script_command(name, *args):
    """scripts\\<name>.ps1 を、PowerShell(5.1・7)でも cmd でもそのまま動く形で動かすコマンド(rebuild_hint と同じ形)。
    args は (引数の名前, 値) の並び。値に埋められない文字があれば None(作業フォルダの名前もリポジトリ次第で選べる)。"""
    script = os.path.join(SCRIPTS, name + '.ps1')
    if not _hint_value(script) or not all(_hint_value(v) for _, v in args):
        return None
    return f'pwsh -NoProfile -ExecutionPolicy Bypass -File "{script}"' + ''.join(f' -{k} "{v}"' for k, v in args)


def rebuild_hint(index, root):
    """登録の無い索引を、この PC で作り直して登録するコマンド(作り直しの記録の構成のまま、変わった分だけ)と、
    コマンドに入れられなかった引数の名前。(コマンド, [引数の名前])。コマンドを作れなければ (None, [])。
    PowerShell(5.1・7)でも cmd でもそのまま動く形にする【実測: "…bat" -Root … は PowerShell では構文エラー、
    & "…bat" は cmd で誤り。pwsh -File "…ps1" は 3 つとも通る】。.bat を通さないので、cmd が値を解釈し直すこともない。"""
    name = os.path.basename(index)[:-len('.tsv')] if index.endswith('.tsv') else ''
    cmd = script_command('index-project', ('Root', root))
    if not (_HINT_NAME.fullmatch(name) and cmd):
        return None, []
    rec = recipe_of(index)
    rec = rec if isinstance(rec, dict) else {}
    old = rec.get('root') if isinstance(rec.get('root'), str) else root
    opts, left = [], []

    def add(key, value):
        if _hint_value(value):
            opts.append(f'-{key} "{value}"')
        else:
            left.append(f'-{key}')

    mk = rec.get('makeCdb')
    if isinstance(mk, dict):  # compile_commands.json を作らせた索引: 作り元と構成を渡す
        for k in ('Project', 'Configuration', 'Platform', 'CMakeArgs'):
            if mk.get(k) not in (None, ''):
                add(k, _rebase(mk[k], old, root) if k == 'Project' else mk[k])
    elif isinstance(rec.get('cdb'), str) and rec['cdb'] and not (os.path.isabs(old) and _same_path(rec['cdb'], old)):
        add('Cdb', _rebase(rec['cdb'], old, root))  # ルートのものを使った索引は、渡さなくても同じものを使う
    jobs = rec.get('jobs')
    if isinstance(jobs, int) and not isinstance(jobs, bool) and 0 < jobs <= 1024:
        opts.append(f'-Jobs {jobs}')
    if rec.get('alt') is False:
        opts.append('-NoAlt')
    return ' '.join([cmd] + ([f'-Name {name}'] if name != 'index' else []) + opts + ['-Update']), left


# compile_commands.json の中で、ltidx(clang)にコードを読ませうる・引数をファイルから足させる・ファイルを書かせる引数。
# 作り直しの前に見て、あれば作り直さない。ltidx と同じ読み方で見る(下の unsafe_cdb)。前に付きうるもの: clang-cl の
# /clang: と -clang:(どちらも効く。/CLANG: は効かない)、-Xclang=【実測: ltidx 0.5.7】
_PREFIX = r'(?:[/-]clang:|-Xclang=)?'
# コンパイラのプラグイン、clang の設定ファイル
UNSAFE_ARG = re.compile(_PREFIX + r'(?:-fplugin\b|-fpass-plugin\b|-load$|-plugin$|-add-plugin$|-plugin-arg-|--config\b)')
# 書き先を選べるファイルを書かせる引数。ltidx は -o・-MD・-MF・-MJ を(前に何も付かなければ)落とすが、ほかは落とさない
# 【実測: ltidx 0.5.7 で、次の形はどれも指定した場所にファイルを作った: --serialize-diagnostics・-serialize-diagnostic-file・
# -header-include-file・-diagnostic-log-file・-dependency-dot・-dependency-file・-stats-file=・-save-stats・
# -gen-cdb-fragment-path・--write-dependencies・--write-user-dependencies。/clang:-MD・-clang:-MF・-Wp,-MMD,<file> も作った。
# 前に何も付かない -MD -MF <file>・-o <file>・-MJ <file> は作らなかった】。依存ファイルは -MT で中身の先頭を選べるので、
# 起動時に実行されるフォルダへ .bat を置かれうる。-fcrash-diagnostics-dir は、clang が落ちたときにソースと再現用のスクリプトを書く
# -dump-minimization-hints= と -fmodules-cache-path=(暗黙のモジュールの置き場)も、指定した場所に書く【Fable のレビューで実測】
OUTPUT_ARG = re.compile(_PREFIX + r'(?:--serialize-diagnostics\b|-serialize-diagnostic-file\b|-header-include-file\b|'
                        r'-diagnostic-log-file\b|-dependency-dot\b|-dependency-file\b|-stats-file\b|-save-stats\b|'
                        r'-gen-cdb-fragment-path\b|--write-(?:user-)?dependencies\b|-fcrash-diagnostics-dir\b|'
                        r'-dump-minimization-hints\b|-fmodules-cache-path\b)')
PREFIXED_DEP = re.compile(r'(?:[/-]clang:|-Xclang=)-M')  # 前に付けた -M…(ltidx が落とさない形)
PASS_ON = {'-Xclang', '-Xpreprocessor'}  # 次の語を、そのまま clang の中に渡す(-Xclang -MF は ltidx が落とさない)
WRAPPERS = {'ccache', 'sccache', 'distcc', 'gomacc'}  # 後ろの語がコンパイラ(clang は、その隣の設定ファイルを読む)
RSP_DEPTH = 32


def _win_tokens(s):
    """s を Windows のコマンドラインの規則で語に分ける(ltidx が command と応答ファイルに使う LLVM の
    TokenizeWindowsCommandLine と同じ【実測: ltidx 0.5.7 で -DMA""RK は MARK を定義した】)。区切りは空白・タブ・改行・NUL。
    " は語を切らずに引用を始め・終える(語の途中でも)。引用の中の "" は文字の "。\\ は " の前でだけ特別で、
    2n 個 + " は \\ n 個と引用符、2n+1 個 + " は \\ n 個と文字の "。' は特別でない。"""
    tok, started, quoted, i, n = [], False, False, 0, len(s)
    while i < n:
        c = s[i]
        if c == '\\':
            j = i
            while j < n and s[j] == '\\':
                j += 1
            k = j - i
            if j < n and s[j] == '"':
                tok.append('\\' * (k // 2))
                if k % 2:
                    tok.append('"')
                    j += 1
            else:
                tok.append('\\' * k)
            started, i = True, j
        elif c == '"':
            if quoted and i + 1 < n and s[i + 1] == '"':
                tok.append('"')
                i += 2
            else:
                quoted, started, i = not quoted, True, i + 1
        elif not quoted and c in ' \t\r\n\0':
            if started:
                yield ''.join(tok)
                tok, started = [], False
            i += 1
        else:
            tok.append(c)
            started, i = True, i + 1
    if started:
        yield ''.join(tok)


# 語に分ける前の絞り込み。見つけたい引数(UNSAFE_ARG・OUTPUT_ARG・PREFIXED_DEP・PASS_ON・-Wp, と @file)は、どれも下のどれかの
# 文字の並びを含む。語に分けるときに消えるのは " と \\ だけなので、それらを除いた文字列にどれも無ければ、分けた語にも無い
# (大きな compile_commands.json のほとんどのコマンドは、ここで済む【実測: O3DE の 32 MB・6,168 件で、全部を語に分けると
# 3.0 秒、絞り込むと 0.3 秒】)
_SUSPECT = ('plugin', '-load', '--config', '@', 'serializ', 'header-include', 'diagnostic-log', 'dependenc', 'stats',
            'cdb-fragment', 'crash-diag', 'clang:-M', 'Xclang', '-Xpreprocessor', '-Wp,', 'minimization', 'modules-cache')
_NO_QUOTING = str.maketrans('', '', '"\\')


def _suspect(s):
    s = s.translate(_NO_QUOTING)
    return any(k in s for k in _SUSPECT)


def _rsp_text(path):
    """応答ファイルの中身。BOM 付きの UTF-16 は変換し、UTF-8 の BOM は除く(LLVM の読み方と同じ)。"""
    with open(path, 'rb') as f:
        data = f.read()
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode('utf-16', errors='replace')
    return (data[3:] if data[:3] == b'\xef\xbb\xbf' else data).decode('utf-8', errors='replace')


def _unsafe_arg(t, prev):
    """語 t(前の語は prev)が、コードを読ませる・ファイルを書かせる引数なら、そのわけ。"""
    if UNSAFE_ARG.match(t) or OUTPUT_ARG.match(t) or PREFIXED_DEP.match(t):
        return t
    if prev in PASS_ON and t.startswith('-M'):
        return f'{prev} {t}'
    if t.startswith('-Wp,'):  # -Wp,-MD,<file> のように、, で区切って clang の中に渡す
        parts = t.split(',')[1:]
        if any(UNSAFE_ARG.match(p) or OUTPUT_ARG.match(p) or p.startswith('-M') for p in parts):
            return t
    return None


class _Refuse(Exception):
    """応答ファイルを、ltidx と同じように開けると言い切れない(だから中身を確かめられない)。"""


def _expand(args, cwd, memo, depth=0):
    """args(引用符を外した語)の応答ファイル(@file)を、ltidx と同じく中身の語に置き換えた並び(CMake の Ninja が
    include の一覧に、UE が TU ごとの引数に使う)。ltidx は @file を cwd(directory)から探し、応答ファイルの中の @file も追い、
    無いファイル・循環したファイルは引数のまま残す(害は無い)【実測: ltidx 0.5.7】。中身を見られないときは _Refuse。
    置き換えてから並びとして見るのは、応答ファイルの最後の -Xclang が、その次の語に効くため【実測: ltidx 0.5.7】。
    memo は、読んだファイルの置き換え後の並び(同じ応答ファイルを何度も読まない)。"""
    out = []
    for t in args:
        if not t.startswith('@') or len(t) == 1:
            out.append(t)
            continue
        name = t[1:]
        # UNC は開くだけで資格情報を送りうる(\\?\ で始まるパスも、ここで止める)。\ をパスの区切りにしない OS でも、
        # ltidx(Windows)が読む形として見る
        if name.replace('\\', '/').startswith('//'):
            raise _Refuse(f'{t} (network path)')
        if not os.path.isabs(name) and not cwd:
            raise _Refuse(f'{t} (relative to a "directory" that is not absolute)')
        full = os.path.normpath(os.path.join(cwd or '', name))
        if full.replace('\\', '/').startswith('//'):
            raise _Refuse(f'{t} (network path)')  # UNC は開くだけで資格情報を送りうる(\\?\ で始まるパスも、ここで止める)
        key = os.path.normcase(full)
        if key not in memo:
            memo[key] = [t]  # 読んでいる途中にまた出てきたら、循環(ltidx は誤りにして、引数のまま残す)
            try:
                if depth >= RSP_DEPTH:
                    raise _Refuse(f'{t} (response files nested deeper than {RSP_DEPTH})')
                try:
                    text = _rsp_text(full)
                except (FileNotFoundError, NotADirectoryError):
                    text = None  # 無い: ltidx も開けない
                except (OSError, ValueError) as e:
                    if not os.path.isdir(full):  # フォルダは ltidx も開けない。ほかは、読めたら何があるか分からない
                        raise _Refuse(f'{t} (cannot read it: {type(e).__name__})')
                    text = None
                if text is not None:
                    memo[key] = _expand(_win_tokens(text), cwd, memo, depth + 1)
            except _Refuse as e:
                memo[key] = e  # 次に出てきたときも断る
                raise
        if isinstance(memo[key], _Refuse):
            raise memo[key]
        out.extend(memo[key])
    return out


def _scan(args):
    """args(応答ファイルを置き換えた語の並び)に、コードを読ませる・ファイルを書かせる引数があれば、そのわけ。"""
    prev = ''
    for t in args:
        bad = _unsafe_arg(t, prev)
        if bad:
            return bad
        prev = t
    return None


def _clang_cfg(name):
    """name が、clang がコンパイラの隣から読む設定ファイルの名前らしいか: clang.cfg・clang++.cfg・clang-cl.cfg と、
    <triple>.cfg・<triple>-clang++.cfg(triple は --target で変えられる)。- を含まない openssl.cfg などは読まれない。"""
    stem, ext = os.path.splitext(name.lower())
    return ext == '.cfg' and ('clang' in stem or 'flang' in stem or '-' in stem)


def _config_beside(args, cwd, memo):
    """コンパイラ(args の先頭。ccache などの後ろならその次)のフォルダに clang の設定ファイルがあれば、その場所。
    clang は、コンパイラの隣の設定ファイルを、引数に無くても読む【実測: ltidx 0.5.7 で、compile_commands.json の先頭を
    <フォルダ>\\tools\\clang++.exe にすると、実行ファイルが無くても tools\\clang++.cfg の -DMARK が効いた(g++.exe・cl.exe の
    名前でも、ccache の後ろでも同じ)。--target=x86_64-linux-gnu を足すと x86_64-unknown-linux-gnu.cfg を読んだ。
    フォルダの付かない clang++ では、作業フォルダや compile_commands.json の隣の .cfg は読まなかった】"""
    for t in args:
        if os.path.dirname(t):
            if not os.path.isabs(t) and not cwd:
                return f'{t} (relative to a "directory" that is not absolute)'
            d = os.path.dirname(os.path.normpath(os.path.join(cwd or '', t)))
            key = os.path.normcase(d)
            if key not in memo:
                try:
                    memo[key] = next((os.path.join(d, f) for f in os.listdir(d) if _clang_cfg(f)), None)
                except OSError:
                    memo[key] = None  # 無いフォルダ
            if memo[key]:
                return f'{memo[key]} (a clang configuration file next to the compiler {t})'
        stem = os.path.basename(t).lower()
        if (stem[:-4] if stem.endswith('.exe') else stem) not in WRAPPERS:
            break
    return None


def _one_key_each(pairs):
    d = dict(pairs)
    if len(d) != len(pairs):  # ltidx は最初の command を、Python は最後を使う【実測: ltidx 0.5.7】
        raise ValueError('duplicate key')
    return d


def unsafe_cdb(cdb):
    """cdb(compile_commands.json のフォルダ)で ltidx を動かすと、コードを読ませうる・引数をファイルから足させうるなら、
    そのわけ(引数など)。無ければ None。ltidx の読み方に合わせて見て、合わせきれないものは安全でない側に倒す。
    - ltidx は compile_commands.json を YAML として読むので、JSON でない書き方(末尾のカンマ・' の文字列・# のコメント・
      ブロック形式)も受ける【実測: ltidx 0.5.7。どれも Python の json は断り、ltidx は -DMARK を効かせた】。
      厳密な JSON として読めないもの・同じキーが 2 度あるものは、安全でないとする(CMake と make-cdb.ps1 の出力は厳密な JSON)
    - command は Windows のコマンドラインの規則で分ける(_win_tokens)。arguments と command の両方があれば両方を見る
    - 応答ファイルは directory から探して中身に置き換えて見る(_expand)。コンパイラの隣の設定ファイルも見る(_config_beside)"""
    try:
        with open(os.path.join(cdb, 'compile_commands.json'), 'rb') as f:
            data = f.read()
    except OSError:
        return None  # 無い・開けない: ltidx が誤りにする
    try:
        entries = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_one_key_each)
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError・JSONDecodeError も ValueError
        return f'not strict JSON, so it cannot be checked ({type(e).__name__}: {str(e)[:80]})'
    rsp, cfg = {}, {}
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        cwd = e.get('directory') if isinstance(e.get('directory'), str) and os.path.isabs(e['directory']) else None
        lists = []
        if isinstance(e.get('arguments'), list):  # 文字列でない要素は ltidx が誤りにするか、そのまま文字にする
            args = [str(a).split('\0', 1)[0] for a in e['arguments']]  # ltidx は NUL で切る【実測: ltidx 0.5.7】
            lists.append((args, '\n'.join(args)))
        if isinstance(e.get('command'), str):
            lists.append((None, e['command']))
        for args, raw in lists:
            if args is None:  # 語に分けるのは要るところまで(_config_beside は先頭の数語しか見ない)
                args = list(_win_tokens(raw)) if _suspect(raw) else _win_tokens(raw)
            if _suspect(raw):
                try:
                    args = _expand(args, cwd, rsp)  # 応答ファイルの中のコンパイラも、隣の設定ファイルを読む
                except _Refuse as err:
                    return str(err)
                bad = _scan(args)
                if bad:
                    return bad
            bad = _config_beside(args, cwd, cfg)
            if bad:
                return bad
    return None


def _path(d, name):
    return os.path.join(d, name)


def running(d):
    """ほかのプロセスが作り直している最中か。持ち主のいないロックは、ついでに消す。"""
    lock = _path(d, 'update.lock')
    if not os.path.exists(lock):
        return False
    try:
        os.remove(lock)
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def read_state(d):
    try:
        with open(_path(d, 'update.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def start(d, full, trusted_only=False):
    """作り直しを別プロセスで始める。標準出力はプロトコル専用なので、子には渡さない。"""
    flags = 0
    if os.name == 'nt':  # 最低優先度は ltidx・make-cdb にも引き継がれる
        flags = subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), '--dir', d] + (['--full'] if full else [])
                     + (['--trusted-only'] if trusted_only else []),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def wait_done(d, since, timeout):
    """since より後に終わった作り直しの結果。timeout 秒で終わらなければ None。"""
    deadline = time.time() + timeout
    while True:
        st = read_state(d)
        if st and st.get('finished', 0) >= since:
            return st
        if time.time() >= deadline:
            return None
        time.sleep(0.1)


def refresh(idx, paths, root, wait, trusted_only=False):
    """(引く索引, 出力の先頭に足す注記)。索引が古ければ作り直しを始め、wait 秒まで待つ。
    idx は ltq.Index。終わったら作り直した索引を開き直して返す。
    trusted_only なら、この PC で作った記録(trusted_recipe)の索引だけを作り直す(skill から自動で許可された呼び出し)。"""
    import ltq  # server.py が query を sys.path に足してから呼ぶ
    changed, new, missing = idx.freshness()
    if not (changed or new or missing):
        return idx, ''
    if any(recipe_of(p) is None for p in paths):
        return idx, ('auto-update: off for this index (no .recipe.json). Rebuild it once with '
                     f'{os.path.join(SCRIPTS, "index-project.bat")} to turn it on.\n')
    untrusted = [p for p in paths if not trusted_recipe(p)] if trusted_only else []
    if untrusted and not trust_available():
        return idx, ('auto-update: off for this index (LOCALAPPDATA is not set, so indexes built on this PC cannot be '
                     'recorded). Results use the current index (lines re-located).\n')
    if untrusted:
        # 作り直しの記録の構成のまま(-Name・作り元・-Update)。-Root だけだと、既定の構成を最初から作るだけになる。
        # コマンドは 1 行に 1 つ(; でつなぐと cmd では動かない)
        hints = [rebuild_hint(p, root) for p in untrusted]
        cmds = [c for c, _ in hints if c]
        left = sorted({k for _, ks in hints for k in ks})
        if cmds:
            how = 'rebuild it once with these commands, one per line, in PowerShell or cmd (only changed files are parsed)'
            if left or len(cmds) < len(untrusted):
                how += ('; the recorded ' + (', '.join(left) or 'options of some indexes') + ' could not be shown '
                        'safely, so add the options used when it was first built')
            how += ':\n' + '\n'.join(cmds) + '\n'
        else:
            how = (f'rebuild it once with {os.path.join(SCRIPTS, "index-project.bat")}, with the options used when it '
                   'was first built and -Update.\n')
        return idx, ('auto-update: off for this index (it was not built on this PC with index-project.bat or '
                     '/cxxref-index). Results use the current index (lines re-located). To turn it on, ask the user to '
                     + how)
    d = os.path.dirname(os.path.abspath(paths[0]))
    n = len(changed) + len(new) + len(missing)
    asked = time.time()
    if not running(d):
        st = read_state(d)
        src = idx.sources()
        newest = max((src[p][1] for p in changed + new if p in src), default=asked)
        if st and not st.get('ok') and st.get('finished', 0) >= newest:
            # 前回失敗してから何も変わっていない。同じ失敗を毎回繰り返さない
            return idx, (f'auto-update: the last rebuild failed ({st.get("message")}); see {_path(d, "update.log")}. '
                         'Results use the current index. It is retried after the next source change.\n')
        start(d, full=bool(new or missing), trusted_only=trusted_only)
    st = wait_done(d, asked, wait)
    if st is None:
        return idx, (f'auto-update: rebuilding the index in the background for {n} changed files. This result uses '
                     'the current index (lines re-located); a later call uses the new index.\n')
    if not st.get('ok'):
        return idx, (f'auto-update: rebuild failed ({st.get("message")}); see {_path(d, "update.log")}. '
                     'Results use the current index.\n')
    return ltq.Index(paths, root), (f'auto-update: rebuilt the index for {n} changed files '
                                     f'({st["finished"] - st["started"]:.1f} s).\n')


def run_recipe(rec, full, log):
    """1 つの構成を作り直す。戻り値は失敗の説明(成功なら None)。"""
    mk = rec.get('makeCdb')
    if full and mk:
        pwsh = shutil.which('pwsh')
        if not pwsh:
            return 'pwsh not found'
        args = [pwsh, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', MAKE_CDB]
        for k, v in mk.items():
            if v not in (None, ''):
                args += [f'-{k}', str(v)]
        rc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
        if rc != 0:
            return f'make-cdb exit {rc}'
    bad = unsafe_cdb(rec['cdb'])
    if bad:
        return (f'compile_commands.json could make the compiler load code or extra arguments ({bad}); not rebuilt '
                'automatically. Rebuild it with index-project.bat if you trust it')
    out = rec['out']
    ltidx = find_ltidx()
    if not ltidx or not os.path.exists(ltidx):
        return no_ltidx()
    args = [ltidx, '-p', rec['cdb'], '-root', rec['root'], '-o', out, '-j', str(rec.get('jobs') or 1)]
    if os.path.exists(out + '.deps.tsv'):
        args.append('-update')
    rc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
    if rc not in (0, 1):  # 1 は解析にエラーのあった TU がある(索引はできている)
        return f'ltidx exit {rc}'
    if rec.get('alt', True):
        # 差分パス(無効な #if の中の解析)。失敗しても索引はできているので、記録に残すだけ
        log.flush()
        try:
            rc = subprocess.run([sys.executable, '-I', ALTPASS, '--index', out, '--ltidx', ltidx],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
            if rc != 0:
                log.write(f'alt pass: exit {rc}\n')
        except OSError as e:
            log.write(f'alt pass: {e}\n')
    return None


def work(d, full, trusted_only=False):
    lock = _path(d, 'update.lock')
    try:
        f = open(lock, 'x')
    except FileExistsError:
        if running(d):
            return 0
        try:
            f = open(lock, 'x')
        except FileExistsError:
            return 0
    started = time.time()
    errors = []
    with f:
        f.write(str(os.getpid()))
        f.flush()
        indexes = sorted(_path(d, x[:-len(RECIPE)]) for x in os.listdir(d) if x.endswith('.tsv' + RECIPE))
        with open(_path(d, 'update.log'), 'w', encoding='utf-8') as log:
            for index in indexes:
                rec = recipe_of(index)
                if rec is None:
                    errors.append(f'{os.path.basename(index)}: unreadable recipe')
                    continue
                if trusted_only and not trusted_recipe(index):
                    errors.append(f'{os.path.basename(index)}: recipe not built on this PC (skipped)')
                    continue
                log.write(f'== {index}{" (full)" if full else ""}\n')
                log.flush()
                err = run_recipe(rec, full, log)
                if err:
                    errors.append(f'{os.path.basename(index)}: {err}')
                elif os.path.exists(index) and os.path.getmtime(index) < started:
                    # 解析し直す TU が無くても、ここまでは確かめたので索引の時刻を進める(同じ変更で毎回作り直さない)
                    os.utime(index, (started, started))
        state = {'started': started, 'finished': time.time(), 'ok': not errors, 'message': '; '.join(errors)}
        tmp = _path(d, 'update.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as s:
            json.dump(state, s)
        os.replace(tmp, _path(d, 'update.json'))
    try:
        os.remove(lock)
    except OSError:
        pass
    return 0 if not errors else 1


def main():
    ap = argparse.ArgumentParser(prog='autoupdate')
    ap.add_argument('--dir', help='the .index folder')
    ap.add_argument('--full', action='store_true', help='also regenerate compile_commands.json')
    ap.add_argument('--trusted-only', action='store_true', help='rebuild only indexes whose recipe was built on this PC')
    ap.add_argument('--trust', metavar='INDEX', help='record INDEX.recipe.json as built on this PC, and exit')
    a = ap.parse_args()
    if a.trust:
        return 0 if trust_recipe(os.path.abspath(a.trust)) else 1
    if not a.dir:
        ap.error('--dir is required')
    return work(os.path.abspath(a.dir), a.full, a.trusted_only)


if __name__ == '__main__':
    sys.exit(main())
