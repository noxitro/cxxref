"""マクロの扱いを直す前と後の計測: Copilot CLI に 1 問を解かせ、記録を集める(v13 の run13.py と同じ集め方)。

usage: python -I run_macro.py --task MT1-assert-kernel --cond N --rep 1 [--round mac1] [--model claude-haiku-5-5]
       [--provider anthropic] [--dry-run]
       python -I run_macro.py --restore-o3de   (O3DE の写しから退避したものを戻す)

条件
  A: cxxref なし(空の COPILOT_HOME)
  O: 直す前(コミット ecd4819)。その版の MCP サーバー・ltq と、その前の ltidx で作った索引
  N: 直した後。このリポジトリの MCP サーバー・ltq と、マクロの行・dropped・local の範囲を出す ltidx で作った索引
  O と N は、/cxxref-enable が入れる使い方の指示(scripts/cxxref-instructions.md)を .github/copilot-instructions.md に置く。
  サーバーには索引の場所を直接渡し(--index)、自動更新は切る(問題はどれも読むだけ)。

作業フォルダ
  nox: git で管理した写し。前と後に git checkout と git clean で戻す(run13.py と同じ)
  o3de: 前の計測が残した設定(.github/copilot-instructions.md・.mcp.json・.serena)を、最初の回で o3de-aside に退避する
        (--restore-o3de で戻す)。回の後は、回の前に無かった変更だけを戻す(元の写しは .github の一部を消してある)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

BENCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BENCH)
import run13  # noqa: E402

REPO = os.path.dirname(BENCH)
DATA = run13.DATA
V13 = run13.V13
RUNS = os.path.join(BENCH, 'runs-macro')
OLD_REPO = os.path.join(V13, 'repo-ecd4819')
LTIDX = {'O': os.path.join(V13, 'ltidx-ecd4819', 'ltidx.exe'), 'N': os.path.join(V13, 'ltidx-macro', 'ltidx.exe')}
WS = {
    'nox': {'path': os.path.join(DATA, 'nox-20261103'), 'clean': ['.github', '.mcp.json', '.serena'],
            'index': {'O': os.path.join(DATA, 'index', 'nox-m-old.tsv'), 'N': os.path.join(DATA, 'index', 'nox-m-fin.tsv')},
            'format': 'answer-format.md'},
    'o3de': {'path': os.path.join(DATA, 'o3de-src'),
             'aside': ['.github/copilot-instructions.md', '.mcp.json', '.serena'],
             'index': {'O': os.path.join(DATA, 'index', 'o3de-v1.tsv'), 'N': os.path.join(DATA, 'index', 'o3de-m.tsv')},
             'format': 'answer-format-o3de.md'},
}
ASIDE = os.path.join(DATA, 'o3de-aside')


def git(ws, *a):
    return run13.git(ws, *a, check=False)


def status(ws):
    return set(l for l in git(ws, 'status', '--porcelain', '--untracked-files=all').splitlines() if l)


def o3de_aside():
    w = WS['o3de']
    for rel in w['aside']:
        src, dst = os.path.join(w['path'], rel), os.path.join(ASIDE, rel)
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)


def o3de_restore():
    w = WS['o3de']
    for rel in w['aside']:
        src, dst = os.path.join(ASIDE, rel), os.path.join(w['path'], rel)
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)


def undo_o3de(ws, before):
    """回の前に無かった変更だけを戻す: 変えられた追跡ファイルは git checkout、増えたファイルは消す。
    名前の変更(`R  old -> new`。git mv したもの)は、索引から両方を外して old を戻し、new を消す"""
    for line in status(ws) - before:
        code, rel = line[:2], line[3:].strip()
        if ' -> ' in rel:
            old, new = (x.strip().strip('"') for x in rel.split(' -> ', 1))
            git(ws, 'reset', '-q', '--', old, new)
            git(ws, 'checkout', '-q', '--', old)
            fp = os.path.join(ws, new)
            if os.path.isfile(fp):
                os.remove(fp)
            continue
        rel = rel.strip('"')
        fp = os.path.join(ws, rel)
        if code == '??':
            if os.path.isdir(fp):
                shutil.rmtree(fp)
            elif os.path.exists(fp):
                os.remove(fp)
        else:
            git(ws, 'checkout', '-q', '--', rel)
    left = status(ws) - before
    if left:
        raise SystemExit('o3de workspace not restored: ' + ' | '.join(sorted(left))[:2000])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tasks', default='v13/tasks-macro.json')
    ap.add_argument('--task')
    ap.add_argument('--cond', choices=['A', 'O', 'N'])
    ap.add_argument('--rep', type=int, default=1)
    ap.add_argument('--round', default='mac1')
    ap.add_argument('--model', default='claude-haiku-5-5')
    ap.add_argument('--provider', default='anthropic', choices=['', 'anthropic'])
    ap.add_argument('--timeout', type=int, default=1200)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--restore-o3de', action='store_true')
    a = ap.parse_args()
    if a.restore_o3de:
        o3de_restore()
        return

    task = next(t for t in json.load(open(os.path.join(BENCH, a.tasks), encoding='utf-8')) if t['id'] == a.task)
    w = WS[task['repo']]
    ws = w['path']
    name = f'{a.round}-{a.task}-{a.cond}-r{a.rep}'
    os.makedirs(RUNS, exist_ok=True)

    if task['repo'] == 'nox':
        run13.reset_ws({'path': ws, 'clean': w['clean']})
    else:
        o3de_aside()
    before = status(ws) if task['repo'] == 'o3de' else None
    if a.cond in ('O', 'N'):
        gh = os.path.join(ws, '.github')
        os.makedirs(gh, exist_ok=True)
        with open(os.path.join(REPO, 'scripts', 'cxxref-instructions.md'), encoding='utf-8') as f:
            text = f.read()
        with open(os.path.join(gh, 'copilot-instructions.md'), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)

    home = os.path.join(V13, 'homes', name)
    if os.path.exists(home):
        shutil.rmtree(home)
    shutil.copytree(os.path.join(V13, 'homes', 'tmpl-A'), home)
    usage_file = os.path.join(RUNS, name + '.usage.json')
    if os.path.exists(usage_file):
        os.remove(usage_file)
    with open(os.path.join(BENCH, w['format']), encoding='utf-8') as f:
        prompt = task['prompt'] + '\n\n' + f.read()

    cmd = [run13.COPILOT, '-p', prompt, '--model', a.model, '--allow-all-tools', '-s', '--trust-folder',
           '--usage-output-file', usage_file, '--share', os.path.join(RUNS, name + '.md'), '--log-level', 'error',
           '--max-ai-credits', '300']
    for d in run13.DENY:
        cmd += ['--deny-tool', f'shell({d})', '--deny-tool', f'shell({d}:*)']
    env = dict(os.environ, COPILOT_HOME=home)
    if a.cond in ('O', 'N'):
        repo = OLD_REPO if a.cond == 'O' else REPO
        mcp = {'mcpServers': {'code-ref-index': {
            'type': 'local', 'command': 'python', 'tools': ['*'],
            'args': ['-I', os.path.join(repo, 'mcp', 'server.py'), '--root', ws, '--index', w['index'][a.cond],
                     '--no-auto-update']}}}
        cmd += ['--additional-mcp-config', json.dumps(mcp)]
        env['LTIDX'] = LTIDX[a.cond]
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
        print(json.dumps({'name': name, 'ws': ws, 'home': home, 'mcp': cmd[-1] if a.cond != 'A' else None},
                         ensure_ascii=False, indent=1))
    else:
        t0 = time.time()
        start = datetime.now(timezone.utc).isoformat(timespec='seconds')
        try:
            p = subprocess.run(cmd, cwd=ws, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=a.timeout)
            code, out = p.returncode, (p.stdout or '') + (p.stderr or '')
        except subprocess.TimeoutExpired as e:
            code, out = 'timeout', str(e)
        sec = round(time.time() - t0)
        with open(os.path.join(RUNS, name + '.out.txt'), 'w', encoding='utf-8') as f:
            f.write(out)
    # 片付け: 置いた指示を消し、作業フォルダを戻す
    gi = os.path.join(ws, '.github', 'copilot-instructions.md')
    if a.cond in ('O', 'N') and os.path.exists(gi):
        os.remove(gi)
    if task['repo'] == 'nox':
        run13.reset_ws({'path': ws, 'clean': w['clean']})
    else:
        undo_o3de(ws, before)
    if a.dry_run:
        return
    rec = {'name': name, 'round': a.round, 'task': a.task, 'cond': a.cond, 'rep': a.rep, 'model': a.model,
           'provider': a.provider, 'exit': code, 'sec': sec, 'at': start}
    rec.update(run13.collect(home, usage_file))
    with open(os.path.join(RUNS, 'index.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(f"{name} exit={code} {sec}s calls={rec.get('calls')} input={rec.get('input')} refs={rec.get('refs_calls')}")


if __name__ == '__main__':
    main()
