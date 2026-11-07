"""v13 の計測: Copilot CLI に 1 問を、既定の道具一式(編集と shell を含む)で解かせ、記録を集める。

usage: python -I run13.py --tasks v13/tasks-stage1.json --task S1-req-usage --cond P --rep 1 [--round v13]
       [--model gpt-6-luna] [--provider anthropic] [--dry-run]

条件
  A: プラグインなし。普段の設定(~/.copilot)は使わず、空の COPILOT_HOME で動かす(全体に登録した MCP などを混ぜない)
  P: 公開中のプラグイン cxxref をマーケットプレイスから入れた COPILOT_HOME(製品のまま。rule あり)
  M: A に、このリポジトリの MCP サーバーだけを --additional-mcp-config で足す(rule なし)
  X: P に、cxxref と食い違うユーザーの指示(v13/adverse-instructions.md)を作業フォルダの .github/copilot-instructions.md に足す

作業フォルダは git で管理した写し。実行の前と後に、追跡しているファイルを戻し、増えたファイルを消す(git checkout と git clean)。
回答の後の差分は runs13/<名前>.diff に残す。採点は score13.py。
"""
import argparse, json, os, shutil, sqlite3, subprocess, sys, time
from datetime import datetime, timezone

BENCH = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BENCH)
DATA = os.environ.get('CRI_BENCH_DATA', r'E:\dev\bench-data\cxxref')
V13 = os.path.join(DATA, 'v13')
RUNS = os.path.join(BENCH, 'runs13')

# 写しの置き場と、C++ のものは索引(ltidx の出力)と compile_commands.json のフォルダ
WORKSPACES = {
    'nox': {'path': os.path.join(DATA, 'nox-20261103'), 'index': os.path.join(DATA, 'index', 'nox-v6.tsv'), 'cdb': '.cdb',
            'clean': ['.github', '.mcp.json', '.serena']},  # 前の計測が置いた設定(元の Nox には無い)
    'requests': {'path': os.path.join(V13, 'requests')},
    'newtonsoft': {'path': os.path.join(V13, 'newtonsoft')},
    'zod': {'path': os.path.join(V13, 'zod')},
    'nox-noindex': {'path': os.path.join(DATA, 'nox-20261103'), 'clean': ['.github', '.mcp.json', '.serena']},
}
LTIDX = os.path.join(V13, 'ltidx-0.5.1', 'ltidx.exe')
# copilot は .exe を直接起動する(npm の copilot.CMD を経ると、cmd が問題文の記号を解釈しうる)
COPILOT = os.environ.get('CRI_COPILOT') or shutil.which('copilot.exe') or os.path.join(
    os.environ.get('LOCALAPPDATA', ''), r'Microsoft\WinGet\Packages\GitHub.Copilot_Microsoft.Winget.Source_8wekyb3d8bbwe\copilot.exe')
# 依存物を取ってくる・ビルドする shell のコマンドは、どの条件でも禁じる(時間と CPU の上限を揃えるため。ネットワークは
# Copilot CLI の既定で URL の許可が無いので使えない)
DENY = ['npm', 'npx', 'pnpm', 'yarn', 'bun', 'pip', 'pip3', 'uv', 'poetry', 'dotnet', 'nuget', 'msbuild', 'cmake', 'ninja',
        'cl', 'clang-cl', 'devenv', 'curl', 'wget', 'Invoke-WebRequest', 'iwr', 'Invoke-RestMethod', 'irm',
        'git push', 'git clone', 'git fetch', 'git pull']


def git(ws, *a, check=True):
    return subprocess.run(['git', '-C', ws] + list(a), capture_output=True, text=True, encoding='utf-8', errors='replace', check=check).stdout


def reset_ws(w):
    ws = w['path']
    for p in w.get('clean', []):
        fp = os.path.join(ws, p)
        if os.path.isdir(fp):
            shutil.rmtree(fp)
        elif os.path.exists(fp):
            os.remove(fp)
    git(ws, 'checkout', '-q', '--', '.')
    git(ws, 'clean', '-q', '-fd')  # 増えたファイルだけ消す(-x なし: .index-keep・依存物など無視しているものは残す)
    if git(ws, 'status', '--porcelain'):
        raise SystemExit(f'workspace not clean: {ws}\n' + git(ws, 'status', '--porcelain')[:2000])


def index_sig(base):
    parts = [s for s in ('', '.tu.tsv', '.skip.tsv', '.deps.tsv', '.own.bin') if os.path.exists(base + s)]
    return '|'.join(f'{s}={os.path.getsize(base + s)}:{os.stat(base + s).st_mtime_ns}' for s in parts), parts


