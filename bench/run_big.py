"""大きなコードベース(O3DE・UE5 など)の写しで、1 問を Copilot CLI に解かせる。条件は run13.py の A・V・W と同じ形。

usage: python -I run_big.py --ws <写しのフォルダ> --tasks <問題の JSON> --task O2 --cond A|V|W|R --rep 1 --round big1
       [--skill-ref <コミット>] [--model claude-haiku-5-5] [--provider anthropic] [--timeout 2400]

条件
  A: プラグインなし。空の COPILOT_HOME。索引(<写し>\\.index)は、回の間だけ写しの外に退かす
  V: MCP 版(タグ v0.5.9 のプラグインと、/cxxref-enable on が置くのと同じ使い方の指示)
  W: skill 版(このリポジトリの今のファイルから作る)
  R: skill 版(--skill-ref のコミットのファイルから作る。直す前と後を同じ時期に比べるため)

run13.py との違い
- 索引は写しの .index をそのまま使う(数十 GB あるので写さない)。git clean はしない(.index と .cdb が消えるため)。
  回の前の `git status` を基準に、増えた変更だけを戻す
- 写しに残っている前の計測の設定(.mcp.json・.github の指示)は、回の間だけ退かして戻す
- 記録は run13.py と同じ runs13/index.jsonl(ratio13.py でそのまま比を出せる)。失敗したツール呼び出しの数(failed)と、
  索引を引くコマンドの時間(cli_sec)も入れる
"""
import argparse, json, os, re, shutil, subprocess, sys, time
from datetime import datetime, timezone

BENCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BENCH)
import run13

PARKED = ['.mcp.json', os.path.join('.github', 'copilot-instructions.md'), os.path.join('.github', 'instructions')]


def git(ws, *a):
    return subprocess.run(['git', '-C', ws] + list(a), capture_output=True, text=True, encoding='utf-8', errors='replace').stdout


def status(ws):
    return {l for l in git(ws, 'status', '--porcelain').splitlines() if l.strip()}


def rename_retry(src, dst):
    for _ in range(60):
        try:
            os.rename(src, dst)
            return
        except OSError:
            time.sleep(1)
    os.rename(src, dst)


