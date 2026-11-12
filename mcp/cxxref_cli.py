"""cxxref の問い合わせを、MCP サーバーを通さずに 1 回だけ実行する(skill から shell で呼ぶ入口)。

  python -I cxxref_cli.py refs <名前> [--path P]... [--exclude X]... [--limit N] [--root R]
  python -I cxxref_cli.py check <path:line>... [--root R]

中身は MCP サーバー(server.py)の refs / check と同じ: 索引のあるフォルダを作業フォルダから親へさかのぼって探し、
索引の後にソースが変わっていれば裏で作り直して待ち(既定 120 秒。CRI_AUTO_UPDATE_WAIT で変えられる)、
結果がクライアントの上限(18,000 バイト)を超えるなら短い形にして、並べきれなかった行を一覧のファイルに書く。

MCP サーバーとの違い
- 呼ぶたびに索引を開き直す(呼び出しをまたいで保たない)。大きな索引では 1 回が遅くなる
- 自動で作り直すのは、この PC で index-project(/cxxref-index)が作った索引だけ(作り直しの記録の指紋を
  %LOCALAPPDATA%\\cxxref\\trusted-recipes.json に登録したもの)。skill からの呼び出しは hook が許可を自動で出すため
- 作り直しを長く待つ。古い索引で答えると、その注記を見たモデルが確かめ直しの検索を足すことがあるため。時間より、
  モデルに送る量を減らすことを優先する
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I ではスクリプトのフォルダが入らない
import server  # noqa: E402

WAIT = 120.0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='cxxref')
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('refs', help='calls, references, definitions and the text matches left out, for a C++ symbol')
    r.add_argument('name', help='qualified name or its suffix, e.g. ns::File::Read or File::Read')
    r.add_argument('--path', action='append', default=[], help='list only lines under this root-relative folder or file')
    r.add_argument('--exclude', action='append', default=[], help='folder or root-relative prefix to skip in the text search')
    r.add_argument('--limit', type=int, help='max lines per section (default 200)')
    c = sub.add_parser('check', help='what the index has on each path:line[:col]')
    c.add_argument('locations', nargs='+')
    for p in (r, c):
        p.add_argument('--root', help='working folder (default: the current folder)')
    a = ap.parse_args(argv)

    try:
        wait = float(os.environ.get('CRI_AUTO_UPDATE_WAIT', WAIT))
    except ValueError:
        wait = WAIT
    cfg = server.Config(wait=None if os.environ.get('CRI_AUTO_UPDATE') == '0' else max(0.0, wait), resident=False)
    # skill からの呼び出しは hook が自動で許可するので、この PC で作った索引だけを自動で作り直す(autoupdate.trusted_recipe)
    cfg.trusted_only = True
    if a.cmd == 'refs':
        args = {'name': a.name, 'path': a.path, 'exclude': a.exclude}
        if a.limit:
            args['limit'] = a.limit
    else:
        args = {'locations': a.locations}
    if a.root:
        args['root'] = a.root
    code, text = server.call_tool(cfg, a.cmd, args)
    out = io_utf8()
    out.write(text)
    out.flush()
    return code


def io_utf8():
    """標準出力を UTF-8 で書く(shell の既定のコードページで日本語のパスが化けないように)。"""
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    return sys.stdout


if __name__ == '__main__':
    sys.exit(main())
