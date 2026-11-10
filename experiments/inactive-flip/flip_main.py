"""flip.py の本体を並列で回す。
usage: python -I flip_main.py <ルート> <索引.tsv> <compile_commands.json> <作業フォルダ> [--all] [--step N] [--limit N]
       [--passes N] [--jobs N] [<相対パス>...]
"""
import collections, concurrent.futures, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv, ARGV = sys.argv[:1], sys.argv[1:]
import flipcore as F

CODE = re.compile(rb'[A-Za-z_]\w*')


def code_lines(data, lines):
    """識別子のある行(空行・// だけの行・前処理の行を除く)。"""
    src = data.split(b'\n')
    out = set()
    for ln in lines:
        if ln - 1 >= len(src):
            continue
        s = src[ln - 1].strip()
        if not s or s.startswith(b'//') or s.startswith(b'#') or s.startswith(b'*') or s.startswith(b'/*'):
            continue
        if CODE.search(s):
            out.add(ln)
    return out


def main():
    a = ARGV
    opt = {'--passes': 4, '--limit': 0, '--step': 1, '--jobs': 6}
    for k in list(opt):
        if k in a:
            i = a.index(k)
            opt[k] = int(a[i + 1])
            del a[i:i + 2]
    do_all = '--all' in a
    a = [x for x in a if x != '--all']
    root, index, cdbp, work = a[:4]
    work = os.path.abspath(work)
    files = a[4:]
    base, comments, tus, readers = F.load_base(index)
    cdb = {os.path.normcase(os.path.normpath(os.path.join(e.get('directory', ''), e['file']))): e
           for e in json.load(open(cdbp, encoding='utf-8')) if 'file' in e}
    if do_all:
        files = sorted(base)
    files = files[::opt['--step']]
    if opt['--limit']:
        files = files[:opt['--limit']]
    tot = collections.Counter()
    all_rows = []

    def one(arg):
        i, rel = arg
        if rel not in base:
            return rel, None, '無効な範囲が無い'
        ap = os.path.normcase(os.path.normpath(os.path.join(root, rel)))
        cands = sorted((t for t in readers.get(rel, ()) if os.path.normcase(os.path.normpath(t)) in cdb),
                       key=lambda t: (os.path.normcase(os.path.normpath(t)) != ap, not tus.get(t, (False, 0))[0],
                                      tus.get(t, (False, 9e9))[1]))
        if not cands:
            return rel, None, '読む TU が無い'
        entry = cdb[os.path.normcase(os.path.normpath(cands[0]))]
        res = F.flip_file(root, rel, base[rel], comments.get(rel, ()), entry, os.path.join(work, 'w', str(i)),
                          opt['--passes'])
        with open(os.path.join(root, rel), 'rb') as f:
            data = f.read()
        return rel, res, data

    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(opt['--jobs']) as ex:
        for rel, res, extra in ex.map(one, enumerate(files)):
            if res is None:
                print(f'{rel}: {extra}', flush=True)
                tot['no_tu' if 'TU' in extra else 'no_range'] += 1
                continue
            target, uncovered, dead, rows, stats = res
            covered = target - uncovered
            tc, cc = code_lines(extra, target), code_lines(extra, covered)
            row_ln = set()
            for r in rows:
                c = r.split('\t')
                row_ln.add(int(c[3].rsplit(':', 2)[1]))
            tot['files'] += 1
            tot['target'] += len(target)
            tot['dead'] += len(dead)
            tot['covered'] += len(covered)
            tot['target_code'] += len(tc)
            tot['covered_code'] += len(cc)
            tot['code_with_rows'] += len(cc & row_ln)
            tot['rows'] += len(rows)
            tot['runs'] += len(stats)
            tot['runs_err'] += sum(1 for s in stats if s[4] > 0)
            tot['runs_fail'] += sum(1 for s in stats if s[4] < 0)
            tot['sec'] += sum(s[5] for s in stats)
            tot['bytes'] += sum(len(r.encode()) + 1 for r in rows)
            tot['full' if not uncovered else 'partial' if covered else 'none'] += 1
            all_rows += [(rel, n, r) for r, n in rows.items()]
            print(f'{rel}: 無効 {len(target)} 行(コード {len(tc)}、+ #if 0 が {len(dead)} 行) -> 有効 {len(covered)} 行'
                  f'(コード {len(cc)}、行のある行 {len(cc & row_ln)})、索引の行 {len(rows)}  '
                  + ' '.join(f'[p{n} 分岐{c} +{nw}行 +{g}件 err{er} {s:.1f}s]' for n, c, nw, g, er, s in stats),
                  flush=True)
    tot['wall'] = int(time.time() - t0)
    print('合計:', dict(tot))
    with open(os.path.join(work, 'flip-rows.tsv'), 'w', encoding='utf-8', newline='\n') as f:
        for rel, n, r in all_rows:
            f.write(f'{n}\t{r}\n')


main()
