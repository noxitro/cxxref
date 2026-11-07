"""条件 B・D の前に、clangd の索引(<作業フォルダ>\\.cdb\\.cache\\clangd\\index)をそろえ、そろったかを確かめる。

clangd を lsp.json と同じ引数で起動し、ファイルを 1 つ開いて(開くまで clangd は compile_commands.json を
読まない)、索引の進み具合($/progress の indexing)が終わるまで待つ。索引し直した TU の数を出す。
0 件なら準備済み(Copilot が起動する clangd も、ファイルを開いてすぐに全体の索引を引ける)。

使い方: python -I clangd_warm.py <clangd.exe> <作業フォルダ> [開くファイル(既定 runtime/kernel/job_system.h)] [--timeout 秒]
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

LSP_JSON = pathlib.Path(__file__).with_name('lsp.json')


def main():
    a = sys.argv[1:]
    timeout = 1800.0
    if '--timeout' in a:
        i = a.index('--timeout')
        timeout = float(a[i + 1])
        del a[i:i + 2]
    exe, ws = a[0], os.path.abspath(a[1])
    rel = a[2] if len(a) > 2 else 'runtime/kernel/job_system.h'
    args = json.loads(LSP_JSON.read_text(encoding='utf-8'))['lspServers']['clangd']['args']
    p = subprocess.Popen([exe] + args, cwd=ws, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    t0 = time.time()
    state = {'total': 0, 'done': None, 'begin': None}

    def send(m):
        b = json.dumps(m).encode()
        p.stdin.write(b'Content-Length: %d\r\n\r\n' % len(b) + b)
        p.stdin.flush()

    def pump():
        f = p.stdout
        while True:
            hdr = b''
            while not hdr.endswith(b'\r\n\r\n'):
                c = f.read(1)
                if not c:
                    return
                hdr += c
            n = int([h for h in hdr.decode().split('\r\n') if h.lower().startswith('content-length')][0].split(':')[1])
            m = json.loads(f.read(n))
            if m.get('method') == 'window/workDoneProgress/create':
                send({'jsonrpc': '2.0', 'id': m['id'], 'result': None})
            elif m.get('method') == '$/progress':
                v = m['params']['value']
                if v.get('kind') == 'begin':
                    state['begin'] = time.time()
                mm = re.match(r'\s*(\d+)/(\d+)', v.get('message') or '')
                if mm:
                    state['total'] = max(state['total'], int(mm.group(2)))
                if v.get('kind') == 'end':
                    state['done'] = time.time()

    threading.Thread(target=pump, daemon=True).start()
    send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'processId': os.getpid(), 'rootUri': pathlib.Path(ws).as_uri(),
        'capabilities': {'window': {'workDoneProgress': True}}}})
    time.sleep(1)
    send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
    text = pathlib.Path(ws, rel).read_text(encoding='utf-8', errors='replace')
    send({'jsonrpc': '2.0', 'method': 'textDocument/didOpen', 'params': {'textDocument': {
        'uri': pathlib.Path(ws, rel).resolve().as_uri(), 'languageId': 'cpp', 'version': 1, 'text': text}}})
    while state['done'] is None and time.time() - t0 < timeout:
        time.sleep(0.5)
    p.kill()
    if state['done'] is None:
        print(f'timeout after {timeout:.0f}s (indexing {state["total"]} TUs, not finished)')
        return 1
    # 進み具合の分母は、キャッシュに無い(索引し直す)TU の数。「0/1」だけなら標準ライブラリの分だけ
    redo = 0 if state['total'] <= 1 else state['total']
    print(f'indexing finished in {state["done"] - (state["begin"] or t0):.1f}s; re-indexed TUs: {redo}'
          + (' (ready)' if redo == 0 else ' (run again to confirm)'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
