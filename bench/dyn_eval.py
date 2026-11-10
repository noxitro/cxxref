"""動的編集の実測: ソースを書き換えた直後に MCP サーバー(server.call_tool、自動更新を 10 秒まで待つ)で refs を引き、
自動更新(ltidx -update)が終わった後にもう一度引く。正解は、書き換えた後のソースを ltidx で最初から作った索引。
場面ごとに、ソースと索引(.index)を元に戻す(ソースは中身と更新日時を戻す)。

usage: python -I dyn_eval.py <リポジトリ> <ルート> <ltidx.exe> <結果の JSON> [場面の id ...]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

REPO, ROOT, LTIDX, OUT = sys.argv[1:5]
ONLY = set(sys.argv[5:])
sys.path.insert(0, os.path.join(REPO, 'mcp'))
sys.path.insert(0, os.path.join(REPO, 'query'))
os.environ['LTIDX'] = LTIDX
import autoupdate  # noqa: E402
import ltq  # noqa: E402
import server  # noqa: E402

IDX_DIR = os.path.join(ROOT, '.index')
BASE_DIR = os.path.join(ROOT, '.index-base')
CDB = os.path.join(ROOT, '.cdb')
TRUTH = os.path.join(ROOT, '.index-truth')


def rd(rel):
    with open(os.path.join(ROOT, rel), 'rb') as f:
        return f.read()


def wr(rel, data):
    with open(os.path.join(ROOT, rel), 'wb') as f:
        f.write(data)


def sub(rel, old, new, count=1):
    d = rd(rel)
    assert d.count(old) >= 1, (rel, old)
    wr(rel, d.replace(old, new, count))


def insert_after_line(rel, pattern, text):
    lines = rd(rel).split(b'\n')
    i = next(i for i, l in enumerate(lines) if re.search(pattern, l))
    lines.insert(i + 1, text)
    wr(rel, b'\n'.join(lines))


# 場面: (id, 説明, 書き換える関数, 確かめる問い)
def s_shift():
    d = rd('runtime/kernel/job_system.cpp')
    wr('runtime/kernel/job_system.cpp', b'// dyn 1\r\n// dyn 2\r\n// dyn 3\r\n// dyn 4\r\n// dyn 5\r\n' + d)


def s_add_call():
    # 既にある呼び出しの行を、すぐ下にもう 1 行足す
    lines = rd('runtime/core/test/job_system_dispatch_test.cpp').split(b'\n')
    i = next(i for i, l in enumerate(lines) if b'job_system.Wait(counter);' in l)
    lines.insert(i + 1, lines[i])
    wr('runtime/core/test/job_system_dispatch_test.cpp', b'\n'.join(lines))


def s_del_call():
    lines = rd('runtime/bench/bench_job.cpp').split(b'\n')
    i = next(i for i, l in enumerate(lines) if b'job_system.Wait(counter);' in l)
    del lines[i]
    wr('runtime/bench/bench_job.cpp', b'\n'.join(lines))


def s_rename():
    pat = re.compile(rb'(?<![A-Za-z0-9_])CreateSocket(?![A-Za-z0-9_])')
    for rel in ('runtime/core/dev/net/server.cpp', 'runtime/kernel/detail/network_win64.cpp', 'runtime/kernel/network.h',
                'runtime/kernel/test/network_test.cpp', 'runtime/reflection_generated/gen/x64/Debug/global_decl_25.g.cpp'):
        wr(rel, pat.sub(b'OpenSocket', rd(rel)))


def s_header():
    # 多くの TU が読むヘッダーの中の inline 関数に呼び出しを足す: job_system.h の末尾に、Wait を呼ぶ inline 関数を足す
    d = rd('runtime/kernel/job_system.h')
    i = d.rfind(b'}')  # 名前空間の閉じかっこの前
    wr('runtime/kernel/job_system.h', d[:i] + b'inline void DynProbeWait(JobSystem& js, JobCounter& c) { js.Wait(c); }\r\n' + d[i:])


def s_macro_body():
    # マクロの本体を書き換える: NOX_ASSERT が引数を捨てるようにする(多くの TU が読む kernel/assertion.h。条件の中の呼び出しが消える)
    # (最初の版は、どの TU も読まない runtime/bin/source/nox_lib/assertion.h の写しを書き換えていた)
    sub('runtime/kernel/assertion.h', b'#define\tNOX_ASSERT(expression, ...) \\',
        b'#define\tNOX_ASSERT(expression, ...) ((void)0)\r\n#define\tNOX_ASSERT_OLD(expression, ...) \\')


def s_unread():
    # どの TU も読まないファイル(リポジトリに置かれた写し)を書き換える。自動更新がどう振る舞うか
    sub('runtime/bin/source/nox_lib/assertion.h', b'#define NOX_ASSERT(condition, message) \\',
        b'#define NOX_ASSERT(condition, message) ((void)0)\r\n#define NOX_ASSERT_OLD(condition, message) \\')


def s_macro_new():
    # 新しいマクロを足して使う(マクロの定義と展開、展開の中の呼び出し)
    sub('runtime/kernel/job_system.h', b'#pragma once', b'#pragma once\r\n#define NOX_DYN_WAIT(js, c) (js).Wait(c)')
    lines = rd('runtime/core/test/job_system_dispatch_test.cpp').split(b'\n')
    i = next(i for i, l in enumerate(lines) if b'job_system.Wait(counter);' in l)
    lines.insert(i + 1, lines[i].replace(b'job_system.Wait(counter);', b'NOX_DYN_WAIT(job_system, counter);'))
    wr('runtime/core/test/job_system_dispatch_test.cpp', b'\n'.join(lines))


SCENES = [
    ('shift', '定義のあるファイルの先頭に 5 行足す', s_shift, ['nox::JobSystem::Wait']),
    ('add-call', '呼び出しを 1 行足す', s_add_call, ['nox::JobSystem::Wait']),
    ('del-call', '呼び出しを 1 行消す', s_del_call, ['nox::JobSystem::Wait']),
    ('rename', '関数の名前を変える(宣言・定義・呼び出し、5 ファイル)', s_rename,
     ['nox::os::network::OpenSocket', 'nox::os::network::CreateSocket']),
    ('header', '多くの TU が読むヘッダーに、呼び出しを含む inline 関数を足す', s_header, ['nox::JobSystem::Wait']),
    ('macro-body', 'マクロの本体を書き換える(NOX_ASSERT が引数を捨てる)', s_macro_body,
     ['NOX_ASSERT', 'nox::detail::AssertFailed']),
    ('unread-file', 'どの TU も読まないファイルを書き換える(nox_lib の assertion.h の写し)', s_unread, ['NOX_ASSERT']),
    ('macro-new', 'マクロを足して使う', s_macro_new, ['NOX_DYN_WAIT', 'nox::JobSystem::Wait']),
]
TOUCHED = ['runtime/kernel/job_system.cpp', 'runtime/core/test/job_system_dispatch_test.cpp', 'runtime/bench/bench_job.cpp',
           'runtime/core/dev/net/server.cpp', 'runtime/kernel/detail/network_win64.cpp', 'runtime/kernel/network.h',
           'runtime/kernel/test/network_test.cpp', 'runtime/reflection_generated/gen/x64/Debug/global_decl_25.g.cpp',
           'runtime/kernel/job_system.h', 'runtime/bin/source/nox_lib/assertion.h', 'runtime/kernel/assertion.h']


def snapshot_sources():
    return {r: (rd(r), os.stat(os.path.join(ROOT, r)).st_mtime) for r in TOUCHED}


def restore(snap):
    for r, (data, mt) in snap.items():
        if rd(r) != data:
            wr(r, data)
        os.utime(os.path.join(ROOT, r), (mt, mt))
    shutil.rmtree(IDX_DIR, ignore_errors=True)
    shutil.copytree(BASE_DIR, IDX_DIR)


def facts(text):
    """refs の出力から (節, path:line) の集合。節は calls・expand・defs・other refs。"""
    out, sec = set(), None
    names = {'calls': 'call', 'macro expansions': 'expand', 'definitions and declarations': 'def',
             'other references': 'ref'}
    for line in text.splitlines():
        m = re.match(r'^== (.*?)(?: \(.*)?: \d+$', line)
        if m:
            sec = next((v for k, v in names.items() if line.startswith('== ' + k)), None)
            continue
        if sec:
            m = re.match(r'^  \S+\s+(\S+?:\d+)\s', line)
            if m:
                out.add((sec, m.group(1)))
    return out


def score(got, truth):
    tp = len(got & truth)
    return {'truth': len(truth), 'got': len(got), 'missing': sorted(truth - got)[:6], 'extra': sorted(got - truth)[:6],
            'ok': got == truth}


def rows_of(path):
    with open(path, 'rb') as f:
        return set(f.read().split(b'\n')) - {b''}


def main():
    if not os.path.isdir(BASE_DIR):
        shutil.copytree(IDX_DIR, BASE_DIR)
    snap = snapshot_sources()
    results = []
    for sid, desc, fn, probes in SCENES:
        if ONLY and sid not in ONLY:
            continue
        restore(snap)
        time.sleep(1.5)  # 索引の更新日時より後に書き換えたことにする(秒の分解能の違いに備える)
        fn()
        cfg = server.Config(ROOT, None, 10.0)
        t0 = time.time()
        first = {}
        for q in probes:
            ts = time.time()
            code, text = server.call_tool(cfg, 'refs', {'name': q, 'root': ROOT})
            first[q] = (text, time.time() - ts)
        # 自動更新が終わるまで待つ(最大 10 分)。始まらなかった(索引が古くない)なら待たない
        started = any(v[0].startswith('auto-update') or '\nauto-update' in v[0] for v in first.values())
        st = autoupdate.wait_done(IDX_DIR, t0 - 5, 600) if started else None
        t_done = time.time() - t0
        after = {}
        for q in probes:
            code, text = server.call_tool(server.Config(ROOT, None, 0.0), 'refs', {'name': q, 'root': ROOT})
            after[q] = text
        # 正解: 書き換えた後のソースを最初から
        shutil.rmtree(TRUTH, ignore_errors=True)
        os.makedirs(TRUTH)
        ts = time.time()
        subprocess.run([LTIDX, '-p', CDB, '-root', ROOT, '-o', os.path.join(TRUTH, 'index.tsv')],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t_full = time.time() - ts
        truth = {}
        for q in probes:
            out = server.call_tool(server.Config(ROOT, os.path.join(TRUTH, 'index.tsv'), None), 'refs', {'name': q, 'root': ROOT})[1]
            truth[q] = out
        upd = os.path.join(IDX_DIR, 'index.tsv')
        same_rows = rows_of(upd) == rows_of(os.path.join(TRUTH, 'index.tsv'))
        same_skip = rows_of(upd + '.skip.tsv') == rows_of(os.path.join(TRUTH, 'index.tsv.skip.tsv'))
        diff_rows = len(rows_of(upd) ^ rows_of(os.path.join(TRUTH, 'index.tsv')))
        r = {'id': sid, 'desc': desc, 'update_ok': st and st.get('ok'), 'update_msg': st and st.get('message'),
             'update_run_sec': st and round(st['finished'] - st['started'], 1), 'update_sec': round(t_done, 1),
             'full_sec': round(t_full, 1), 'index_rows_equal_full': same_rows, 'skip_equal_full': same_skip,
             'rows_differing': diff_rows, 'probes': {}}
        for q in probes:
            ft, fsec = first[q]
            r['probes'][q] = {
                'first_sec': round(fsec, 1),
                'first_note': next((l for l in ft.splitlines() if l.startswith('auto-update')), ''),
                'first_vs_truth': score(facts(ft), facts(truth[q])),
                'after_vs_truth': score(facts(after[q]), facts(truth[q])),
            }
        results.append(r)
        print(json.dumps(r, ensure_ascii=False), flush=True)
    restore(snap)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
