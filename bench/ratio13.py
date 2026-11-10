"""v13 の入力トークンの比と、その 95% の bootstrap 信頼区間を出す。

usage: python -I ratio13.py --round v13q --conds A,P,Q [--base A] [--tasks local/v13-tasks-stage2-a.json]

比の出し方(v13 の段階 1・2 と測り直しは、どれもこの出し方の値)
- 問題ごとの比: 各条件の回の入力の平均どうしの比(例: P の平均 / A の平均)。入力の合計の比と同じ
- まとめ: 問題ごとの比を、問題をまたいで幾何平均したもの(docs/benchmarks.md・design.md の「A との比」)と、
  中央値(docs/bench-plan-v13.md で事前に決めた主な指標)
- 信頼区間: 2 段の bootstrap(10,000 回、乱数の種 1)。問題を引き直し、選んだ問題の中で各条件の回を引き直す
rep 0(試し)と、exit が 0 でない回は除く。記録は runs13/index.jsonl(git に入れていない)。
"""
import argparse, collections, json, math, os, random, statistics as st, sys

BENCH = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--round', required=True)
    ap.add_argument('--conds', required=True)
    ap.add_argument('--base', default='A')
    ap.add_argument('--tasks', help='この問題のファイル(local/v13-tasks-stage2-a.json など)の問題だけに絞る')
    ap.add_argument('--only', help='問題の id をこの文字で始まるものに絞る(例: S1-requests,S1-newtonsoft,S1-zod)')
    a = ap.parse_args()
    conds = a.conds.split(',')
    only = {t['id'] for t in json.load(open(os.path.join(BENCH, a.tasks), encoding='utf-8'))} if a.tasks else None
    prefixes = tuple(a.only.split(',')) if a.only else None
    rs = [json.loads(l) for l in open(os.path.join(BENCH, 'runs13', 'index.jsonl'), encoding='utf-8-sig')]
    rs = [r for r in rs if r['round'] == a.round and r['rep'] > 0 and r.get('exit') == 0 and r['cond'] in conds
          and (only is None or r['task'] in only) and (prefixes is None or r['task'].startswith(prefixes))]
    by = collections.defaultdict(list)
    for r in rs:
        by[(r['task'], r['cond'])].append(r['input'])
    tasks = sorted({t for t, _ in by})
    gm = lambda xs: math.exp(st.mean(math.log(x) for x in xs))

    sys.stdout.reconfigure(encoding='utf-8')
    print('task'.ljust(24) + ''.join(f'{c:>10}' for c in conds) + ''.join(f'{c + "/" + a.base:>8}' for c in conds if c != a.base))
    for t in tasks:
        m = {c: st.mean(by[(t, c)]) for c in conds}
        print(t.ljust(24) + ''.join(f'{m[c]:10.0f}' for c in conds) +
              ''.join(f'{m[c] / m[a.base]:8.2f}' for c in conds if c != a.base))
    rnd = random.Random(1)
    for c in conds:
        if c == a.base:
            continue
        ratio = lambda t: st.mean(by[(t, c)]) / st.mean(by[(t, a.base)])
        point = [ratio(t) for t in tasks]
        bg, bm = [], []
        for _ in range(10000):
            rr = []
            for t in (rnd.choice(tasks) for _ in tasks):
                num = [rnd.choice(by[(t, c)]) for _ in by[(t, c)]]
                den = [rnd.choice(by[(t, a.base)]) for _ in by[(t, a.base)]]
                rr.append(st.mean(num) / st.mean(den))
            bg.append(gm(rr))
            bm.append(st.median(rr))
        bg.sort()
        bm.sort()
        print(f'{c}/{a.base}: 幾何平均 {gm(point):.3f} ({bg[250]:.2f}-{bg[9750]:.2f})  '
              f'中央値 {st.median(point):.3f} ({bm[250]:.2f}-{bm[9750]:.2f})  問題ごと {min(point):.2f}-{max(point):.2f}')


if __name__ == '__main__':
    main()
