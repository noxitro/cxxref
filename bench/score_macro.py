"""run_macro.py の回を採点してまとめる。回答の中の path:line を正解と突き合わせる(行は完全一致)。

usage: python -I score_macro.py [--round mac1] [--tasks v13/tasks-macro.json --tasks local/v13-tasks-macro-a.json]
"""
import argparse
import collections
import json
import math
import os
import re

BENCH = os.path.dirname(os.path.abspath(__file__))


def locs(text):
    out = set()
    for m in re.finditer(r'([\w./\\-]+\.(?:h|hpp|hh|inl|cpp|cc|cxx|c|def)):(\d+)', text or ''):
        p = m.group(1).replace('\\', '/').lstrip('./')
        out.add(f'{p}:{m.group(2)}')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--round', default='mac1')
    ap.add_argument('--tasks', action='append', help='問題のファイル(繰り返せる。既定は v13/tasks-macro.json と、あれば local の分)')
    a = ap.parse_args()
    files = [f for f in (a.tasks or ['v13/tasks-macro.json', 'local/v13-tasks-macro-a.json']) if os.path.exists(os.path.join(BENCH, f))]
    tasks = {t['id']: t for f in files for t in json.load(open(os.path.join(BENCH, f), encoding='utf-8'))}
    recs = [json.loads(l) for l in open(os.path.join(BENCH, 'runs-macro', 'index.jsonl'), encoding='utf-8')]
    recs = [r for r in recs if r['round'] == a.round and r['task'] in tasks]
    by = collections.defaultdict(list)
    for r in recs:
        truth = set(tasks[r['task']]['truth'])
        accept = set(tasks[r['task']].get('accept', []))
        # パスの書き方の揺れ(runtime/ を省く・ファイル名だけ)は、末尾が正解の 1 つにだけ合えばそれとみなす
        got = set()
        for g in locs(r.get('answer')):
            hit = [x for x in truth | accept if x == g or x.endswith('/' + g)]
            got.add(hit[0] if len(hit) == 1 else g)
        got -= accept
        tp = len(got & truth)
        r['recall'] = tp / len(truth)
        r['precision'] = tp / len(got) if got else 0.0
        r['exact'] = got == truth
        r['missing'] = sorted(truth - got)
        r['extra'] = sorted(got - truth)
        by[(r['task'], r['cond'])].append(r)
    print(f'{"task":24} {"cond":4} {"n":>2} {"input":>8} {"calls":>5} {"refs":>4} {"sec":>4} {"recall":>6} {"prec":>5} exact')
    gm = collections.defaultdict(list)
    for (t, c), rs in sorted(by.items()):
        n = len(rs)
        mean = lambda k: sum(r.get(k) or 0 for r in rs) / n
        print(f'{t:24} {c:4} {n:2} {mean("input"):8.0f} {mean("calls"):5.1f} {mean("refs_calls"):4.1f} {mean("sec"):4.0f}'
              f' {mean("recall"):6.2f} {mean("precision"):5.2f} {sum(r["exact"] for r in rs)}/{n}')
        gm[c].append((t, mean('input')))
    # 問題ごとの入力の平均の比(A に対して)の幾何平均
    base = dict(gm.get('A', []))
    for c in ('O', 'N'):
        rat = [v / base[t] for t, v in gm.get(c, []) if base.get(t)]
        if rat:
            print(f'{c}/A input ratio (geomean over tasks): {math.exp(sum(map(math.log, rat)) / len(rat)):.2f}  per task:',
                  ', '.join(f'{t.split("-")[0]} {v / base[t]:.2f}' for t, v in gm[c] if base.get(t)))
    for (t, c), rs in sorted(by.items()):
        for r in rs:
            if not r['exact']:
                print(f'  {r["name"]}: missing {r["missing"][:4]}{" ..." if len(r["missing"]) > 4 else ""} extra {r["extra"][:4]}')


if __name__ == '__main__':
    main()
