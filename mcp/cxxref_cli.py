"""cxxref の問い合わせを、MCP サーバーを通さずに 1 回だけ実行する(skill から shell で呼ぶ入口)。

  python -I cxxref_cli.py refs <名前> [--path P]... [--exclude X]... [--limit N] [--context N] [--root R]
  python -I cxxref_cli.py check <path:line>... [--root R]

中身は MCP サーバー(server.py)の refs / check と同じ: 索引のあるフォルダを作業フォルダから親へさかのぼって探し、
索引の後にソースが変わっていれば裏で作り直して待ち(既定 120 秒。CRI_AUTO_UPDATE_WAIT で変えられる)、
結果がクライアントの上限(18,000 バイト)を超えるなら短い形にして、並べきれなかった行を一覧のファイルに書く。

MCP サーバーとの違い
- 呼ぶたびにプロセスを起こすので、メモリには何も保てない。ディスクの写し(名前の目次 <索引>.names.cache と、単語のフィルタ
  <索引>.words.cache)は MCP サーバーと同じものを使い、無ければ・古ければ作る(その回だけ遅い)。写しを使わないと、
  大きな索引では 1 回が数分かかる【実測: UE5(索引 12 GB)で 41〜210 秒 → 8〜21 秒、O3DE で 3.7 秒 → 1.2 秒】
- 自動で作り直すのは、この PC で index-project(/cxxref-index)が作った索引だけ(作り直しの記録の指紋を
  %LOCALAPPDATA%\\cxxref\\trusted に登録したもの)。skill からの呼び出しは hook が許可を自動で出すため
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
    # 省略形(--roo など)を受けない。hook が許可するかを決める形と、ここで受ける形を揃える(scripts/cxxref_hook.py)
    ap = argparse.ArgumentParser(prog='cxxref', allow_abbrev=False)
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('refs', allow_abbrev=False,
                       help='calls, references, definitions and the text matches left out, for a C++ symbol')
    r.add_argument('name', help='qualified name or its suffix, e.g. ns::File::Read or File::Read')
    r.add_argument('--path', action='append', default=[], help='list only lines under this root-relative folder or file')
    r.add_argument('--exclude', action='append', default=[], help='folder or root-relative prefix to skip in the text search')
    r.add_argument('--limit', type=int, help='max lines per section (default 200)')
    r.add_argument('--context', type=int,
                   help='also print N (up to 10) source lines around each call, reference and declaration, when they are in '
                        '5 or more files and the result stays under the size limit (for editing callers)')
    c = sub.add_parser('check', allow_abbrev=False, help='what the index has on each path:line[:col]')
    c.add_argument('locations', nargs='+')
    for p in (r, c):
        p.add_argument('--root', help='working folder (default: the current folder)')
    a = ap.parse_args(argv)

    try:
        wait = float(os.environ.get('CRI_AUTO_UPDATE_WAIT', WAIT))
    except ValueError:
        wait = WAIT
    cfg = server.Config(wait=None if os.environ.get('CRI_AUTO_UPDATE') == '0' else max(0.0, wait), watch=False)
    # skill からの呼び出しは hook が自動で許可するので、この PC で作った索引だけを自動で作り直す(autoupdate.trusted_recipe)
    cfg.trusted_only = True
    if a.cmd == 'refs':
        args = {'name': a.name, 'path': a.path, 'exclude': a.exclude}
        if a.limit:
            args['limit'] = a.limit
        if a.context:
            args['context'] = a.context
    else:
        args = {'locations': a.locations}
    if a.root:
        args['root'] = a.root
    try:
        code, text = server.call_tool(cfg, a.cmd, args)
    except Exception as e:  # 索引の読み込みの失敗など。MCP サーバー(server.handle)と同じく 1 行の誤りにする
        code, text = 1, f'{type(e).__name__}: {e}\nSearch with rg as usual.\n'
    finally:
        cfg.close()  # 目次の写しを開いたままにしない
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
