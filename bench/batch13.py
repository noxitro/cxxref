"""v13 の問題 × 条件 × 回数を、run13.py で順に流す。runs13/index.jsonl に終わった回(exit 0)があれば飛ばす。
回を外側にして、どこで止まっても条件と問題の間で回数が揃うようにする。並べては流さない。

usage: python -I batch13.py --tasks v13/tasks-stage1.json --conds A,P --reps 5 [--round v13] [--model ...] [--provider ...]
       [--only ID,ID] [--stop-file <path>](このファイルがあれば、次の回の前に止まる)
"""
import argparse, json, os, subprocess, sys

BENCH = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser()
ap.add_argument('--tasks', required=True)
ap.add_argument('--conds', required=True)
ap.add_argument('--reps', type=int, default=5)
ap.add_argument('--round', default='v13')
ap.add_argument('--model', default='gpt-6-luna')
ap.add_argument('--provider', default='')
ap.add_argument('--only', default='')
ap.add_argument('--stop-file', default=os.path.join(BENCH, 'runs13', 'STOP'))
a = ap.parse_args()

tasks = [t['id'] for t in json.load(open(os.path.join(BENCH, a.tasks), encoding='utf-8'))]
if a.only:
    tasks = [t for t in tasks if t in a.only.split(',')]
done = set()
idx = os.path.join(BENCH, 'runs13', 'index.jsonl')
if os.path.exists(idx):
    for line in open(idx, encoding='utf-8'):
        r = json.loads(line)
        if r.get('exit') == 0:
            done.add(r['name'])
for rep in range(1, a.reps + 1):
    for t in tasks:
        for c in a.conds.split(','):
            name = f'{a.round}-{t}-{c}-r{rep}'
            if name in done:
                continue
            if os.path.exists(a.stop_file):
                sys.exit('stopped by ' + a.stop_file)
            cmd = [sys.executable, '-I', os.path.join(BENCH, 'run13.py'), '--tasks', a.tasks, '--task', t, '--cond', c,
                   '--rep', str(rep), '--round', a.round, '--model', a.model]
            if a.provider:
                cmd += ['--provider', a.provider]
            r = subprocess.run(cmd, cwd=BENCH)
            if r.returncode != 0:
                print(f'{name}: run13 failed ({r.returncode})', flush=True)
