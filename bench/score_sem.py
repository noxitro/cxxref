"""5 つの観点(継承・基底呼び出し・virtual dispatch・条件コンパイル・テンプレートの実体化)の指示つきの問題を採点する。

usage: python -I score_sem.py --round sem1 --tasks tasks-sem-o3de.json [--tasks local/...json] [--conds A,V,W] [--runs]

回答(runs13/<名前>.out.txt)から `path:line [種類]` の行を拾い、問題の正解(path・line・kind・flags)と突き合わせる。
- lines: 正解の行のうち、回答にあった割合。wrong: 回答に種類つきで並んでいて、正解に無い行の数(説明の文に出てきただけの行は数えない)
- kind: 回答にあった正解の行のうち、種類が合っていた割合。override は override、base-call は base-call、
  virtual-call は virtual-call、call は call か virtual-call(値の変数越しの呼び出しは、どちらの書き方もある)、
  decl・def は decl か def、member-call(UE5 の問題。受け手を 1 行ずつは確かめていない)は virtual-call か call
- 種類ごとの行の割合(ov・base・virt・call)と、inactive・template の印の付いた正解の行について、回答にあった数 / 印も合っていた数 / 全数。
  finact は、有効な正解の行に inactive と付けた数(flags に inactive? のある行は、索引の構成に入っていないことがあるテストの行で、数えない)
パスは、正解のパスの末尾と一致すれば同じとみなす(回答が先頭を省くことがある)。行を `path:34, 98` とまとめた回答も拾う。
"""
import argparse, collections, json, os, re

BENCH = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BENCH, 'runs13')
PATH = re.compile(r'([\w.+/\\-]+\.(?:h|hpp|hxx|cpp|cxx|cc|c|inl|mm|m))\b`?')
NUMS = re.compile(r'\s*[::]\s*(\d+(?:\s*[,、]\s*\d+)*)')
LIST = re.compile(r'(?<![\w.])(\d+(?:\s*[,、]\s*\d+)+)')
OK = {'override': {'override'}, 'base-call': {'base-call'}, 'virtual-call': {'virtual-call'}, 'call': {'call', 'virtual-call'},
      'decl': {'decl', 'def'}, 'def': {'decl', 'def'}, 'member-call': {'virtual-call', 'call'}}


def kind_of(text):
    """行の文面から種類を 1 つ決める。[...] があればその中、無ければ行の残りを見る。"""
    m = re.search(r'\[([^\]]*)\]', text)
    s = (m.group(1) if m else text).lower()
    for k in ('base-call', 'virtual-call', 'override'):
        if k in s:
            return k, s
    if re.search(r'\bcall\b', s):
        return 'call', s
    if re.search(r'\bdecl', s):
        return 'decl', s
    if re.search(r'\bdef', s):
        return 'def', s
    return '', s


def locs(line):
    """1 行の中の (path, 行番号) の並びと、最後の一致の終わりの位置。`path:12`・`path:12, 34`・`` `path`: 12 `` と、
    path が 1 つだけの行の「103, 115, 127 行」のような番号の並びを拾う。"""
    out, end = [], 0
    ms = list(PATH.finditer(line))
    for m in ms:
        n = NUMS.match(line, m.end())
        if n:
            out += [(m.group(1), int(x)) for x in re.split(r'\s*[,、]\s*', n.group(1))]
            end = n.end()
    if not out and len(ms) == 1:
        n = LIST.search(line, ms[0].end())
        if n:
            out += [(ms[0].group(1), int(x)) for x in re.split(r'\s*[,、]\s*', n.group(1))]
            end = n.end()
    return out, end


def answer_rows(text):
    rows = {}
    for line in text.splitlines():
        found, end = locs(line)
        if not found:
            continue
        k, s = kind_of(line[end:])
        for p, l in found:
            rows.setdefault((p.replace('\\', '/').lstrip('./'), l), (k, 'inactive' in s, 'template' in s))
    return rows


