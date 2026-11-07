"""C++ の写しを、ファイルを書き換えずに clang-cl の構文チェック(/Zs)にかける。ファイルの差し替えは VFS の overlay で行う。

v13 の段階 2 の正解づくりと採点に使う(cxxref の索引は使わない)。
- tus_including(ws, paths): そのファイルを読む TU。compile_commands.json の全 TU を -E せずに決めるため、ltidx の .deps.tsv を使う
  (どの TU がどのヘッダーを読むかだけ。呼び出しの解決には使わない)
- check(ws, tus, overlay): 各 TU のエラー(path, line, col, message)の一覧
"""
import concurrent.futures as cf, json, os, re, subprocess, tempfile

LLVM = os.environ.get('CRI_BENCH_LLVM', r'E:\tools\llvm-23.1.4')
CLANG_CL = os.path.join(LLVM, 'bin', 'clang-cl.exe')
DROP = re.compile(r'^/(Yu|Yc|Fp|Fo|Fd|Fa|Fe|FR|Fr|Zi|ZI|Z7|MP|Gm|FS)')  # 大文字小文字を区別する(/fp:precise は残す)
DIAG = re.compile(r'^(?P<path>[A-Za-z]:[^()]+?)\((?P<line>\d+),(?P<col>\d+)\)\s*:\s*error\s*:\s*(?P<msg>.*)$')
IDLE = 0x00000040  # IDLE_PRIORITY_CLASS


def load_cdb(ws, cdb='.cdb'):
    d = json.load(open(os.path.join(ws, cdb, 'compile_commands.json'), encoding='utf-8'))
    out = {}
    for e in d:
        if 'arguments' not in e or not e['file'].lower().endswith(('.cpp', '.cc', '.cxx', '.c')):
            continue
        out[os.path.normcase(os.path.abspath(e['file']))] = e
    return out


def args_for(e, overlay=None):
    a = [CLANG_CL]
    skip = False
    for x in e['arguments'][1:]:
        if skip:
            skip = False
            continue
        if x in ('/c', '-c'):
            continue
        if x in ('/Fo', '/Fd', '/Fp'):
            skip = True
            continue
        if DROP.match(x) or os.path.normcase(x) == os.path.normcase(e['file']):
            continue
        a.append(x)
    a += ['/Zs', '-ferror-limit=0', '-Wno-everything', '/clang:-fno-color-diagnostics', '-fdiagnostics-format=msvc']
    if overlay:
        a += ['-Xclang', '-ivfsoverlay', '-Xclang', overlay]
    a.append(e['file'])
    return a


def tus_including(ws, index_base, paths):
    """paths(ルートからの相対パス、/ 区切り)のどれかを読む TU(絶対パス、normcase)。.cpp 自身も含む"""
    want = {p.replace('\\', '/') for p in paths}
    tus = set()
    for p in want:
        if p.lower().endswith(('.cpp', '.cc', '.cxx', '.c')):
            tus.add(os.path.normcase(os.path.join(ws, p.replace('/', os.sep))))
    with open(index_base + '.deps.tsv', encoding='utf-8', errors='replace') as f:
        next(f)
        for line in f:
            tu, _, dep = line.rstrip('\n').partition('\t')
            if dep in want:
                tus.add(os.path.normcase(tu))
    return tus


def make_overlay(ws, files):
    """files: {相対パス: 新しい中身}。overlay の yaml(json)と一時フォルダを作る。戻り値は overlay のパス"""
    tmp = tempfile.mkdtemp(prefix='cxxcheck-')
    roots = []
    for i, (rel, text) in enumerate(sorted(files.items())):
        dst = os.path.join(tmp, f'f{i}' + os.path.splitext(rel)[1])
        with open(dst, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        roots.append({'type': 'file', 'name': os.path.join(ws, rel.replace('/', os.sep)), 'external-contents': dst})
    ov = os.path.join(tmp, 'overlay.json')
    json.dump({'version': 0, 'case-sensitive': 'false', 'overlay-relative': False, 'roots': roots}, open(ov, 'w', encoding='utf-8'))
    return ov


def _one(e, overlay):
    p = subprocess.run(args_for(e, overlay), cwd=e['directory'], capture_output=True, text=True, encoding='utf-8', errors='replace',
                       creationflags=IDLE)
    errs = []
    for line in (p.stdout + p.stderr).splitlines():
        m = DIAG.match(line.strip())
        if m:
            errs.append((os.path.normcase(m['path']), int(m['line']), int(m['col']), m['msg']))
    return e['file'], p.returncode, errs


def check(cdb, tus, overlay=None, jobs=4):
    """{tu: (returncode, [errors])}"""
    out = {}
    with cf.ThreadPoolExecutor(jobs) as ex:
        for f, rc, errs in ex.map(lambda t: _one(cdb[t], overlay), sorted(t for t in tus if t in cdb)):
            out[os.path.normcase(f)] = (rc, errs)
    return out


def add_param(text, line, col, param):
    """line:col(1 始まり)にある関数名の直後の ( ... ) の最後に param を足す。() と (void) は param だけにする"""
    lines = text.split('\n')
    off = sum(len(x) + 1 for x in lines[:line - 1]) + col - 1
    i = text.index('(', off)
    depth, j = 0, i
    while True:
        c = text[j]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                break
        j += 1
    inner = text[i + 1:j]
    if inner.strip() in ('', 'void'):
        return text[:i + 1] + param + text[j:]
    return text[:j] + ', ' + param + text[j:]
