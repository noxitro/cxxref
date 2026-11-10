"""索引の自動更新の計測(Copilot を使わない)。MCP サーバーを直接起動し、問題の edit を当ててから refs を呼ぶ。

  python -I autoupdate_bench.py --workspace <作業フォルダ> --index <索引.tsv> [--cdb <フォルダ>] [--reps N] [--out <tsv>] <問題 ID>=<問い合わせ> ...

問題は local/tasks-a.json(edit と truth。公開しない)から読む。条件ごと(auto: 自動更新あり / off: --no-auto-update)に、
  1. 作業フォルダの .index に索引と作り方の記録を置き直す(run.ps1 の条件 F・G と同じ)
  2. edit を当てる(edit の無い問題は、ソースを変えない場合の基準)
  3. サーバーを起動して refs を呼び、応答の時間と、calls の節が正解と同じかを記録する
  4. 作り直しが裏に回ったら、5 秒ごとに呼び直し、新しい索引で答えるまでの時間を記録する(最大 --settle 秒)
  5. edit を戻し(中身と更新日時)、裏の作り直しが終わるのを待って .index を消す
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, '..', 'mcp', 'server.py')
CALL = re.compile(r'^  (call\S*)\s+(\S+):(\d+)')


def prepare(ws, index, cdb):
    d = os.path.join(ws, '.index')
    wait_lock(d)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    for s in ('', '.tu.tsv', '.skip.tsv', '.deps.tsv', '.own.bin'):
        if os.path.exists(index + s):
            shutil.copy2(index + s, os.path.join(d, 'index.tsv' + s))  # 更新日時を保つ(古さの判定に使う)
    rec = {'version': 1, 'root': ws, 'cdb': cdb, 'out': os.path.join(d, 'index.tsv'),
           'jobs': max(1, (os.cpu_count() or 2) // 2), 'makeCdb': None}
    with open(os.path.join(d, 'index.tsv.recipe.json'), 'w', encoding='utf-8') as f:
        json.dump(rec, f)


def wait_lock(d, timeout=900):
    lock = os.path.join(d, 'update.lock')
    end = time.time() + timeout
    while os.path.exists(lock) and time.time() < end:
        try:
            os.remove(lock)  # 開いたままなら作り直しの最中(Windows では消せない)
        except OSError:
            time.sleep(0.5)


def apply_edits(ws, edits):
    saved = []
    for e in edits:
        fp = os.path.join(ws, e['file'])
        with open(fp, 'rb') as f:
            data = f.read()
        saved.append((fp, data, os.stat(fp).st_mtime_ns))
        bom = data.startswith(b'\xef\xbb\xbf')
        text = data[3 if bom else 0:].decode('utf-8')
        eol = '\r\n' if '\r\n' in text else '\n'
        lines = re.split(r'\r?\n', text)
        if e.get('delete'):
            del lines[e['line'] - 1]
        else:
            lines.insert(e['line'] - 1, e['insert'])
        with open(fp, 'wb') as f:
            f.write((b'\xef\xbb\xbf' if bom else b'') + eol.join(lines).encode('utf-8'))
    return saved


def restore(saved):
    for fp, data, mtime in reversed(saved):
        with open(fp, 'wb') as f:
            f.write(data)
        os.utime(fp, ns=(mtime, mtime))


class Server:
    def __init__(self, ws, auto):
        args = [sys.executable, '-I', SERVER] + ([] if auto else ['--no-auto-update'])
        self.p = subprocess.Popen(args, cwd=ws, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.n = 0
        self.call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'bench', 'version': '0'}})

    def call(self, method, params):
        self.n += 1
        self.p.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': self.n, 'method': method, 'params': params}) + '\n').encode())
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def refs(self, name):
        t0 = time.time()
        r = self.call('tools/call', {'name': 'refs', 'arguments': {'name': name}})
        return time.time() - t0, r['result']['content'][0]['text']

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=30)


def calls_of(text, ignore=()):
    out, on = set(), False
    for line in text.splitlines():
        if line.startswith('== calls'):
            on = True
            continue
        if on:
            m = CALL.match(line)
            if not m:
                break
            if not m.group(2).startswith(tuple(ignore)):
                out.add(f'{m.group(2)}:{m.group(3)}')
    return out


def state_of(text):
    for k in ('rebuilt the index', 'rebuilding the index in the background', 'rebuild failed', 'auto-update: off'):
        if k in text:
            return k.split(' the index')[0].replace('auto-update: ', '')
    return 'stale' if 'changed after indexing' in text else 'fresh'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--index', required=True)
    ap.add_argument('--cdb')
    ap.add_argument('--tasks', default=os.path.join(HERE, 'local', 'tasks-a.json'))
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--settle', type=float, default=600)
    ap.add_argument('--conds', default='auto,off')
    ap.add_argument('--out')
    ap.add_argument('--ignore', action='append', default=[],
                    help='path prefix the answer format excludes (e.g. generated files)')
    ap.add_argument('queries', nargs='+', help='<task id>=<qualified name>')
    a = ap.parse_args()
    ws = os.path.abspath(a.workspace)
    cdb = a.cdb or os.path.join(ws, '.cdb')
    tasks = {t['id']: t for t in json.load(open(a.tasks, encoding='utf-8'))}
    rows = []
    for q in a.queries:
        tid, name = q.split('=', 1)
        t = tasks[tid]
        truth = set(t['truth'])
        for cond in a.conds.split(','):
            for rep in range(1, a.reps + 1):
                prepare(ws, os.path.abspath(a.index), cdb)
                saved = apply_edits(ws, t.get('edit') or [])
                try:
                    srv = Server(ws, cond == 'auto')
                    sec, text = srv.refs(name)
                    first = state_of(text)
                    got = calls_of(text, a.ignore)
                    row = {'task': tid, 'cond': cond, 'rep': rep, 'first_sec': round(sec, 2), 'first_state': first,
                           'first_correct': int(got == truth), 'first_missed': ' '.join(sorted(truth - got)),
                           'first_extra': ' '.join(sorted(got - truth)), 'settle_sec': '', 'settle_correct': ''}
                    if first == 'rebuilding':
                        t0 = time.time() - sec
                        while time.time() - t0 < a.settle:
                            time.sleep(5)
                            s2, text = srv.refs(name)
                            if state_of(text) not in ('rebuilding',):
                                break
                        row['settle_sec'] = round(time.time() - t0, 1)
                        row['settle_state'] = state_of(text)
                        row['settle_correct'] = int(calls_of(text, a.ignore) == truth)
                    srv.close()
                finally:
                    restore(saved)
                    wait_lock(os.path.join(ws, '.index'))
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    shutil.rmtree(os.path.join(ws, '.index'), ignore_errors=True)
    if a.out:
        keys = ['task', 'cond', 'rep', 'first_sec', 'first_state', 'first_correct', 'first_missed', 'first_extra',
                'settle_sec', 'settle_state', 'settle_correct']
        with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\t'.join(keys) + '\n')
            for r in rows:
                f.write('\t'.join(str(r.get(k, '')) for k in keys) + '\n')


if __name__ == '__main__':
    main()
