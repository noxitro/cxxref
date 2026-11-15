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
    などの非 ASCII の空白を区切りに、全角の引用符(‘ ’ “ ” など)を引用符に、– — を - に使うので、それらは受けない
  - "…": 中に " ` $ を含まない(式の展開をさせない)。'…': 中に ' を含まない。どちらも、中は ASCII の印字可能文字と
    上の非 ASCII の文字・数字だけ。空でなく、\\ で終わらない(Windows PowerShell 5.1 などの古い渡し方では、
    中の \\" や "" で語が分かれたり、空の語が消えたりする【実測: 5.1.26100 で "X"" --root C:\\evil" が 3 つの引数になった】)。
    閉じた引用符のすぐ後に文字を続けない
  これで、関数名の operator() や Vec<int> は引用符の中で、日本語のフォルダ名やプラグインの場所は裸でも渡せる
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
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.normcase(os.path.realpath(os.path.join(HERE, '..', 'mcp', 'cxxref_cli.py')))
SHELLS = {'powershell', 'pwsh'}
PYTHON = re.compile(r'(?i)(?:python3?|py)(?:\.exe)?')
_ASCII = {chr(c) for c in range(0x20, 0x7f)}
BARE = set(string.ascii_letters + string.digits + '_.:\\/=+~-')  # 裸の語に使える ASCII
QUOTED = {'"': _ASCII - set('"`$'), "'": _ASCII - {"'"}}  # 引用符の中に使える ASCII


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
                'load the cxxref skill before grepping. When asked to make the change yourself, grep as usual.')


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


# --- grep を refs に振り向ける ---------------------------------------------------------------------------------
# 呼び出し側を直す作業では、呼び出しが多くのファイルに散るときだけ、索引を引くと rg だけより安い【実測: v14e・v14f で
# 呼び出し 16 の問題は 0.71〜0.81 倍、2〜3 ファイルの問題は 1.06〜1.32 倍】。ただ、小さいモデルは grep の結果を見ても
# skill を読まない【実測: v14g で 0/15】。そこで、C++ の名前の grep を見たときに索引で呼び出しのファイル数を数え、
# MANY_FILES 以上なら、その grep を 1 回だけ断り、理由に refs のコマンドをそのまま書く(skill を読む往復も要らない)。
# 同じ名前の grep は、同じセッションでは 2 回目から通す(テキストの一致が要る場面を塞がない)
MANY_FILES = 5
EDIT_TOOLS = {'edit', 'create', 'write', 'str_replace', 'str_replace_editor', 'apply_patch'}
SCAN_LIMIT = 300 << 20  # 目次(.names.cache)が無いとき、頭から読むのはこの大きさまで(hook の制限時間 10 秒に収める)
NAME = re.compile(r'[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*')
ESCAPES = re.compile(r'\\[bBsSwWdDnrtAZz<>]|\\.|\[[^\]]*\]|\(\?[^)]*\)')
SKIP_WORDS = {'class', 'struct', 'enum', 'union', 'void', 'const', 'static', 'virtual', 'override', 'inline',
              'return', 'template', 'typename', 'auto', 'int', 'bool', 'char', 'float', 'double', 'unsigned',
              'define', 'include', 'if', 'else', 'for', 'while', 'switch', 'case', 'new', 'delete'}


def pattern_names(pattern):
    """grep の pattern に書かれた、C++ の名前らしいもの(`\\bWait\\s*\\(` なら Wait、`A::B|class C` なら A::B と C)。"""
    if not isinstance(pattern, str) or len(pattern) > 300:
        return []
    out = []
    for m in NAME.finditer(ESCAPES.sub(' ', pattern)):
        n = m.group(0)
        if n not in out and n not in SKIP_WORDS and len(n.rsplit('::', 1)[-1]) >= 3:
            out.append(n)
    return out[:4]


def _index_lines(path, short):
    """索引 path の、記号の最後の部分が short(bytes)である行の候補(ハッシュの衝突を含む)。"""
    sys.path.insert(0, os.path.join(HERE, '..', 'query'))
    import namedir  # noqa: E402  (hook は呼ぶたびに別のプロセス。sessionStart では読まない)
    nd = namedir.NameDir(path)
    try:
        if nd.open():
            with open(path, 'rb') as f:
                for off in nd.offsets(short):
                    f.seek(off)
                    yield f.readline()
            return
    finally:
        nd.close()
    if os.path.getsize(path) > SCAN_LIMIT:
        return
    with open(path, 'rb') as f:
        data = f.read()
    key = short + b'\tcall'
    i = data.find(key)
    while i >= 0:
        s = data.rfind(b'\n', 0, i) + 1
        e = data.find(b'\n', i)
        yield data[s:e if e >= 0 else len(data)]
        i = data.find(key, e if e >= 0 else len(data))


