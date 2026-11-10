"""v13 の問題の対象を、決めた手順で無作為に選ぶ(段階 1: C++ 以外と、索引の無い C++)。

usage: python -I sample.py <出力の json>
各リポジトリについて、手書きのソース(テスト・生成物を除く)から公開の関数・メソッドの定義を正規表現で拾い、
定義のファイル以外の 1 か所以上と、合わせて 2 か所以上から名前で呼ばれているものを候補にする。
候補を名前で並べ、乱数の種 13 で 2 つを選ぶ(1 つ目を「使っている箇所」、2 つ目を「引数を足す」に使う)。
「説明」には、候補の定義があるファイルから 1 つを選ぶ。cxxref の索引は使わない。
"""
import json, os, random, re, sys

DATA = os.environ.get('CRI_BENCH_DATA', r'E:\dev\bench-data\cxxref')
REPOS = {
    'requests': {'root': os.path.join(DATA, 'v13', 'requests'), 'src': ['src/requests'], 'ext': ('.py',),
                 'def': re.compile(r'^\s*def ([a-zA-Z][A-Za-z0-9_]*)\s*\(', re.M)},
    'newtonsoft': {'root': os.path.join(DATA, 'v13', 'newtonsoft'), 'src': ['Src/Newtonsoft.Json'], 'ext': ('.cs',),
                   'def': re.compile(r'^\s*(?:public|internal)\s+(?:static\s+|virtual\s+|override\s+|sealed\s+)*[\w<>\[\],.? ]+?\s+([A-Z]\w*)\s*(?:<[^>]*>)?\s*\(', re.M)},
    'zod': {'root': os.path.join(DATA, 'v13', 'zod'), 'src': ['src'], 'ext': ('.ts',),
            'def': re.compile(r'^\s*export\s+(?:async\s+)?function\s+([A-Za-z]\w*)\s*[<(]', re.M)},
    'a-noindex': {'root': None, 'src': ['runtime'], 'ext': ('.h', '.cpp'),  # コードベース A。場所は bench/local/codebase-a.json
                  'def': re.compile(r'^\s*(?:static\s+|inline\s+|virtual\s+)*(?:[\w:<>,*& ]+?)\s+([A-Z]\w*)\s*\([^;{)]*\)\s*(?:const\s*)?;', re.M)},
}
_A = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'local', 'codebase-a.json')
if os.path.exists(_A):
    REPOS['a-noindex']['root'] = json.load(open(_A, encoding='utf-8'))['path']
else:
    del REPOS['a-noindex']
# 言語の組み込みと同じ名前(Python の next など)は、名前の一致が組み込みの呼び出しと混ざるので外す
import builtins
BUILTINS = set(dir(builtins))
SKIP = re.compile(r'(^|/)(tests?|Tests?|__tests__|benchmarks?|bench|gen|reflection_generated|third_party|Newtonsoft\.Json\.Tests)(/|$)|\.test\.|\.spec\.|_test\.')


def files(cfg):
    out = []
    for s in cfg['src']:
        for d, dirs, fs in os.walk(os.path.join(cfg['root'], s)):
            dirs.sort()
            for f in sorted(fs):
                rel = os.path.relpath(os.path.join(d, f), cfg['root']).replace('\\', '/')
                if f.endswith(cfg['ext']) and not SKIP.search(rel):
                    out.append(rel)
    return out


_ALL = {}


def all_files(cfg):
    """リポジトリ全体(テストを含む)の、同じ言語のファイルの中身"""
    if cfg['root'] not in _ALL:
        out = {}
        for d, dirs, fs in os.walk(cfg['root']):
            dirs[:] = sorted(x for x in dirs if not x.startswith('.') and x not in ('vcpkg_installed', 'node_modules'))
            for f in sorted(fs):
                if f.endswith(cfg['ext']):
                    fp = os.path.join(d, f)
                    out[os.path.relpath(fp, cfg['root']).replace(os.sep, '/')] = open(fp, encoding='utf-8', errors='replace').read().splitlines()
        _ALL[cfg['root']] = out
    return _ALL[cfg['root']]


def uses(cfg, name):
    """名前の一致(定義の行を除く)の位置。リポジトリ全体(テストを含む)"""
    pat = re.compile(r'\b' + re.escape(name) + r'\s*[(<]')
    hits = []
    for path, lines in all_files(cfg).items():
        for i, text in enumerate(lines, 1):
            if pat.search(text) and not re.search(r'\b(def|function|class)\s+' + re.escape(name) + r'\b', text):
                hits.append((path, i))
    return hits


def main():
    rng = random.Random(13)
    out = {}
    for key, cfg in REPOS.items():
        defs = {}
        for rel in files(cfg):
            text = open(os.path.join(cfg['root'], rel), encoding='utf-8', errors='replace').read()
            for m in cfg['def'].finditer(text):
                defs.setdefault(m.group(1), set()).add(rel)
        cands = []
        for name in sorted(defs):
            if len(defs[name]) != 1 or len(name) < 4 or name in BUILTINS:
                continue  # 同じ名前の定義が複数のファイルにあるものは、名前だけでは問題にしにくいので外す
            h = uses(cfg, name)
            dfile = next(iter(defs[name]))
            if len(h) >= 2 and any(p != dfile for p, _ in h):
                cands.append({'name': name, 'def_file': dfile, 'uses': len(h)})
        pick = rng.sample(cands, 2)
        expl = rng.choice(sorted({c['def_file'] for c in cands}))
        out[key] = {'candidates': len(cands), 'usage': pick[0], 'change': pick[1], 'explain_file': expl}
        print(key, len(cands), pick[0]['name'], pick[1]['name'], expl, file=sys.stderr)
    json.dump(out, open(sys.argv[1], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
