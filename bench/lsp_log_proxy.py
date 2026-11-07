"""LSP のやり取りを記録する中継。本物のサーバーを子プロセスで起動し、メッセージはそのまま通す。

条件 B・D で run.ps1 が .github/lsp.json の command に入れる(Copilot CLI の公式の設定の形は変えない)。
記録するのは、向き(C>S: Copilot → サーバー、S>C: その逆)・起動からの秒・メソッド・id・短い要約。
サーバーが動いていたか、Copilot がいつファイルを開いたか、返った件数を、実行の後に確かめるためのもの。

使い方: python -I lsp_log_proxy.py <記録のファイル> <サーバー> [サーバーの引数...]
"""
import json
import subprocess
import sys
import threading
import time

log_path, server = sys.argv[1], sys.argv[2:]
t0 = time.time()
lk = threading.Lock()
logf = open(log_path, 'a', encoding='utf-8')


def log(s):
    with lk:
        logf.write(f'{time.time() - t0:8.2f} {s}\n')
        logf.flush()


def summarize(m):
    meth = m.get('method')
    if meth == '$/progress':
        v = m['params']['value']
        return f"$/progress {v.get('kind')} {v.get('title', '')} {v.get('message', '')}"
    if meth is None:
        r = m.get('result')
        n = len(r) if isinstance(r, list) else ('null' if r is None else type(r).__name__)
        err = m.get('error')
        return f"response id={m.get('id')} result={n}" + (f' error={err}' if err else '')
    p = m.get('params') or {}
    extra = ''
    td = (p.get('textDocument') or {}).get('uri', '') if isinstance(p, dict) else ''
    if td:
        extra += ' ' + td.rsplit('/', 1)[-1]
    if isinstance(p, dict) and 'position' in p:
        extra += f" @{p['position']}"
    if isinstance(p, dict) and 'query' in p:
        extra += f" q={p['query']!r}"
    if meth == 'initialize':
        extra += ' rootUri=' + str(p.get('rootUri')) + ' workspaceFolders=' + json.dumps(p.get('workspaceFolders'))[:200]
    return f"{meth} id={m.get('id')}{extra}"


def read_msg(f):
    hdr = b''
    while not hdr.endswith(b'\r\n\r\n'):
        c = f.read(1)
        if not c:
            return None, None
        hdr += c
    n = int([h for h in hdr.decode().split('\r\n') if h.lower().startswith('content-length')][0].split(':')[1])
    return hdr, f.read(n)


p = subprocess.Popen(server, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
log('start ' + ' '.join(server))


def client_to_server():
    i = sys.stdin.buffer
    while True:
        hdr, body = read_msg(i)
        if hdr is None:
            break
        try:
            log('C>S ' + summarize(json.loads(body)))
        except Exception as e:  # 記録に失敗しても中継は止めない
            log(f'C>S ? {e}')
        p.stdin.write(hdr + body)
        p.stdin.flush()
    log('client closed')
    try:
        p.stdin.close()
    except OSError:
        pass


threading.Thread(target=client_to_server, daemon=True).start()
o = sys.stdout.buffer
reports = 0
while True:
    hdr, body = read_msg(p.stdout)
    if hdr is None:
        break
    try:
        m = json.loads(body)
        # 索引の進み具合(report)は多いので、50 件に 1 件だけ書く
        if m.get('method') == '$/progress' and m['params']['value'].get('kind') == 'report':
            reports += 1
            if reports % 50 == 1:
                log(f'S>C {summarize(m)} (#{reports})')
        else:
            log('S>C ' + summarize(m))
    except Exception as e:
        log(f'S>C ? {e}')
    o.write(hdr + body)
    o.flush()
log('server closed')