def tool_times(home):
    """events.jsonl から、失敗したツール呼び出しの数と、索引を引いた呼び出し(MCP の refs・check、CLI)の秒数の一覧。"""
    sd = os.path.join(home, 'session-state')
    failed, secs = 0, []
    for s in (os.listdir(sd) if os.path.isdir(sd) else []):
        ev = os.path.join(sd, s, 'events.jsonl')
        if not os.path.exists(ev):
            continue
        start = {}
        for line in open(ev, encoding='utf-8'):
            e = json.loads(line)
            d = e.get('data', {})
            if e['type'] == 'tool.execution_start':
                cmd = str((d.get('arguments') or {}).get('command', '')) if isinstance(d.get('arguments'), dict) else ''
                if d.get('toolName', '').startswith('code-ref-index') or 'cxxref_cli.py' in cmd:
                    start[d.get('toolCallId')] = e.get('timestamp')
            elif e['type'] == 'tool.execution_complete':
                if d.get('success') is False:
                    failed += 1
                t0 = start.pop(d.get('toolCallId'), None)
                if t0 and e.get('timestamp'):
                    f = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00'))
                    secs.append(round((f(e['timestamp']) - f(t0)).total_seconds(), 1))
    return failed, secs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--ws', required=True)
    ap.add_argument('--tasks', required=True)
    ap.add_argument('--task', required=True)
    ap.add_argument('--cond', required=True, choices=['A', 'V', 'W', 'R'])
    ap.add_argument('--skill-ref', default='', help='条件 R の skill 版を作るコミット(条件 R には必須)')
    ap.add_argument('--rep', type=int, default=1)
    ap.add_argument('--round', required=True)
    ap.add_argument('--model', default='claude-haiku-5-5')
    ap.add_argument('--provider', default='anthropic', choices=['', 'anthropic'])
    ap.add_argument('--timeout', type=int, default=2400)
    a = ap.parse_args()
    if a.cond == 'R' and not a.skill_ref:
        ap.error('--cond R needs --skill-ref')

    ws = os.path.abspath(a.ws)
    task = next(t for t in json.load(open(os.path.join(BENCH, a.tasks), encoding='utf-8')) if t['id'] == a.task)
    name = f'{a.round}-{a.task}-{a.cond}-r{a.rep}'
    os.makedirs(run13.RUNS, exist_ok=True)
    home = os.path.join(run13.V13, 'homes', name)
    usage_file = os.path.join(run13.RUNS, name + '.usage.json')
    if os.path.exists(home) or os.path.exists(usage_file):
        raise SystemExit(f'{name} already exists; use another --round')

    index, park_index = os.path.join(ws, '.index'), ws.rstrip('\\/') + '.index-parked'
    park = os.path.join(run13.V13, 'park-big', os.path.basename(ws))
    if os.path.isdir(park_index) and not os.path.isdir(index):
        rename_retry(park_index, index)  # 前の回が途中で止まっていたら戻す
    if any(files for _, _, files in os.walk(park)):  # 空のフォルダだけなら、前の回が戻し終えている
        raise SystemExit(f'{park} is not empty; put its files back into the workspace first')
    if not os.path.isdir(index):
        raise SystemExit(f'no index in {ws}')
    known = status(ws)

    os.makedirs(home)
    cmd = [run13.COPILOT, '-p', task['prompt'], '--model', a.model, '--allow-all-tools', '-s', '--trust-folder',
           '--usage-output-file', usage_file, '--share', os.path.join(run13.RUNS, name + '.md'), '--log-level', 'error']
    for d in run13.DENY:
        cmd += ['--deny-tool', f'shell({d})', '--deny-tool', f'shell({d}:*)']
    env = dict(os.environ, COPILOT_HOME=home, LTIDX=run13.LTIDX_R)
    for v in ('COPILOT_PROVIDER_TYPE', 'COPILOT_PROVIDER_BASE_URL', 'COPILOT_PROVIDER_API_KEY', 'COPILOT_PROVIDER_API_KEY_COMMAND',
              'COPILOT_MODEL', 'COPILOT_PROVIDER_MODEL_ID', 'COPILOT_PROVIDER_WIRE_MODEL', 'CRI_ROOT', 'CRI_INDEX'):
        env.pop(v, None)
    if a.provider == 'anthropic':  # BYOK。キーは providers.json から、表示せずに渡す
        env.update(COPILOT_PROVIDER_TYPE='anthropic', COPILOT_PROVIDER_BASE_URL='https://api.anthropic.com',
                   COPILOT_PROVIDER_API_KEY_COMMAND=f'python -I "{os.path.join(BENCH, "provider_key.py")}" anthropic',
                   COPILOT_PROVIDER_WIRE_MODEL=a.model)
        env['COPILOT_PROVIDER_MODEL_ID'] = env['COPILOT_MODEL'] = re.sub(r'-(\d+)-(\d+)$', r'-\1.\2', a.model)
    if a.cond == 'V':
        run13.make_plugin_v()
        run13.install_plugin_r(home, run13.PLUGIN_V)
    if a.cond == 'W':
        run13.make_plugin_r(run13.PLUGIN_W)
        run13.install_plugin_r(home, run13.PLUGIN_W)
    if a.cond == 'R':
        run13.make_plugin_r(ref=a.skill_ref)
        run13.install_plugin_r(home)

    moved, index_parked = [], False
    code, out, d, extra = 'error', '', '', []
    t0 = time.time()
    start = datetime.now(timezone.utc).isoformat(timespec='seconds')
    try:
        for rel in PARKED:  # 前の計測の設定を退かす
            src = os.path.join(ws, rel)
            if os.path.exists(src):
                dst = os.path.join(park, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                moved.append((src, dst))
        if a.cond == 'A':
            rename_retry(index, park_index)
            index_parked = True
        if a.cond == 'V':
            run13.place_instructions_v(ws)
        base = status(ws)  # 条件の設定を置いた後の状態(ここから増えた分が、回の変更)
        t0 = time.time()
        try:
            p = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=a.timeout)
            code, out = p.returncode, (p.stdout or '') + (p.stderr or '')
        except subprocess.TimeoutExpired as e:
            code, out = 'timeout', str(e)
        sec = round(time.time() - t0)
        after = status(ws)
        changed = sorted(l for l in after - base if not l.startswith('??'))
        extra = sorted(l[3:] for l in after - base if l.startswith('??'))
        if changed:  # 追跡しているファイルの変更は差分を残して戻す。増えたファイルは個別に消す(git clean はしない)
            d = git(ws, 'diff', '--no-color')
            git(ws, 'checkout', '-q', '--', *[l[3:].strip('"') for l in changed])
        for x in extra:
            fp = os.path.join(ws, x.strip('"').rstrip('/'))
            if os.path.isdir(fp):
                shutil.rmtree(fp)
            elif os.path.exists(fp):
                os.remove(fp)
    finally:
        sec = round(time.time() - t0)
        if a.cond == 'V':
            f = os.path.join(ws, '.github', 'instructions', 'cxxref.instructions.md')
            if os.path.exists(f):
                os.remove(f)
            if os.path.isdir(os.path.dirname(f)) and not os.listdir(os.path.dirname(f)):
                os.rmdir(os.path.dirname(f))
        if index_parked:
            rename_retry(park_index, index)
        for src, dst in reversed(moved):
            if os.path.isdir(src) and not os.listdir(src):
                os.rmdir(src)
            shutil.move(dst, src)
    left = status(ws) - known
    with open(os.path.join(run13.RUNS, name + '.out.txt'), 'w', encoding='utf-8') as f:
        f.write(out)
    if d:
        with open(os.path.join(run13.RUNS, name + '.diff'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(d)
    rec = {'name': name, 'round': a.round, 'task': a.task, 'cond': a.cond, 'rep': a.rep, 'model': a.model, 'provider': a.provider,
           'ws': os.path.basename(ws), 'exit': code, 'sec': sec, 'at': start, 'diff_lines': d.count('\n'), 'new_files': extra}
    rec.update(run13.collect(home, usage_file))
    rec['failed'], rec['cli_sec'] = tool_times(home)
    with open(os.path.join(run13.RUNS, 'index.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f"{name} exit={code} {sec}s calls={rec.get('calls')} input={rec.get('input')} refs={rec.get('refs_calls')} "
          f"cli_sec={rec['cli_sec']} failed={rec['failed']} tools={rec.get('tools')}", flush=True)
    if left:
        raise SystemExit('workspace differs from before the run:\n' + '\n'.join(sorted(left)))


if __name__ == '__main__':
    main()
