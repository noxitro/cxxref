"""Copilot CLI の preToolUse hook(プラグインの com.github.copilot/hooks/hooks.json から呼ばれる)。

skill の cxxref は、プラグインの mcp/cxxref_cli.py を shell で呼ぶ。プラグインは作業フォルダの外
(~/.copilot/installed-plugins)にあるので、Copilot CLI は呼ぶたびに許可を求め、確認できない場面(copilot -p)では断る
【実測: 1.0.109。断られたモデルは rg で探し直した】。この hook は、このプラグイン自身の cxxref_cli.py を refs か check で
呼ぶだけのコマンドに限って、許可を返す。hook が allow を返すと、Copilot はその呼び出しの許可の確認(作業フォルダの外の
パスの確認も)を飛ばす【実測: 1.0.109 の本体】。そのため、形を狭く決めて、それ以外には何も返さない(ふだんの確認に任せる)。

許す形(PowerShell の 1 行だけ)
  python[.exe] -I "<このプラグインの mcp\\cxxref_cli.py の絶対パス>" refs|check <引数>...
- コマンド全体が ASCII の印字可能文字だけ。PowerShell は NBSP・全角空白などの非 ASCII の空白も区切りに使うので、
  それを含むと、ここでの分け方と PowerShell の分け方が食い違う
- 先頭は裸の python / python.exe / py(フォルダ付きは許さない。.\\python などは別の実行ファイルを動かせる)
- 次は -I(必須。ほかのフラグは受けない)、その次は実体がこのプラグインの cxxref_cli.py である絶対パス
- shell の記号(; & | < > ` $ ( ) { } % ^ @ #)を含まない。--root と、\\\\ や // で始まる引数(UNC)を含まない
  (--root で別の場所の索引と作り直しの記録を読ませない。UNC は開くだけで資格情報を送りうる)
- 道具は powershell / pwsh だけ(ほかの shell は字句の規則が違う)

自動で許可した呼び出しでは、CLI は、この PC で index-project(/cxxref-index も同じ)が作った索引だけを自動で作り直す
(mcp/autoupdate.py の trusted_recipe。細工したリポジトリに置かれた作り直しの記録で ltidx や make-cdb を動かさない)。
"""
import json
import os
import re
import shlex
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.normcase(os.path.realpath(os.path.join(HERE, '..', 'mcp', 'cxxref_cli.py')))
SHELLS = {'powershell', 'pwsh'}
PRINTABLE = re.compile(r'[\x20-\x7e]+')
UNSAFE = re.compile(r'[;&|<>`$(){}%^@#]')
PYTHON = re.compile(r'(?i)(?:python3?|py)(?:\.exe)?')


def allowed(command):
    """command が、このプラグインの cxxref_cli.py を refs / check で呼ぶだけなら True。"""
    if not isinstance(command, str) or not PRINTABLE.fullmatch(command) or UNSAFE.search(command):
        return False
    try:
        words = shlex.split(command.replace('\\', '/'), posix=True)
    except ValueError:
        return False
    if len(words) < 4 or not PYTHON.fullmatch(words[0]) or words[1] != '-I':
        return False
    script, sub, rest = words[2], words[3], words[4:]
    if not os.path.isabs(script) or script.startswith('//'):
        return False
    if os.path.normcase(os.path.realpath(script)) != CLI or sub not in ('refs', 'check'):
        return False
    return not any(w == '--root' or w.startswith('--root=') or w.startswith('//') for w in rest)


SESSION_NOTE = ('This repository has a cxxref index (.index, built by clang over every C++ translation unit). '
                'When a task needs where a C++ symbol is used or called (impact of a change, all callers before '
                'changing a signature), load the cxxref skill before grepping.')


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
    """sessionStart: 索引のあるリポジトリでだけ、skill を使う場面を 1 文で知らせる。索引が無ければ何も足さない
    (skill の説明文だけでは、使わないモデルがいた【実測: Claude Haiku 5.5 は grep から始めた。gpt-6-luna は読んだ】)。"""
    if has_index(data.get('cwd')):
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