def score(task, text):
    ans = answer_rows(text)
    truth = task['truth']
    used, hit = set(), []
    for r in truth:
        for (p, l), v in ans.items():
            if l == r['line'] and (r['path'] == p or r['path'].endswith('/' + p)):
                hit.append((r, v))
                used.add((p, l))
                break
    same = lambda k: any(k[1] == r['line'] and (r['path'] == k[0] or r['path'].endswith('/' + k[0])) for r in truth)
    # 種類の付いた行だけ(説明の文に出てきただけの行は数えない)。正解の行を別の書き方で 2 度書いたものも数えない
    extra = sorted(k for k in set(ans) - used if ans[k][0] and not same(k))
    out = {'n': len(truth), 'hit': len(hit), 'wrong': len(extra)}
    judged = [(r, v) for r, v in hit if r['kind'] in OK]
    out['kind_ok'] = sum(v[0] in OK[r['kind']] for r, v in judged)
    out['kind_n'] = len(judged)
    for name, kinds in (('ov', {'override'}), ('base', {'base-call'}), ('virt', {'virtual-call', 'member-call'}), ('call', {'call'})):
        rs = [r for r in truth if r['kind'] in kinds]
        out[name] = (sum(r['kind'] in kinds for r, _ in hit), len(rs))
    for i, flag in ((1, 'inactive'), (2, 'template')):
        rs = [r for r in truth if flag in r['flags']]
        got = [(r, v) for r, v in hit if flag in r['flags']]
        out[flag] = (len(got), sum(v[i] for _, v in got), len(rs))
    out['finact'] = sum(v[1] for r, v in hit if 'inactive' not in r['flags'] and 'inactive?' not in r['flags'])
    out['wrong_rows'] = [f'{p}:{l}' for p, l in extra]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--round', required=True)
    ap.add_argument('--tasks', action='append', required=True)
    ap.add_argument('--conds', default='A,V,W')
    ap.add_argument('--runs', action='store_true', help='回ごとの値と、正解に無かった行も出す')
    a = ap.parse_args()
    tasks = {}
    for f in a.tasks:
        for t in json.load(open(os.path.join(BENCH, f), encoding='utf-8')):
            tasks[t['id']] = t
    recs = [json.loads(l) for l in open(os.path.join(RUNS, 'index.jsonl'), encoding='utf-8')]
    recs = [r for r in recs if r.get('round') == a.round and r['task'] in tasks and r['cond'] in a.conds.split(',')]
    agg = collections.defaultdict(list)
    for r in recs:
        p = os.path.join(RUNS, r['name'] + '.out.txt')
        s = score(tasks[r['task']], open(p, encoding='utf-8', errors='replace').read() if os.path.exists(p) else '')
        agg[(r['task'], r['cond'])].append(s)
        if a.runs:
            print(f"{r['name']} lines {s['hit']}/{s['n']} wrong {s['wrong']} kind {s['kind_ok']}/{s['kind_n']} "
                  f"inactive {s['inactive']} template {s['template']} finact {s['finact']}  {' '.join(s['wrong_rows'][:12])}")
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0
    frac = lambda ss, k: (f"{mean([s[k][0] / s[k][1] for s in ss]):.2f}" if ss[0][k][1] else '-')
    print('task\tcond\tn\tlines\twrong\tkind\tov\tbase\tvirt\tcall\tinactive(listed/tagged/all)\ttemplate(listed/tagged/all)\tfinact')
    for (t, c), ss in sorted(agg.items()):
        flag = lambda k: (f"{mean([s[k][0] for s in ss]):.1f}/{mean([s[k][1] for s in ss]):.1f}/{ss[0][k][2]}" if ss[0][k][2] else '-')
        kind = mean([s['kind_ok'] / s['kind_n'] for s in ss if s['kind_n']])
        print(f"{t}\t{c}\t{len(ss)}\t{mean([s['hit'] / s['n'] for s in ss]):.2f}\t{mean([s['wrong'] for s in ss]):.1f}\t{kind:.2f}\t"
              f"{frac(ss, 'ov')}\t{frac(ss, 'base')}\t{frac(ss, 'virt')}\t{frac(ss, 'call')}\t{flag('inactive')}\t{flag('template')}\t"
              f"{mean([s['finact'] for s in ss]):.1f}")


if __name__ == '__main__':
    main()
