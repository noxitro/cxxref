"""索引の自動更新。MCP サーバーが、索引の後にソースが変わったのを見つけたら、裏で作り直して少しだけ待つ。

  python -I autoupdate.py --dir <.index のフォルダ> [--full]   (サーバーが最低優先度の別プロセスで起動する)

作り方は scripts/index-project.ps1 が残す <索引>.recipe.json を使う。ふだんは ltidx -update だけを流し、
新しいソース・消えた TU があるとき(--full)は、compile_commands.json も make-cdb.ps1 で作り直す。
同時に 1 つだけ走らせる。ロックは update.lock を開いたままにして示す(Windows では開かれたファイルを
消せないので、消せたら持ち主はもういない)。結果は update.json、ログは update.log に書く。
"""
import argparse
import json
import os
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


def start(d, full):
    """作り直しを別プロセスで始める。標準出力はプロトコル専用なので、子には渡さない。"""
    flags = 0
    if os.name == 'nt':  # 最低優先度は ltidx・make-cdb にも引き継がれる
        flags = subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), '--dir', d] + (['--full'] if full else []),
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


def refresh(idx, paths, root, wait):
    """(引く索引, 出力の先頭に足す注記)。索引が古ければ作り直しを始め、wait 秒まで待つ。
    idx は ltq.Index。終わったら作り直した索引を開き直して返す。"""
    import ltq  # server.py が query を sys.path に足してから呼ぶ
    changed, new, missing = idx.freshness()
    if not (changed or new or missing):
        return idx, ''
    if any(recipe_of(p) is None for p in paths):
        return idx, ('auto-update: off for this index (no .recipe.json). Rebuild it once with '
                     f'{os.path.join(SCRIPTS, "index-project.bat")} to turn it on.\n')
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
        start(d, full=bool(new or missing))
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


def work(d, full):
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
    ap.add_argument('--dir', required=True, help='the .index folder')
    ap.add_argument('--full', action='store_true', help='also regenerate compile_commands.json')
    a = ap.parse_args()
    return work(os.path.abspath(a.dir), a.full)


if __name__ == '__main__':
    sys.exit(main())
