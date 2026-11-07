"""条件 B・D の LSP の記録(runs\\<名前>.lsp.log。lsp_log_proxy.py が書く)を 1 回 1 行にまとめる。

サーバーが起動したか(initialize の応答)、Copilot が送った要求ごとの件数(0 件の応答の数)、ファイルを最初に開いた時刻、
索引し直しが走ったか($/progress の indexing の分母)を出す。

使い方: python -I lsp_summary.py [round]   (round を省くとすべて)
"""
import collections
import os
import re
import sys

bench = os.path.dirname(os.path.abspath(__file__))
runs = os.path.join(bench, 'runs')
want = sys.argv[1] if len(sys.argv) > 1 else None
LINE = re.compile(r'\s*([\d.]+) (C>S|S>C) (\S+)(.*)')

print('\t'.join(['name', 'init', 'requests', 'empty', 'first_open_s', 'reindex', 'detail']))
for fn in sorted(os.listdir(runs)):
    if not fn.endswith('.lsp.log') or (want and not fn.startswith(want + '-')):
        continue
    init = False
    req = {}             # id -> method
    per = collections.Counter()
    empty = collections.Counter()
    first_open = None
    reindex = 0
    for line in open(os.path.join(runs, fn), encoding='utf-8'):
        m = LINE.match(line)
        if not m:
            continue
        t, d, meth, rest = float(m.group(1)), m.group(2), m.group(3), m.group(4)
        mid = re.search(r'id=(\d+)', rest)
        if d == 'C>S' and meth not in ('response',) and mid:
            req[mid.group(1)] = meth
        if d == 'C>S' and meth == 'textDocument/didOpen' and first_open is None:
            first_open = t
        if d == 'S>C' and meth == 'response' and mid and mid.group(1) in req:
            k = req.pop(mid.group(1))
            if k == 'initialize':
                init = True
            elif k not in ('shutdown',):
                per[k] += 1
                if re.search(r'result=(0|null)\b', rest):
                    empty[k] += 1
        if d == 'S>C' and meth == '$/progress':
            mm = re.search(r'(\d+)/(\d+)', rest)
            if mm and int(mm.group(2)) > 1:
                reindex = max(reindex, int(mm.group(2)))
    detail = ', '.join(f'{k.split("/")[-1]}={per[k]}(empty {empty[k]})' for k in sorted(per))
    print('\t'.join([fn[:-len('.lsp.log')], 'ok' if init else 'NG', str(sum(per.values())), str(sum(empty.values())),
                     '-' if first_open is None else f'{first_open:.1f}', str(reindex), detail]))