def call_files(root, name):
    """{記号: 呼び出しのあるファイルの集合}。name は修飾名かその後ろの部分(ltq の refs と同じ合わせ方)。"""
    short = name.rsplit('::', 1)[-1].encode()
    tail = b'::' + name.encode()
    out = {}
    idx = os.path.join(root, '.index')
    for f in sorted(os.listdir(idx)):
        if not f.endswith('.tsv') or f.count('.') != 1:  # 本体だけ(index.tsv.skip.tsv などの付属は除く)
            continue
        for line in _index_lines(os.path.join(idx, f), short):
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


def _state_path(root):
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('TEMP') or HERE
    key = hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode('utf-8')).hexdigest()[:16]
    return os.path.join(base, 'cxxref', 'hook-state', key + '.json')


def _read_state(root):
    try:
        with open(_state_path(root), encoding='utf-8') as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(root, state):
    p = _state_path(root)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f'{p}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f)
        os.replace(tmp, p)
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
    if root is None or not isinstance(args, dict):
        return None
    names = pattern_names(args.get('pattern'))
    if not names:
        return None
    state = _read_state(root)
    if state.get('told') or state.get('edited'):
        # 振り向けはセッションに 1 回だけ。編集の後もしない(索引が古くなり、refs は作り直しを待つ。確かめの grep を塞がない)
        # 【実測: 試しの 1 回で、編集の後の確かめの grep に std::forward_like(37 ファイル)を案内していた】
        return None
    told = set()
    hits = []
    for i, n in enumerate(names):
        if n in told:
            continue
        try:
            found = call_files(root, n)
        except (OSError, ValueError):
            continue
        # 一度案内した記号は、別の書き方の pattern(Wait の後の JobSystem::Wait など)でも 2 回目は案内しない
        hits += [(i, -len(fs), sym, n) for sym, fs in found.items() if len(fs) >= MANY_FILES and sym not in told]
    if not hits:
        return None
    hits.sort()  # pattern に先に書かれた名前、その中では呼び出しのファイルが多い記号から
    state['told'] = sorted(told | {h[3] for h in hits} | {h[2] for h in hits})
    _write_state(root, state)
    lines = [f'{sym} is called from {-k} files' for _, k, sym, _ in hits[:3]]
    sym = hits[0][2]
    return ('cxxref (clang index of every translation unit): ' + '; '.join(lines) + '. Instead of this grep, run once '
            'in PowerShell, as a command of its own:\n'
            f'python -I "{os.path.join(os.path.dirname(HERE), "mcp", "cxxref_cli.py")}" refs {sym} --context 3\n'
            'It lists every call, reference, declaration and definition with the source lines around them; write the '
            'edits from those lines without opening whole files. If you need the text matches too, run the same grep '
            'again (this note is shown once per name).')


def session_start(data):
    """sessionStart: 索引のあるリポジトリでだけ、skill を使う場面を 1 文で知らせる。索引が無ければ何も足さない
    (skill の説明文だけでは、使わないモデルがいた【実測: Claude Haiku 5.5 は grep から始めた。gpt-6-luna は読んだ】)。
    grep を振り向けた名前の記録も、ここで空にする(新しいセッションでは、もう一度 1 回だけ振り向ける)。"""
    root = index_root(data.get('cwd'))
    if root is not None:
        _write_state(root, {})
        sys.stdout.write(json.dumps({'additionalContext': SESSION_NOTE}))
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
    if tool == 'grep':
        try:
            reason = redirect_grep(data)
        except Exception:  # 振り向けは補助。何があっても grep は止めない
            reason = None
        if reason:
            sys.stdout.write(json.dumps({'permissionDecision': 'deny', 'permissionDecisionReason': reason}))
        return 0
    if tool in EDIT_TOOLS:  # 編集が始まったら、振り向けをやめる(許可には何も返さない)
        root = index_root(data.get('cwd'))
        if root is not None:
            state = _read_state(root)
            if not state.get('edited'):
                state['edited'] = True
                _write_state(root, state)
        return 0
    if str(data.get('toolName', '')).lower() not in SHELLS:
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
