"""v13 の段階 2(Nox)の対象を、決めた手順で無作為に選び、正解を作る。

usage: python -I sample2.py <出力の json>

候補: runtime/ の手書きのヘッダーに宣言が 1 つ、.cpp に定義が 1 つある関数(オーバーロード・仮想関数・テンプレート・
コンストラクター・演算子を除く)で、定義のファイルの外から 2 か所以上呼ばれているもの。候補を挙げるのには ltidx の索引の
宣言・定義・呼び出しの行を使う(数えるだけ。正解には使わない)。
正解: 宣言と定義に引数 `int bench_extra` を足した中身を VFS の overlay で差し替えて、そのヘッダーを読む全 TU を
clang-cl /Zs にかけ、差し替えない場合には無いエラーの位置(生成物を除く)を、直す必要のある呼び出しとする。
候補を名前で並べ、乱数の種 13 で混ぜ、先頭から正解が 2 か所以上あるものを 5 つ取る(先の 3 つを変更、後の 2 つを影響の調査)。
参照と関係の無い作業の対象(説明するファイル、ログを足す関数、テストを足すファイル)も同じ乱数で選ぶ。
"""
import collections, json, os, random, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cxxcheck

WS = r'E:\dev\bench-data\cxxref\nox-20261103'
IDX = r'E:\dev\bench-data\cxxref\index\nox-v6.tsv'
GEN = re.compile(r'(^|/)(reflection_generated|gen)/')
TEST = re.compile(r'(^|/)(test|tests|bench)/')


def loc(s):
    p, l, c = s.rsplit(':', 2)
    return p, int(l), int(c)


def main():
    decl, defs, calls, over, shorts = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list), set(), collections.defaultdict(set)
    for line in open(IDX, encoding='utf-8', errors='replace'):
        f = line.rstrip('\n').split('\t')
        if len(f) < 5:
            continue
        name, role, ctx, where, text = f[0], f[1], f[2], f[3], f[4]
        if not where.startswith('runtime/') or GEN.search(where):
            continue
        if role == 'override':
            over.add(name)
            over.add(ctx)
        elif role == 'decl':
            decl[name].append((where, text))
        elif role == 'def':
            defs[name].append((where, text))
        elif role == 'call':
            calls[name].append(where)
        if role in ('decl', 'def'):
            shorts[name.rsplit('::', 1)[-1]].add(name)
    cands = []
    for name in sorted(decl):
        short = name.rsplit('::', 1)[-1]
        cls = name.rsplit('::', 2)[-2] if name.count('::') >= 1 else ''
        if name in over or '<' in name or short.startswith(('operator', '~')) or short == cls:
            continue
        if len(decl[name]) != 1 or len(defs[name]) != 1:
            continue
        dw, dt = decl[name][0]
        fw, ft = defs[name][0]
        if not loc(dw)[0].endswith('.h') or not loc(fw)[0].endswith('.cpp') or TEST.search(dw):
            continue
        if re.search(r'\b(virtual|template|static_assert)\b|=\s*(default|delete|0)\b', dt + ' ' + ft):
            continue
        outside = [c for c in calls[name] if loc(c)[0] != loc(fw)[0]]
        if len(outside) < 2:
            continue
        cands.append({'name': name, 'short': short, 'decl': dw, 'def': fw, 'calls_index': len(calls[name]),
                      'same_short': len(shorts[short])})
    print('candidates', len(cands), file=sys.stderr)
    rng = random.Random(13)
    order = cands[:]
    rng.shuffle(order)
    cdb = cxxcheck.load_cdb(WS)
    picked, rejected = [], []
    for c in order:
        if len(picked) == 5:
            break
        files = {}
        for key in ('decl', 'def'):
            p, l, col = loc(c[key])
            text = files.get(p) or open(os.path.join(WS, p), encoding='utf-8', newline='').read()
            files[p] = cxxcheck.add_param(text, l, col, 'int bench_extra')
        tus = cxxcheck.tus_including(WS, IDX, list(files))
        base = cxxcheck.check(cdb, tus, None, jobs=4)
        ov = cxxcheck.make_overlay(WS, files)
        after = cxxcheck.check(cdb, tus, ov, jobs=4)
        def errs(r):
            ws = os.path.normcase(WS)
            return {(os.path.relpath(p, WS).replace(os.sep, '/'), l) for rc, es in r.values() for p, l, _, m in es if p.startswith(ws)}
        new = sorted(e for e in errs(after) - errs(base) if not GEN.search(e[0]) and e[0] not in (loc(c['decl'])[0], loc(c['def'])[0]) or
                     (e[0] in (loc(c['decl'])[0], loc(c['def'])[0]) and e[1] not in (loc(c['decl'])[1], loc(c['def'])[1])))
        new = [e for e in new if not GEN.search(e[0])]
        c['truth'] = [f'{p}:{l}' for p, l in new]
        c['tus'] = len(tus)
        print(c['name'], len(tus), 'truth', len(new), file=sys.stderr)
        (picked if len(new) >= 2 else rejected).append(c)
    rest = random.Random(14)
    files_h = sorted({loc(c['decl'])[0] for c in cands})
    tests = sorted(os.path.relpath(t, WS).replace(os.sep, '/') for t in cdb if TEST.search(os.path.relpath(t, WS).replace(os.sep, '/'))
                   and not GEN.search(t.replace(os.sep, '/')))
    others = [c for c in cands if c not in picked]
    out = {'candidates': len(cands), 'change': picked[:3], 'impact': picked[3:5], 'rejected': rejected,
           'explain_file': rest.choice(files_h), 'log_function': rest.choice(others), 'test_file': rest.choice(tests)}
    json.dump(out, open(sys.argv[1], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
