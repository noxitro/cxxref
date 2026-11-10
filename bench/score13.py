"""v13 の採点と集計。runs13/index.jsonl の各回に、問題の種類ごとの採点を付け、条件ごとにまとめる。

usage: python -I score13.py --tasks v13/tasks-stage1.json [--tasks ...] [--round v13] [--compile]

採点(cxxref の索引は使わない)
- usage・impact: 正解の呼び出し(問題の truth、無ければ名前の一致から作ったもの)のうち、回答が挙げたファイルの割合
  (file_recall)と、ファイルと行(±2 行)まで挙げた割合(line_recall)
- change: 正解の呼び出しの行が差分で書き換えられた割合(callers_changed)と、引数の名前が宣言・定義の側に入ったか。
  --compile を付けると C++ は差分を overlay で当てて clang-cl /Zs にかけ、元には無いエラーが 0 件か(builds)も見る
- log・test: --compile で builds だけ
- explain: 採点しない
"""
import argparse, collections, json, os, re, statistics as st, sys

BENCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BENCH, 'v13'))
sys.path.insert(0, BENCH)
import run13


def diff_files(diff):
    """{path: {'removed': set(古い行番号), 'added': [新しい行の中身]}}。新しいファイルは removed が空"""
    out, cur, old = {}, None, 0
    for line in diff.splitlines():
        if line.startswith('diff --git '):
            cur = None
        elif line.startswith('+++ '):
            p = line[4:].strip()
            cur = p[2:] if p.startswith('b/') else None
            if cur:
                out.setdefault(cur, {'removed': set(), 'added': []})
        elif line.startswith('@@'):
            m = re.match(r'@@ -(\d+)', line)
            old = int(m.group(1)) if m else 0
        elif cur is not None:
            if line.startswith('-') and not line.startswith('---'):
                out[cur]['removed'].add(old)
                old += 1
            elif line.startswith('+') and not line.startswith('+++'):
                out[cur]['added'].append(line[1:])
            else:
                old += 1
    return out


