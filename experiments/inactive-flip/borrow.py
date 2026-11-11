"""試作: どの TU からも読まれない .cpp を、いちばん近い TU のコンパイルのコマンドを借りて解析する。
usage: python -I borrow.py <ルート> <索引.tsv> <compile_commands.json> <作業フォルダ> [--sample N | <相対パス>...] [--jobs N]
       [--excl <フォルダー>]... [--fix-includes N]
--fix-includes N: 「ヘッダーが見つからない」で止まったら、ルートの中からそのヘッダーを探して -I を足し、N 回まで読み直す。
"""
import collections, concurrent.futures, json, os, random, re, shutil, subprocess, sys, time

sys.stdout.reconfigure(encoding='utf-8')
LTIDX = os.environ.get('LTIDX') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'indexer', 'build', 'ltidx.exe')


def main():
    a = sys.argv[1:]
    opt = {'--sample': 0, '--jobs': 8, '--fix-includes': 0}
    for k in list(opt):
        if k in a:
            i = a.index(k)
            opt[k] = int(a[i + 1])
            del a[i:i + 2]
    excl = []
    while '--excl' in a:
        i = a.index('--excl')
        excl.append(a[i + 1].rstrip('/') + '/')
        del a[i:i + 2]
    root, index, cdbp, work = a[:4]
    work = os.path.abspath(work)
    root = os.path.abspath(root)
    files = a[4:]
    seen = set()
    with open(index + '.deps.tsv', 'rb') as f:
        for raw in f:
            i = raw.find(b'\t')
            seen.add(raw[i + 1:].rstrip(b'\r\n').decode('utf-8', 'replace').lower())
    ok_tu = {}
    with open(index + '.tu.tsv', encoding='utf-8', errors='replace') as f:
        next(f)
        for line in f:
            c = line.rstrip('\n').split('\t')
            ok_tu[os.path.normcase(os.path.normpath(c[0]))] = (c[1] == '1', float(c[3]))
    by_dir = collections.defaultdict(list)
    for e in json.load(open(cdbp, encoding='utf-8')):
        if 'file' not in e:
            continue
        p = os.path.normpath(os.path.join(e.get('directory', ''), e['file']))
        try:
            rel = os.path.relpath(p, root).replace('\\', '/')
        except ValueError:
            continue
        if rel.startswith('..') or not ok_tu.get(os.path.normcase(p), (False, 0))[0]:
            continue
        by_dir[os.path.dirname(rel).lower()].append((ok_tu[os.path.normcase(p)][1], p, e))
    # フォルダーの下にある TU(子孫も含む)を引けるように、祖先にも登録する
    under = collections.defaultdict(list)
    for d, lst in by_dir.items():
        x = d
        while True:
            under[x].append(min(lst, key=lambda t: t[0]))
            if not x:
                break
            x = os.path.dirname(x)

    if not files:
        for d, dirs, fs in os.walk(root):
            rd = os.path.relpath(d, root).replace('\\', '/')
            rd = '' if rd == '.' else rd
            dirs[:] = [x for x in dirs if not x.startswith('.')
                       and not ((rd + '/' + x).lstrip('/') + '/').startswith(tuple(excl))]
            for fn in fs:
                if os.path.splitext(fn)[1].lower() in ('.cpp', '.cc', '.cxx'):
                    rel = (rd + '/' + fn).lstrip('/')
                    if rel.lower() not in seen:
                        files.append(rel)
        files.sort()
        if opt['--sample']:
            random.seed(7)
            files = sorted(random.sample(files, min(opt['--sample'], len(files))))

    # --fix-includes: 見つからないヘッダーをルートの中から探して、-I を足して読み直す
    hdrs = collections.defaultdict(list)
    if opt['--fix-includes']:
        for d, dirs, fs in os.walk(root):
            rd = os.path.relpath(d, root).replace('\\', '/')
            rd = '' if rd == '.' else rd
            dirs[:] = [x for x in dirs if not x.startswith('.')
                       and not ((rd + '/' + x).lstrip('/') + '/').startswith(tuple(excl))]
            for fn in fs:
                if os.path.splitext(fn)[1].lower() in ('.h', '.hpp', '.hxx', '.inl', '.hh', '.ipp'):
                    hdrs[fn.lower()].append((rd + '/' + fn).lstrip('/'))

    def find_inc(missing, rel):
        m = missing.replace('\\', '/').lower()
        cands = [p for p in hdrs.get(os.path.basename(m), ()) if ('/' + p.lower()).endswith('/' + m)]
        if not cands:
            return None
        best = max(cands, key=lambda p: len(os.path.commonprefix([p.lower(), rel.lower()])))
        return os.path.join(root, best[:len(best) - len(m)].rstrip('/').replace('/', os.sep)).rstrip(os.sep)

    def pick(rel):
        x, hop = os.path.dirname(rel).lower(), 0
        while True:
            if under.get(x):
                # 同じ深さなら、パスが近いもの(共通の接頭辞が長いもの)
                best = max(under[x], key=lambda t: len(os.path.commonprefix(
                    [os.path.relpath(t[1], root).replace('\\', '/').lower(), rel.lower()])))
                return best, hop
            if not x:
                return None, hop
            x, hop = os.path.dirname(x), hop + 1

    def one(arg):
        i, rel = arg
        best, hop = pick(rel)
        if best is None:
            return rel, None
        _, src, e0 = best
        ap = os.path.join(root, rel.replace('/', os.sep))
        extra, sec, tries = [], 0.0, 0
        while True:
            e = dict(e0)
            inc = [f'-I{x}' for x in extra]
            if 'arguments' in e:
                args = []
                for x in e['arguments']:
                    if os.path.normcase(os.path.normpath(os.path.join(e.get('directory', ''), x))) == os.path.normcase(src):
                        args += inc + [ap]
                    else:
                        args.append(x)
                e['arguments'] = args
            else:
                cmd = e['command']
                j = cmd.lower().rfind(os.path.basename(src).lower())
                k = cmd.rfind(' ', 0, j) + 1
                e['command'] = cmd[:k] + ''.join(f'"{x}" ' for x in inc) + '"' + ap + '"'
            e['file'] = ap
            w = os.path.join(work, str(i))
            shutil.rmtree(w, ignore_errors=True)
            os.makedirs(w)
            json.dump([e], open(os.path.join(w, 'compile_commands.json'), 'w', encoding='utf-8'))
            out = os.path.join(w, 'out.tsv')
            t0 = time.time()
            subprocess.run([LTIDX, '-p', w, '-root', root, '-o', out, '-j', '1'], capture_output=True)
            sec += time.time() - t0
            tries += 1
            rows, kinds, errors, first = 0, collections.Counter(), -1, ''
            nb = 0
            if os.path.exists(out):
                with open(out, 'rb') as f:
                    for raw in f:
                        c = raw.rstrip(b'\r\n').split(b'\t')
                        if len(c) >= 4 and c[3].rsplit(b':', 2)[0].decode('utf-8', 'replace') == rel:
                            rows += 1
                            nb += len(raw)
                            kinds[c[2].decode()] += 1
                with open(out + '.tu.tsv', encoding='utf-8', errors='replace') as f:
                    next(f)
                    for line in f:
                        c = line.rstrip('\n').split('\t')
                        errors, first = int(c[2]), c[5]
            m = re.search(r"'([^']+)' file not found", first)
            if errors <= 0 or not m or len(extra) >= opt['--fix-includes']:
                break
            d = find_inc(m.group(1), rel)
            if d is None or d in extra:
                break
            extra.append(d)
        with open(ap, 'rb') as f:
            nl = f.read().count(b'\n') + 1
        return rel, (hop, os.path.relpath(src, root).replace('\\', '/'), errors, first, rows, nb, nl, sec, dict(kinds),
                     len(extra), tries)

    tot = collections.Counter()
    firsts = collections.Counter()
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(opt['--jobs']) as ex:
        for rel, r in ex.map(one, enumerate(files)):
            if r is None:
                print(f'{rel}: 借りる TU が無い', flush=True)
                tot['no_tu'] += 1
                continue
            hop, src, errors, first, rows, nb, nl, sec, kinds, nfix, tries = r
            tot['fixed_dirs'] += nfix
            tot['runs'] += tries
            key = 'clean' if errors == 0 else 'errors' if rows else 'no_rows'
            tot[key + '_lines'] += nl
            tot[key + '_rows'] += rows
            tot['files'] += 1
            tot['lines'] += nl
            tot['rows'] += rows
            tot['bytes'] += nb
            tot['sec'] += sec
            tot['clean' if errors == 0 else 'errors' if rows else 'no_rows'] += 1
            if errors:
                firsts[first[:70]] += 1
            print(f'{rel}: {nl} 行、借りた TU {src}({hop} 段上)、err {errors}、索引の行 {rows}、-I を {nfix} 個足した、{sec:.1f}s  {first[:80]}',
                  flush=True)
    tot['wall'] = int(time.time() - t0)
    print('合計:', dict(tot))
    print('最初のエラー(上位):', firsts.most_common(8))


main()
