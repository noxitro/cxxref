"""索引の自動更新。MCP サーバーが、索引の後にソースが変わったのを見つけたら、裏で作り直して少しだけ待つ。

  python -I autoupdate.py --dir <.index のフォルダ> [--full]   (サーバーが最低優先度の別プロセスで起動する)

作り方は scripts/index-project.ps1 が残す <索引>.recipe.json を使う。ふだんは ltidx -update だけを流し、
新しいソース・消えた TU があるとき(--full)は、compile_commands.json も make-cdb.ps1 で作り直す。
同時に 1 つだけ走らせる。ロックは update.lock を開いたままにして示す(Windows では開かれたファイルを
消せないので、消せたら持ち主はもういない)。結果は update.json、ログは update.log に書く。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, '..', 'scripts'))
MAKE_CDB = os.path.join(SCRIPTS, 'make-cdb.ps1')
ALTPASS = os.path.join(SCRIPTS, 'altpass.py')
RECIPE = '.recipe.json'


def find_ltidx():
    """ltidx.exe の場所。環境変数 LTIDX(あればそのまま)、このリポジトリでビルドしたもの(indexer\\build)、
    配布物を取ってきたもの(scripts\\get-ltidx.ps1 が %LOCALAPPDATA%\\cxxref\\ltidx に置く)の順。無ければ None。
    プラグインとして入れた写しにはビルドしたものが無いので、配布物を使う。"""
    if os.environ.get('LTIDX'):
        return os.environ['LTIDX']
    cands = [os.path.join(HERE, '..', 'indexer', 'build', 'ltidx.exe')]
    if os.environ.get('LOCALAPPDATA'):  # 無ければ探さない(作業フォルダからの相対パスにしない)
        cands.append(os.path.join(os.environ['LOCALAPPDATA'], 'cxxref', 'ltidx', 'ltidx.exe'))
    for p in cands:
        if os.path.exists(p):
            return os.path.normpath(p)
    return None


def plugin_version():
    """このプラグイン(リポジトリ)の版(plugin.json の version)。読めなければ空"""
    try:
        with open(os.path.join(HERE, '..', 'plugin.json'), encoding='utf-8-sig') as f:
            return str(json.load(f).get('version') or '')
    except (OSError, ValueError):
        return ''


def stale_ltidx():
    """使う ltidx が取ってきたもの(%LOCALAPPDATA%/cxxref/ltidx)で、その版(隣の version.txt)がプラグインと違えば
    (その版, プラグインの版)。ビルドしたもの・環境変数 LTIDX のもの・見つからないときは None。
    プラグインを更新しても取ってきた ltidx は入れ替わらないので、/cxxref-index で取り直すよう知らせるのに使う"""
    p = find_ltidx()
    if not p or os.environ.get('LTIDX') or not os.environ.get('LOCALAPPDATA'):
        return None
    got = os.path.normcase(os.path.normpath(os.path.join(os.environ['LOCALAPPDATA'], 'cxxref', 'ltidx', 'ltidx.exe')))
    if os.path.normcase(os.path.normpath(os.path.abspath(p))) != got:
        return None
    want = plugin_version()
    try:
        with open(os.path.join(os.path.dirname(p), 'version.txt'), encoding='utf-8-sig') as f:
            have = f.read().strip()
    except OSError:
        have = ''
    return (have or 'unknown', want) if want and have != want else None


def no_ltidx():
    if os.environ.get('LTIDX'):  # 取ってきても直らない
        return f'ltidx.exe not found at LTIDX={os.environ["LTIDX"]}; fix or unset the LTIDX environment variable'
    return f'ltidx.exe not found; run {os.path.join(SCRIPTS, "get-ltidx.bat")} once (or build it with indexer\\build.bat)'


def recipe_of(index):
    try:
        with open(index + RECIPE, encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# この PC で index-project(/cxxref-index も同じもの)が書いた作り直しの記録の指紋。リポジトリの外に置く。
# skill から自動で許可された CLI の呼び出しは、ここに載った記録の索引だけを作り直す。リポジトリに置かれた記録
# (compile_commands.json の場所と make-cdb の引数)を信じると、細工したリポジトリを開いただけで、作り直しが
# 細工した compile_commands.json で ltidx を動かすため(コンパイラのプラグインを読ませる引数は、別に unsafe_cdb で止める)。
# 記録 1 つごとに 1 ファイル(trusted\<鍵のハッシュ>.json)。1 つのファイルを読んで足して書き戻すと、索引づくりを
# 同時に走らせたときに片方の登録が消えるため。0.5.10 の trusted-recipes.json(全部を 1 つに書いたもの)も読む
TRUST_DIR = 'trusted'
TRUST_FILE = 'trusted-recipes.json'


def _trust_base():
    base = os.environ.get('LOCALAPPDATA')
    return os.path.join(base, 'cxxref') if base else None


def _recipe_key(index):
    return os.path.normcase(os.path.realpath(index + RECIPE))


def _trust_path(index):
    base = _trust_base()
    if not base:
        return None
    name = hashlib.sha256(_recipe_key(index).encode('utf-8')).hexdigest()[:32] + '.json'
    return os.path.join(base, TRUST_DIR, name)


def _recipe_digest(index):
    try:
        with open(index + RECIPE, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _read_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def trust_available():
    """登録を置く場所(%LOCALAPPDATA%\\cxxref)があるか。"""
    return _trust_base() is not None


def trust_recipe(index):
    """index の作り直しの記録を、この PC で作ったものとして登録する(index-project.ps1 が記録を書いた直後に呼ぶ)。"""
    p, digest = _trust_path(index), _recipe_digest(index)
    if not p or not digest:
        return False
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f'{p}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'recipe': _recipe_key(index), 'digest': digest}, f, ensure_ascii=False)
    os.replace(tmp, p)
    return True


def trusted_recipe(index):
    """index の作り直しの記録が、登録したときから変わっていなければ True。"""
    digest, p = _recipe_digest(index), _trust_path(index)
    if not digest or not p:
        return False
    d = _read_json(p)
    if d.get('recipe') == _recipe_key(index):
        return d.get('digest') == digest
    return _read_json(os.path.join(_trust_base(), TRUST_FILE)).get(_recipe_key(index)) == digest


def rebuild_hint(index, root):
    """登録の無い索引を、この PC で作り直して登録するコマンド(作り直しの記録の構成のまま、変わった分だけ)。"""
    name = os.path.basename(index)[:-len('.tsv')] if index.endswith('.tsv') else 'index'
    cmd = f'"{os.path.join(SCRIPTS, "index-project.bat")}" -Root "{root}"' + (f' -Name {name}' if name != 'index' else '')
    rec = recipe_of(index) or {}
    mk = rec.get('makeCdb')
    if isinstance(mk, dict):
        for k in ('Project', 'Configuration', 'Platform', 'CMakeArgs'):
            if mk.get(k) not in (None, ''):
                cmd += f' -{k} "{mk[k]}"'
    elif rec.get('cdb') and os.path.normcase(os.path.abspath(rec['cdb'])) != os.path.normcase(os.path.abspath(root)):
        cmd += f' -Cdb "{rec["cdb"]}"'
    return cmd + ' -Update'


# compile_commands.json の中で、ltidx(clang)にファイルからコードを読ませうる引数。作り直しの前に見て、あれば作り直さない
# (コンパイラのプラグイン、clang の設定ファイル)。応答ファイル(@file)は中身も見る(CMake の Ninja が include の一覧に使う)
UNSAFE_ARG = re.compile(r'(?:/clang:|-Xclang=)?(?:-fplugin\b|-fpass-plugin\b|-load$|-plugin$|-add-plugin$|-plugin-arg-|--config\b)')


def _unsafe_args(args, cwd, depth=0):
    for t in args:
        t = t.strip('"\'')
        if UNSAFE_ARG.match(t):
            return t
        if t.startswith('@'):
            if depth or t[1:].replace('\\', '/').startswith('//'):  # UNC は開くだけで資格情報を送りうる
                return t  # 応答ファイルの中の応答ファイルまでは追わない
            try:
                with open(os.path.join(cwd, t[1:]), encoding='utf-8', errors='replace') as f:
                    bad = _unsafe_args(f.read().split(), cwd, depth + 1)
            except OSError:
                continue  # 読めない応答ファイルは clang も読めない
            if bad:
                return f'{bad} (in {t})'
    return None


def unsafe_cdb(cdb):
    """cdb(compile_commands.json のフォルダ)に、コードを読ませうる引数があれば、その引数。無い・読めなければ None。"""
    try:
        with open(os.path.join(cdb, 'compile_commands.json'), encoding='utf-8-sig') as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return None  # ltidx が誤りにする
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        args = e.get('arguments') if isinstance(e.get('arguments'), list) else str(e.get('command') or '').split()
        bad = _unsafe_args([str(a) for a in args], str(e.get('directory') or cdb))
        if bad:
            return bad
    return None


def _path(d, name):
    return os.path.join(d, name)


def running(d):
    """ほかのプロセスが作り直している最中か。持ち主のいないロックは、ついでに消す。"""
    lock = _path(d, 'update.lock')
    if not os.path.exists(lock):
        return False
    try:
        os.remove(lock)
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


def read_state(d):
    try:
        with open(_path(d, 'update.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def start(d, full, trusted_only=False):
    """作り直しを別プロセスで始める。標準出力はプロトコル専用なので、子には渡さない。"""
    flags = 0
    if os.name == 'nt':  # 最低優先度は ltidx・make-cdb にも引き継がれる
        flags = subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), '--dir', d] + (['--full'] if full else [])
                     + (['--trusted-only'] if trusted_only else []),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def wait_done(d, since, timeout):
    """since より後に終わった作り直しの結果。timeout 秒で終わらなければ None。"""
    deadline = time.time() + timeout
    while True:
        st = read_state(d)
        if st and st.get('finished', 0) >= since:
            return st
        if time.time() >= deadline:
            return None
        time.sleep(0.1)


def refresh(idx, paths, root, wait, trusted_only=False):
    """(引く索引, 出力の先頭に足す注記)。索引が古ければ作り直しを始め、wait 秒まで待つ。
    idx は ltq.Index。終わったら作り直した索引を開き直して返す。
    trusted_only なら、この PC で作った記録(trusted_recipe)の索引だけを作り直す(skill から自動で許可された呼び出し)。"""
    import ltq  # server.py が query を sys.path に足してから呼ぶ
    changed, new, missing = idx.freshness()
    if not (changed or new or missing):
        return idx, ''
    if any(recipe_of(p) is None for p in paths):
        return idx, ('auto-update: off for this index (no .recipe.json). Rebuild it once with '
                     f'{os.path.join(SCRIPTS, "index-project.bat")} to turn it on.\n')
    untrusted = [p for p in paths if not trusted_recipe(p)] if trusted_only else []
    if untrusted and not trust_available():
        return idx, ('auto-update: off for this index (LOCALAPPDATA is not set, so indexes built on this PC cannot be '
                     'recorded). Results use the current index (lines re-located).\n')
    if untrusted:
        # 作り直しの記録の構成のまま(-Name・作り元・-Update)。-Root だけだと、既定の構成を最初から作るだけになる
        return idx, ('auto-update: off for this index (it was not built on this PC with index-project.bat or '
                     '/cxxref-index). Results use the current index (lines re-located). To turn it on, ask the user to '
                     'rebuild it once (only changed files are parsed): '
                     + '; '.join(rebuild_hint(p, root) for p in untrusted) + '\n')
    d = os.path.dirname(os.path.abspath(paths[0]))
    n = len(changed) + len(new) + len(missing)
    asked = time.time()
    if not running(d):
        st = read_state(d)
        src = idx.sources()
        newest = max((src[p][1] for p in changed + new if p in src), default=asked)
        if st and not st.get('ok') and st.get('finished', 0) >= newest:
            # 前回失敗してから何も変わっていない。同じ失敗を毎回繰り返さない
            return idx, (f'auto-update: the last rebuild failed ({st.get("message")}); see {_path(d, "update.log")}. '
                         'Results use the current index. It is retried after the next source change.\n')
        start(d, full=bool(new or missing), trusted_only=trusted_only)
    st = wait_done(d, asked, wait)
    if st is None:
        return idx, (f'auto-update: rebuilding the index in the background for {n} changed files. This result uses '
                     'the current index (lines re-located); a later call uses the new index.\n')
    if not st.get('ok'):
        return idx, (f'auto-update: rebuild failed ({st.get("message")}); see {_path(d, "update.log")}. '
                     'Results use the current index.\n')
    return ltq.Index(paths, root), (f'auto-update: rebuilt the index for {n} changed files '
                                     f'({st["finished"] - st["started"]:.1f} s).\n')


def run_recipe(rec, full, log):
    """1 つの構成を作り直す。戻り値は失敗の説明(成功なら None)。"""
    mk = rec.get('makeCdb')
    if full and mk:
        pwsh = shutil.which('pwsh')
        if not pwsh:
            return 'pwsh not found'
        args = [pwsh, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', MAKE_CDB]
        for k, v in mk.items():
            if v not in (None, ''):
                args += [f'-{k}', str(v)]
        rc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
        if rc != 0:
            return f'make-cdb exit {rc}'
    bad = unsafe_cdb(rec['cdb'])
    if bad:
        return (f'compile_commands.json has an argument that loads code into the compiler ({bad}); not rebuilt '
                'automatically. Rebuild it with index-project.bat if you trust it')
    out = rec['out']
    ltidx = find_ltidx()
    if not ltidx or not os.path.exists(ltidx):
        return no_ltidx()
    args = [ltidx, '-p', rec['cdb'], '-root', rec['root'], '-o', out, '-j', str(rec.get('jobs') or 1)]
    if os.path.exists(out + '.deps.tsv'):
        args.append('-update')
    rc = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
    if rc not in (0, 1):  # 1 は解析にエラーのあった TU がある(索引はできている)
        return f'ltidx exit {rc}'
    if rec.get('alt', True):
        # 差分パス(無効な #if の中の解析)。失敗しても索引はできているので、記録に残すだけ
        log.flush()
        try:
            rc = subprocess.run([sys.executable, '-I', ALTPASS, '--index', out, '--ltidx', ltidx],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log).returncode
            if rc != 0:
                log.write(f'alt pass: exit {rc}\n')
        except OSError as e:
            log.write(f'alt pass: {e}\n')
    return None


def work(d, full, trusted_only=False):
    lock = _path(d, 'update.lock')
    try:
        f = open(lock, 'x')
    except FileExistsError:
        if running(d):
            return 0
        try:
            f = open(lock, 'x')
        except FileExistsError:
            return 0
    started = time.time()
    errors = []
    with f:
        f.write(str(os.getpid()))
        f.flush()
        indexes = sorted(_path(d, x[:-len(RECIPE)]) for x in os.listdir(d) if x.endswith('.tsv' + RECIPE))
        with open(_path(d, 'update.log'), 'w', encoding='utf-8') as log:
            for index in indexes:
                rec = recipe_of(index)
                if rec is None:
                    errors.append(f'{os.path.basename(index)}: unreadable recipe')
                    continue
                if trusted_only and not trusted_recipe(index):
                    errors.append(f'{os.path.basename(index)}: recipe not built on this PC (skipped)')
                    continue
                log.write(f'== {index}{" (full)" if full else ""}\n')
                log.flush()
                err = run_recipe(rec, full, log)
                if err:
                    errors.append(f'{os.path.basename(index)}: {err}')
                elif os.path.exists(index) and os.path.getmtime(index) < started:
                    # 解析し直す TU が無くても、ここまでは確かめたので索引の時刻を進める(同じ変更で毎回作り直さない)
                    os.utime(index, (started, started))
        state = {'started': started, 'finished': time.time(), 'ok': not errors, 'message': '; '.join(errors)}
        tmp = _path(d, 'update.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as s:
            json.dump(state, s)
        os.replace(tmp, _path(d, 'update.json'))
    try:
        os.remove(lock)
    except OSError:
        pass
    return 0 if not errors else 1


def main():
    ap = argparse.ArgumentParser(prog='autoupdate')
    ap.add_argument('--dir', help='the .index folder')
    ap.add_argument('--full', action='store_true', help='also regenerate compile_commands.json')
    ap.add_argument('--trusted-only', action='store_true', help='rebuild only indexes whose recipe was built on this PC')
    ap.add_argument('--trust', metavar='INDEX', help='record INDEX.recipe.json as built on this PC, and exit')
    a = ap.parse_args()
    if a.trust:
        return 0 if trust_recipe(os.path.abspath(a.trust)) else 1
    if not a.dir:
        ap.error('--dir is required')
    return work(os.path.abspath(a.dir), a.full, a.trusted_only)


if __name__ == '__main__':
    sys.exit(main())