def apply_diff(ws, diff):
    """差分を当てた後の中身 {path: text}。git apply を一時フォルダで行う(作業フォルダは触らない)"""
    import subprocess, tempfile
    files = diff_files(diff)
    tmp = tempfile.mkdtemp(prefix='score13-')
    for p in files:
        src = os.path.join(ws, p.replace('/', os.sep))
        if os.path.exists(src):
            dst = os.path.join(tmp, p.replace('/', os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            orig = subprocess.run(['git', '-C', ws, 'show', 'HEAD:' + p], capture_output=True).stdout
            open(dst, 'wb').write(orig)
    pf = os.path.join(tmp, '_patch.diff')
    open(pf, 'w', encoding='utf-8', newline='\n').write(diff)
    r = subprocess.run(['git', 'apply', '--whitespace=nowarn', '--ignore-whitespace', pf], cwd=tmp, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    out = {}
    for p in files:
        fp = os.path.join(tmp, p.replace('/', os.sep))
        if os.path.exists(fp):
            out[p] = open(fp, encoding='utf-8', errors='replace', newline='').read()
    return out


def mentioned(answer, truth):
    a = answer.replace('\\', '/')
    files = {t.rsplit(':', 1)[0] for t in truth}
    fhit = {f for f in files if f in a or os.path.basename(f) in a}
    lhit = set()
    for t in truth:
        f, l = t.rsplit(':', 1)
        b = re.escape(os.path.basename(f))
        for m in re.finditer(b + r'[:(,\s]*(?:line\s*)?(\d+)', a):
            if abs(int(m.group(1)) - int(l)) <= 2:
                lhit.add(t)
    return len(fhit) / len(files) if files else None, len(lhit) / len(truth) if truth else None


def score(task, rec, do_compile):
    out = {}
    truth = task.get('truth') or []
    ans = rec.get('answer', '')
    diff = open(os.path.join(run13.RUNS, rec['name'] + '.diff'), encoding='utf-8').read() if os.path.exists(
        os.path.join(run13.RUNS, rec['name'] + '.diff')) else ''
    kind = task['kind']
    if kind in ('usage', 'impact') and truth:
        out['file_recall'], out['line_recall'] = mentioned(ans, truth)
    if kind == 'change':
        df = diff_files(diff)
        hit = 0
        for t in truth:
            f, l = t.rsplit(':', 1)
            if f in df and any(abs(int(l) - x) <= 2 for x in df[f]['removed']):
                hit += 1
        out['callers_changed'] = hit / len(truth) if truth else None
        pname = re.sub(r'[^A-Za-z0-9_]', ' ', task['change_spec']['param']).split()
        pname = [w for w in pname if w not in ('bool', 'boolean', 'int', 'const', 'std', 'string')]
        dfile = task['def_file']
        out['param_added'] = bool(pname) and any(pname[-1] in x for p, d in df.items() for x in d['added'] if p == dfile or p.endswith(('.h', '.cpp', '.py', '.ts', '.cs')))
    if do_compile and kind in ('change', 'log', 'test') and task['repo'] == 'a':
        import cxxcheck
        w = run13.WORKSPACES[task['repo']]
        files = apply_diff(w['path'], diff) if diff else {}
        if files is None:
            out['builds'] = False
        elif not files:
            out['builds'] = None
        else:
            cdb = cxxcheck.load_cdb(w['path'])
            tus = cxxcheck.tus_including(w['path'], w['index'], list(files))
            # 元の写しの結果は変わらないので、TU ごとに runs13/cxxcheck-base.json に貯めて使い回す
            bf = os.path.join(run13.RUNS, 'cxxcheck-base.json')
            cache = json.load(open(bf, encoding='utf-8')) if os.path.exists(bf) else {}
            miss = [t for t in tus if t not in cache]
            if miss:
                for t, (rc, es) in cxxcheck.check(cdb, miss, None, jobs=4).items():
                    cache[t] = [rc, [list(x) for x in es]]
                json.dump(cache, open(bf, 'w', encoding='utf-8'))
            base = {t: (cache[t][0], [tuple(x) for x in cache[t][1]]) for t in tus if t in cache}
            after = cxxcheck.check(cdb, tus, cxxcheck.make_overlay(w['path'], files), jobs=4)
            e = lambda r: {(p, l, m) for rc, es in r.values() for p, l, _, m in es if 'reflection_generated' not in p}
            new = e(after) - e(base)
            out['builds'] = not new
            out['new_errors'] = len(new)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tasks', action='append', required=True)
    ap.add_argument('--round', default='v13')
    ap.add_argument('--compile', action='store_true')
    a = ap.parse_args()
    tasks = {}
    for f in a.tasks:
        for t in json.load(open(os.path.join(BENCH, f), encoding='utf-8')):
            tasks[t['id']] = t
    rows = []
    for line in open(os.path.join(run13.RUNS, 'index.jsonl'), encoding='utf-8'):
        r = json.loads(line)
        if r['round'] != a.round or r['task'] not in tasks or r['rep'] == 0 or r.get('exit') != 0:
            continue
        r.update(score(tasks[r['task']], r, a.compile))
        rows.append(r)
    keys = ['name', 'task', 'cond', 'rep', 'sec', 'calls', 'first_input', 'input', 'cache_read', 'output', 'ai_credits', 'refs_calls',
            'diff_lines', 'file_recall', 'line_recall', 'callers_changed', 'param_added', 'builds', 'new_errors']
    with open(os.path.join(run13.RUNS, f'summary-{a.round}.tsv'), 'w', encoding='utf-8') as f:
        f.write('\t'.join(keys) + '\n')
        for r in rows:
            f.write('\t'.join(str(r.get(k, '')) for k in keys) + '\n')
    sys.stdout.reconfigure(encoding='utf-8')
    by = collections.defaultdict(list)
    for r in rows:
        by[(r['task'], r['cond'])].append(r)
    m = lambda v: f'{st.mean(v):.2f}' if v else '-'
    print('task  cond  n  calls  first_in  input  aic  sec  refs  file_rec  line_rec  callers  param  builds')
    for (t, c), v in sorted(by.items()):
        g = lambda k: [x[k] for x in v if isinstance(x.get(k), (int, float)) and not isinstance(x.get(k), bool)]
        b = lambda k: [int(x[k]) for x in v if isinstance(x.get(k), bool)]
        print(f"{t:24} {c} {len(v):2} {m(g('calls')):>5} {m(g('first_input')):>9} {m(g('input')):>9} {m(g('ai_credits')):>5} {m(g('sec')):>5} "
              f"{m(g('refs_calls')):>4} {m(g('file_recall')):>5} {m(g('line_recall')):>5} {m(g('callers_changed')):>5} {m(b('param_added')):>5} {m(b('builds')):>5}")


if __name__ == '__main__':
    main()
