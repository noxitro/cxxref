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
import ltq  # noqa: E402
import autoupdate  # noqa: E402

VERSION = '0.5.1'
PROTOCOL = '2025-06-18'
# 結果の大きさの上限の既定。Copilot CLI は 20,480 バイト(COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES)を超える
# ツールの結果をモデルに渡さず、ファイルに退かせて先頭だけを見せる【実測: 1.0.18 の app.js。1.0.105 は native に移り、
# 計測で 21.2 KB 以上が退かされ、18,000 バイト以下は退かされなかった】。少し下回らせる
MAX_BYTES = 18000
FIT_LIMITS = (60, 25, 10, 5, 2)  # 短い形でも上限を超えるときに、順に試す --limit(1 節に並べる行数)

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
            'virtual overrides, unresolved template calls (decide from the line), and text matches of the name that '
            'are not in the index, with the reason (comment, inactive #if, dropped macro argument, local name, macro, '
            'string, file not analyzed). Code in #if branches the build does not compile is included, marked '
            '[inactive: #if ...]. Matches in non-C/C++ files (scripts, build files, docs) are listed too. Lines over the '
            'limit are counted by folder; pass path to list them. Macros (#define and expansions) are symbols too.'),
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
    def __init__(self, root=None, index=None, wait=None, max_bytes=MAX_BYTES):
        self.fixed = os.path.abspath(root) if root else None  # None なら、呼ばれるたびに索引のあるフォルダを探す
        self.index = os.path.abspath(index) if index else None
        self.wait = wait  # 自動更新で待つ秒数。None なら自動更新しない
        self.max_bytes = max_bytes  # 結果の大きさの上限(0 なら切らない)
        self.opened = None  # (鍵, 索引のファイルの状態, ltq.Index)。最後に引いた索引を 1 つだけ保つ
        self.told_ltidx = False  # 取ってきた ltidx が古いことを、もう知らせたか(1 回だけ)

    def open_index(self, paths, root):
        """索引を開く。前の呼び出しと同じ索引で、索引のファイル(本体と隣のファイル)の大きさと更新日時が変わっていなければ、
        前に開いた Index を使い回す(TU の記録と、TU が読んだファイルの一覧を読み直さない。UE5 で 1 回 30 秒ほど)。
        ソースの走査と鮮度の確認は、呼ぶたびにやり直す。"""
        key = (tuple(os.path.normcase(os.path.abspath(p)) for p in paths), os.path.normcase(os.path.abspath(root)))
        sig = ltq.index_signature(paths)
        if self.opened and self.opened[0] == key and self.opened[1] == sig:
            idx = self.opened[2]
            idx.renew()
            return idx
        idx = ltq.Index(paths, root)
        self.opened = (key, sig, idx)
        return idx

    def keep(self, paths, root, idx):
        """自動更新で開き直した索引を、次の呼び出しで使い回せるように保つ。"""
        key = (tuple(os.path.normcase(os.path.abspath(p)) for p in paths), os.path.normcase(os.path.abspath(root)))
        self.opened = (key, ltq.index_signature(paths), idx)

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

    def run(extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = ltq.main(base + args + extra, idx=idx)  # idx が None なら ltq が探して、無ければ誤りを書く
            except SystemExit as e:  # argparse の誤り
                code = e.code or 2
        return code, note + out.getvalue()

    code, text = run([])
    # クライアントは大きな結果をモデルに渡さない(Copilot CLI は 20,480 バイトを超えるとファイルに退かせ、先頭だけを見せる)。
    # 超えたら、報告しない節(コメント・文字列など)を件数にまとめた短い形で出し直す。それでも超えたら、無効な #if の中や
    # マクロの定義の中の一致もフォルダごとの件数にし(どのフォルダに何件あるかは残り、path で 1 行ずつ出し直せる)、
    # それでも超えたら末尾を切る
    # それでも超えたら(解決済みの行だけで数百行ある名前)、節ごとの行数を減らす。省いた行はフォルダごとの件数で残る
    # (呼び出し側が渡した limit より小さい値だけを試す。後ろに足した --limit が勝つ)
    asked = int(args[args.index('--limit') + 1]) if '--limit' in args else 200
    for extra in (['--compact'], ['--compact'] * 2,
                  *(['--compact'] * 2 + ['--limit', str(n)] for n in FIT_LIMITS if n < asked)):
        if cfg.max_bytes and len(text.encode('utf-8')) > cfg.max_bytes and args[0] == 'refs':
            code, text = run(extra)
    if cfg.max_bytes and len(text.encode('utf-8')) > cfg.max_bytes:
        cut = text.encode('utf-8')[:cfg.max_bytes - 300].decode('utf-8', 'ignore')
        text = cut[:cut.rfind('\n') + 1] + (f'... output cut at {cfg.max_bytes} bytes (the client limit). Ask for a '
                                            'narrower name, or pass path / exclude / limit to see the rest.\n')
    return code, text


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
    a = ap.parse_args()
    cfg = Config(a.root, a.index, None if a.no_auto_update else max(0.0, a.auto_update_wait), max(0, a.max_output_bytes))
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
