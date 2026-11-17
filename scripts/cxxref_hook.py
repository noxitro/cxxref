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
import json
import os
import re
import string
import sys
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
                'load the cxxref skill before grepping. When asked to make the change yourself, grep as usual. '
                'Answer in the language of the question.')
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


def has_index(cwd):
    """cwd かその親に、索引(.index の中の .tsv)があるか(ltq.find_root と同じ探し方)。"""
    d = os.path.abspath(cwd) if isinstance(cwd, str) and cwd else None
    while d:
        idx = os.path.join(d, '.index')
        if os.path.isdir(idx):
            try:
                if any(f.endswith('.tsv') for f in os.listdir(idx)):
                    return True
            except OSError:
                return False
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent
    return False


def session_start(data):
    """sessionStart: 索引のあるリポジトリでだけ、索引を引く場面と、そのコマンドを知らせる。索引が無ければ何も足さない
    (skill の説明文だけでは、使わないモデルがいた【実測: Claude Haiku 5.5 は grep から始めた。gpt-6-luna は読んだ】)。"""
    if has_index(data.get('cwd')):
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