def place_index(w):
    """索引を作業フォルダの .index に置く。前の回の .index-keep が元と同じなら使い回す(写す時間を省く)"""
    ws, src = w['path'], w['index']
    sig, parts = index_sig(src)
    keep, dst = os.path.join(ws, '.index-keep'), os.path.join(ws, '.index')
    if os.path.exists(dst):
        shutil.rmtree(dst)
    same = os.path.exists(os.path.join(keep, 'keep.txt')) and open(os.path.join(keep, 'keep.txt'), encoding='utf-8').read().strip() == sig
    if same:
        for s in parts:
            k = os.path.join(keep, 'index.tsv' + s)
            if not os.path.exists(k) or os.path.getsize(k) != os.path.getsize(src + s) or os.stat(k).st_mtime_ns != os.stat(src + s).st_mtime_ns:
                same = False
    if same:
        os.rename(keep, dst)
    else:
        if os.path.exists(keep):
            shutil.rmtree(keep)
        os.makedirs(dst)
        for s in parts:
            shutil.copy2(src + s, os.path.join(dst, 'index.tsv' + s))  # 更新日時を保つ(ソースと比べて古さを決める)
        with open(os.path.join(dst, 'keep.txt'), 'w', encoding='utf-8') as f:
            f.write(sig)
    with open(os.path.join(dst, '.gitignore'), 'w', encoding='utf-8') as f:
        f.write('*\n')
    recipe = {'version': 1, 'root': ws, 'cdb': os.path.join(ws, w['cdb']), 'out': os.path.join(dst, 'index.tsv'),
              'jobs': max(1, (os.cpu_count() or 2) // 4), 'makeCdb': None}  # 自動更新の並列は少なめ(CPU の負荷を抑える)
    with open(os.path.join(dst, 'index.tsv.recipe.json'), 'w', encoding='utf-8') as f:
        json.dump(recipe, f, ensure_ascii=False, indent=1)


def stash_index(w):
    """回の後、索引が変わっていなければ .index-keep に退避する。自動更新で作り直されていたら捨てる"""
    ws = w['path']
    dst, keep = os.path.join(ws, '.index'), os.path.join(ws, '.index-keep')
    lock = os.path.join(dst, 'update.lock')
    deadline = time.time() + 600
    while os.path.exists(lock) and time.time() < deadline:
        try:
            os.remove(lock)
        except OSError:
            time.sleep(0.5)
    if not os.path.isdir(dst):
        return
    sig, _ = index_sig(w['index'])
    _, parts = index_sig(w['index'])
    ok = all(os.path.exists(os.path.join(dst, 'index.tsv' + s)) and
             os.stat(os.path.join(dst, 'index.tsv' + s)).st_mtime_ns == os.stat(w['index'] + s).st_mtime_ns for s in parts)
    if ok:
        if os.path.exists(keep):
            shutil.rmtree(keep)
        os.rename(dst, keep)
    else:
        shutil.rmtree(dst)


def diff_of(ws):
    git(ws, 'add', '-A')  # 無視しているもの(.index・.index-keep・Nox の .github など)は入らない
    d = git(ws, 'diff', '--cached', '--no-color')
    git(ws, 'reset', '-q')
    return d


def collect(home, usage_file):
    """COPILOT_HOME の記録から、往復・道具・トークンを集める"""
    rec = {}
    sd = os.path.join(home, 'session-state')
    sess = [d for d in os.listdir(sd) if not d.startswith('.')] if os.path.isdir(sd) else []  # .session-operation-locks などは除く
    rec['session'] = sess[0] if len(sess) == 1 else ' '.join(sess)
    tools, results, refs_calls = {}, {}, 0
    if len(sess) == 1:
        names = {}
        for line in open(os.path.join(sd, sess[0], 'events.jsonl'), encoding='utf-8'):
            e = json.loads(line)
            if e['type'] == 'tool.execution_start':
                t = e['data']['toolName']
                names[e['data'].get('toolCallId')] = t
                tools[t] = tools.get(t, 0) + 1
            elif e['type'] == 'tool.execution_complete':
                t = names.get(e['data'].get('toolCallId'), '?')
                r = e['data'].get('result')
                results[t] = results.get(t, 0) + (len(json.dumps(r, ensure_ascii=False)) if r is not None else 0)
            elif e['type'] == 'assistant.message':
                rec['answer'] = e['data'].get('content', '')
    rec['tools'] = tools
    rec['tool_result_chars'] = results
    rec['refs_calls'] = sum(n for t, n in tools.items() if t.startswith('code-ref-index'))
    db = os.path.join(home, 'session-store.db')
    if os.path.exists(db) and len(sess) == 1:
        c = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
        u = c.execute('select input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, total_nano_aiu from assistant_usage_events '
                      'where session_id=? order by id', (sess[0],)).fetchall()
        c.close()
        rec['calls'] = len(u)
        rec['first_input'] = u[0][0] if u else 0
        rec['input'] = sum(x[0] or 0 for x in u)
        rec['cache_read'] = sum(x[1] or 0 for x in u)
        rec['cache_write'] = sum(x[2] or 0 for x in u)
        rec['output'] = sum(x[3] or 0 for x in u)
        rec['per_call_input'] = [x[0] for x in u]
    if os.path.exists(usage_file):
        us = json.load(open(usage_file, encoding='utf-8'))
        rec['ai_credits'] = round(us.get('totalNanoAiu', 0) / 1e9, 4)
        rec['api_ms'] = us.get('totalApiDurationMs')
        rec['files_modified'] = us.get('codeChanges', {}).get('filesModifiedCount')
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tasks', required=True)
    ap.add_argument('--task', required=True)
    ap.add_argument('--cond', required=True, choices=['A', 'P', 'M', 'X'])
    ap.add_argument('--rep', type=int, default=1)
    ap.add_argument('--round', default='v13')
    ap.add_argument('--model', default='gpt-6-luna')
    ap.add_argument('--provider', default='', choices=['', 'anthropic'])
    ap.add_argument('--timeout', type=int, default=1200)
    ap.add_argument('--max-ai-credits', default='300')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    task = next(t for t in json.load(open(os.path.join(BENCH, a.tasks), encoding='utf-8')) if t['id'] == a.task)
    w = WORKSPACES[task['repo']]
    ws = w['path']
    name = f'{a.round}-{a.task}-{a.cond}-r{a.rep}'
    os.makedirs(RUNS, exist_ok=True)

    reset_ws(w)
    if a.cond in ('P', 'M', 'X') and w.get('index'):
        place_index(w)
    if a.cond == 'X':
        gh = os.path.join(ws, '.github')
        os.makedirs(gh, exist_ok=True)
        with open(os.path.join(gh, 'copilot-instructions.md'), 'a', encoding='utf-8') as f:
            f.write(open(os.path.join(BENCH, 'v13', 'adverse-instructions.md'), encoding='utf-8').read())

    home = os.path.join(V13, 'homes', name)
    if os.path.exists(home):
        shutil.rmtree(home)
    shutil.copytree(os.path.join(V13, 'homes', 'tmpl-P' if a.cond in ('P', 'X') else 'tmpl-A'), home)
    usage_file = os.path.join(RUNS, name + '.usage.json')
    if os.path.exists(usage_file):
        os.remove(usage_file)

    cmd = [COPILOT, '-p', task['prompt'], '--model', a.model, '--allow-all-tools', '-s', '--trust-folder',
           '--usage-output-file', usage_file, '--share', os.path.join(RUNS, name + '.md'), '--log-level', 'error',
           '--max-ai-credits', a.max_ai_credits]
    for d in DENY:
        cmd += ['--deny-tool', f'shell({d})', '--deny-tool', f'shell({d}:*)']
    if a.cond == 'M':
        mcp = {'mcpServers': {'code-ref-index': {'type': 'local', 'command': 'python',
                                                  'args': ['-I', os.path.join(REPO, 'mcp', 'server.py')], 'tools': ['*']}}}
        cmd += ['--additional-mcp-config', json.dumps(mcp)]
    env = dict(os.environ, COPILOT_HOME=home, LTIDX=LTIDX)
    for v in ('COPILOT_PROVIDER_TYPE', 'COPILOT_PROVIDER_BASE_URL', 'COPILOT_PROVIDER_API_KEY', 'COPILOT_PROVIDER_API_KEY_COMMAND',
              'COPILOT_MODEL', 'COPILOT_PROVIDER_MODEL_ID', 'COPILOT_PROVIDER_WIRE_MODEL'):
        env.pop(v, None)
    if a.provider == 'anthropic':  # BYOK(Anthropic API を直接。従量課金)。キーは providers.json から、表示せずに渡す
        import re
        env.update(COPILOT_PROVIDER_TYPE='anthropic', COPILOT_PROVIDER_BASE_URL='https://api.anthropic.com',
                   COPILOT_PROVIDER_API_KEY_COMMAND=f'python -I "{os.path.join(BENCH, "provider_key.py")}" anthropic',
                   COPILOT_PROVIDER_WIRE_MODEL=a.model)
        env['COPILOT_PROVIDER_MODEL_ID'] = env['COPILOT_MODEL'] = re.sub(r'-(\d+)-(\d+)$', r'-\1.\2', a.model)
    if a.dry_run:
        print(json.dumps({'name': name, 'ws': ws, 'home': home, 'cmd': cmd[:4] + ['...'] + cmd[5:]}, ensure_ascii=False, indent=1))
        reset_ws(w)
        return

    t0 = time.time()
    start = datetime.now(timezone.utc).isoformat(timespec='seconds')
    try:
        p = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=a.timeout)
        code, out = p.returncode, (p.stdout or '') + (p.stderr or '')
    except subprocess.TimeoutExpired as e:
        code, out = 'timeout', str(e)
    sec = round(time.time() - t0)
    with open(os.path.join(RUNS, name + '.out.txt'), 'w', encoding='utf-8') as f:
        f.write(out)
    d = diff_of(ws)
    with open(os.path.join(RUNS, name + '.diff'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(d)
    if w.get('index'):
        stash_index(w)
    reset_ws(w)
    rec = {'name': name, 'round': a.round, 'task': a.task, 'cond': a.cond, 'rep': a.rep, 'model': a.model, 'provider': a.provider,
           'exit': code, 'sec': sec, 'at': start, 'diff_lines': d.count('\n')}
    rec.update(collect(home, usage_file))
    with open(os.path.join(RUNS, 'index.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f"{name} exit={code} {sec}s calls={rec.get('calls')} input={rec.get('input')} refs={rec.get('refs_calls')} aic={rec.get('ai_credits')}")


if __name__ == '__main__':
    main()
