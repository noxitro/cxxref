"""ltidx の結合テスト(文字コード・仮想関数の上書き・-update)。一時フォルダに小さなプロジェクトを作って索引にする。

usage: python -I -m unittest discover -s indexer/test   (ltidx をビルドしてから。場所は環境変数 LTIDX で変えられる)
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LTIDX = os.environ.get('LTIDX') or os.path.join(HERE, '..', 'build', 'ltidx.exe')
sys.path.insert(0, os.path.join(HERE, '..', '..', 'query'))
import ltq  # noqa: E402

def llvm_resource_dir():
    """ltidx をビルドした LLVM の組み込みヘッダーの場所(lib\\clang\\<版>)。分からなければ None。"""
    root = os.environ.get('LLVM_ROOT')
    rf = os.path.join(HERE, '..', 'llvm-root.txt')
    if not root and os.path.exists(rf):
        with open(rf, 'rb') as f:
            root = f.read().decode('mbcs' if os.name == 'nt' else 'utf-8').splitlines()[0].strip()
    d = os.path.join(root, 'lib', 'clang') if root else ''
    vers = sorted(os.listdir(d)) if os.path.isdir(d) else []
    return os.path.join(d, vers[-1]) if vers else None


SJIS_SRC = '''struct Base { virtual void Draw(); virtual ~Base(); };
struct Derived : Base { void Draw() override; };
void Run();
void Call(Base* b, Derived* d) {
  b->Draw();
  d->Draw();
  // 表
  Run();
  const char* s = "表"; Run();
}
'''


@unittest.skipUnless(os.path.exists(LTIDX), 'ltidx.exe is not built')
class LtidxTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cmds = {}

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rel, data, args=None):
        """args を渡すと、そのファイルを TU として compile_commands.json に載せる。"""
        fp = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, 'wb') as f:
            f.write(data.encode('utf-8') if isinstance(data, str) else data)
        if args is not None:
            self.cmds[rel] = args
        with open(os.path.join(self.root, 'compile_commands.json'), 'w', encoding='utf-8') as f:
            d = self.root.replace('\\', '/')
            json.dump([{'directory': d, 'file': f'{d}/{r}', 'arguments': ['cl.exe', '/c', '/std:c++17'] + a + [r]}
                       for r, a in self.cmds.items()], f)

    def run_ltidx(self, *args, out='.index/index.tsv'):
        os.makedirs(os.path.join(self.root, '.index'), exist_ok=True)
        r = subprocess.run([LTIDX, '-p', self.root, '-root', self.root, '-o', os.path.join(self.root, out)] + list(args),
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        return r.returncode, r.stderr

    def rows(self, out='.index/index.tsv'):
        with open(os.path.join(self.root, out), encoding='utf-8') as f:
            return [line.rstrip('\n').split('\t') for line in f]

    def tus(self, out='.index/index.tsv'):
        with open(os.path.join(self.root, out + '.tu.tsv'), encoding='utf-8') as f:
            next(f)
            return {os.path.basename(c[0]): c for c in (line.rstrip('\n').split('\t') for line in f)}

    def has(self, sym, role, loc, out='.index/index.tsv', caller=None):
        return any(r[0] == sym and r[1] == role and r[3] == loc and (caller is None or r[2] == caller)
                   for r in self.rows(out))

    def test_shift_jis(self):
        # 「表」(0x95 0x5C)の 2 バイト目は `\`。UTF-8 として読むと、コメントが次の行を飲み、文字列が閉じない
        self.write('a.cpp', SJIS_SRC.encode('cp932'), [])
        rc, err = self.run_ltidx('-source-cp', '932')
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('Run', 'call', 'a.cpp:8:3'))
        self.assertTrue(self.has('Run', 'call', 'a.cpp:9:26'))  # 列は UTF-8 に直した行のバイト位置
        self.assertIn('const char* s = "表"; Run();', [r[4] for r in self.rows()])  # ソース行は UTF-8
        self.assertEqual(self.tus()['a.cpp'][7], '932')
        # ltq も同じ変換をするので、列で突き合わせられる
        out = self.ltq('check', 'a.cpp:9:26', 'a.cpp:7:6')
        self.assertNotIn('configurations', out)
        self.assertIn('indexed: call Run at col 26', out)
        self.assertIn('inside comment 7:3-7:9', out)

    def test_source_charset_flag_wins(self):
        # /source-charset は clang-cl がエラーにするので外し、そのコードページで読む
        self.write('a.cpp', SJIS_SRC.encode('cp932'), ['/source-charset:shift_jis', '/execution-charset:shift_jis'])
        rc, err = self.run_ltidx('-source-cp', '1252')
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('Run', 'call', 'a.cpp:8:3'))
        self.assertEqual(self.tus()['a.cpp'][7], '932')

    def test_utf16(self):
        self.write('a.cpp', SJIS_SRC.encode('utf-16'), [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('Run', 'call', 'a.cpp:9:26'))

    def test_deep_template_instantiation(self):
        # 深いテンプレートの実体化は、作業用のスレッドの既定(1 MB)のスタックを使い切って ltidx ごと落ちていた
        # (STATUS_STACK_OVERFLOW。O3DE で起きた)。並列で動かしても、この TU が通ること
        self.write('deep.cpp', 'template <int N> struct Deep { static int Get() { return Deep<N - 1>::Get() + 1; } };\n'
                               'template <> struct Deep<0> { static int Get() { return 0; } };\n'
                               'void Run();\n'
                               'int Main() { Run(); return Deep<900>::Get(); }\n', [])
        rc, err = self.run_ltidx('-j', '2')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tus()['deep.cpp'][1], '1')
        self.assertTrue(self.has('Run', 'call', 'deep.cpp:4:14'))

    def test_non_cpp_inputs_are_skipped(self):
        # CMake の compile_commands.json には、アセンブリとリソースも載る。解析すると「失敗した TU」になる
        self.write('a.cpp', 'void Run() {}\n', [])
        self.write('x.asm', 'END\n', [])
        self.write('v.rc', '1 VERSIONINFO\n', [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(self.tus()), ['a.cpp'])

    @unittest.skipUnless(llvm_resource_dir(), 'needs the LLVM that ltidx was built with (LLVM_ROOT or llvm-root.txt)')
    def test_bundled_resource_dir(self):
        # 配布物の形(LLVM 無し): ltidx.exe の隣の lib\clang\<版>\include を組み込みヘッダーに使う
        self.write('a.cpp', '#include <stddef.h>\nvoid Run();\nvoid F() { size_t n = 0; Run(); }\n', [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        built = self.tus()['a.cpp']
        pkg = tempfile.mkdtemp(prefix='cri')
        try:
            src = llvm_resource_dir()
            shutil.copytree(os.path.join(src, 'include'), os.path.join(pkg, 'lib', 'clang', os.path.basename(src), 'include'))
            exe = shutil.copy(LTIDX, pkg)
            out = os.path.join(self.root, '.index', 'index.tsv')
            r = subprocess.run([exe, '-p', self.root, '-root', self.root, '-o', out],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(r.returncode, 0, r.stderr)
            bundled = self.tus()['a.cpp']
            self.assertEqual(bundled[1], '1', bundled)
            self.assertTrue(self.has('Run', 'call', 'a.cpp:3:26'))
            self.assertNotEqual(bundled[6], built[6])  # 作り方の記録(組み込みヘッダーの場所を含む)が隣のものに変わった
            # 組み込みヘッダーが無ければ、解析に進まずに止まる
            r = subprocess.run([exe, '-p', self.root, '-root', self.root, '-o', out, '-resource-dir', os.path.join(pkg, 'none')],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(r.returncode, 2)
            self.assertIn('組み込みヘッダーがありません', r.stderr)
        finally:
            shutil.rmtree(pkg, ignore_errors=True)

    def test_gcc_style_command_lines(self):
        # gcc 形式(g++・clang++)の引数でも TU が通り、実体化されないテンプレートの中の呼び出しも載る。
        # /Zc:twoPhase はファイル名に取られ、MSVC 向けの既定では gcc 形式でもテンプレートの解析を遅らせるので、
        # 形式に合わせて -fno-delayed-template-parsing を足す
        self.write('a.cpp', 'void Run();\ntemplate <class T> void Never(T) { Run(); }\n')
        d = self.root.replace('\\', '/')
        # 形式は clang の driver と同じ決め方(最後の --driver-mode=、無ければ版の付いた名前も含めて実行ファイルの名前)
        for argv in (['g++', '-c', '-std=c++03'], ['clang++', '-c', '-std=c++98'], ['clang-cl.exe', '/c'],
                     ['clang', '--driver-mode=cl', '/c'], [r'C:\tools\cl.exe', '/c'], ['clang-cl-17', '/c'],
                     ['clang', '--driver-mode=g++', '--driver-mode=cl', '/c']):
            with self.subTest(argv[0] + ' ' + argv[1]):
                with open(os.path.join(self.root, 'compile_commands.json'), 'w', encoding='utf-8') as f:
                    json.dump([{'directory': d, 'file': f'{d}/a.cpp', 'arguments': argv + ['a.cpp']}], f)
                rc, err = self.run_ltidx()
                self.assertEqual(rc, 0, err)
                self.assertEqual(self.tus()['a.cpp'][1], '1', self.tus()['a.cpp'])
                self.assertTrue(self.has('Run', 'call', 'a.cpp:2:36'))

    def test_utf8_unchanged(self):
        self.write('a.cpp', SJIS_SRC, ['/utf-8'])
        rc, err = self.run_ltidx('-source-cp', '932')
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('Run', 'call', 'a.cpp:9:26'))
        self.assertEqual(self.tus()['a.cpp'][7], '65001')

    def test_override(self):
        self.write('a.cpp', SJIS_SRC, [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('Derived::Draw', 'override', 'a.cpp:2:30', caller='Base::Draw'))
        self.assertTrue(self.has('Base::Draw', 'call', 'a.cpp:5:6'))  # 基底のポインタ越しは基底のメソッドに解決される

    def test_update(self):
        self.write('h.h', '#pragma once\nvoid Shared();\n')
        self.write('a.cpp', '#include "h.h"\nvoid A() { Shared(); }\n', [])
        self.write('b.cpp', 'void Other();\nvoid B() { Other(); }\n', [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.root, '.index', 'index.tsv.deps.tsv'), encoding='utf-8') as f:
            deps = {(os.path.basename(t), d) for t, d in (line.rstrip('\n').split('\t') for line in list(f)[1:])}
        self.assertEqual(deps, {('a.cpp', 'a.cpp'), ('a.cpp', 'h.h'), ('b.cpp', 'b.cpp')})

        # 何も変えなければ解析しない
        rc, err = self.run_ltidx('-update')
        self.assertIn('update: 0 files changed, 0/2 TU to re-index, 0 TU removed', err)
        self.assertTrue(self.has('Shared', 'call', 'a.cpp:2:12'))

        # ヘッダーを変えると、それを読む TU だけを解析し直す。行がずれた古い行は残らない
        self.touch_later('h.h', '#pragma once\n// moved\nvoid Shared();\n')
        rc, err = self.run_ltidx('-update')
        self.assertIn('update: 1 files changed, 1/2 TU to re-index, 0 TU removed', err)
        self.assertTrue(self.has('Shared', 'decl', 'h.h:3:6'))
        self.assertFalse(self.has('Shared', 'decl', 'h.h:2:6'))
        self.assertTrue(self.has('Other', 'call', 'b.cpp:2:12'))  # 解析し直さない TU の行は残る

        # TU を足す・消す・コマンドを変える
        self.write('c.cpp', '#include "h.h"\nvoid C() { Shared(); }\n', [])
        del self.cmds['b.cpp']
        self.write('a.cpp', '#include "h.h"\nvoid A() { Shared(); }\n', ['/DCHANGED'])
        os.utime(os.path.join(self.root, 'a.cpp'), (time.time() - 3600,) * 2)  # 中身の時刻は古いまま
        rc, err = self.run_ltidx('-update')
        self.assertIn('2/2 TU to re-index, 1 TU removed', err)
        self.assertTrue(self.has('Shared', 'call', 'c.cpp:2:12'))
        self.assertFalse(any(r[3].startswith('b.cpp:') for r in self.rows()))  # 消えた TU だけが読んでいたファイル
        self.assertEqual(set(self.tus()), {'a.cpp', 'c.cpp'})

        # 索引の中身を変える ltidx のオプションを変えたら、全 TU を解析し直す
        rc, err = self.run_ltidx('-update', '-no-dep')
        self.assertIn('2/2 TU to re-index', err)
        rc, err = self.run_ltidx('-update')
        self.assertIn('2/2 TU to re-index', err)

        # 全体の作り直しと同じになる
        self.assertSameAsFull()

    def assertSameAsFull(self):
        """-update で作った索引(index.tsv)が、全体の作り直し(full.tsv)と行の集合で一致する。"""
        rc, err = self.run_ltidx(out='.index/full.tsv')
        self.assertEqual(rc, 0, err)
        for suffix in ('', '.skip.tsv', '.deps.tsv'):
            with open(os.path.join(self.root, '.index', 'index.tsv' + suffix), encoding='utf-8') as f:
                u = set(f)
            with open(os.path.join(self.root, '.index', 'full.tsv' + suffix), encoding='utf-8') as f:
                full = set(f)
            self.assertEqual(u - full, set(), 'stale rows in index.tsv' + suffix)
            self.assertEqual(full - u, set(), 'missing rows in index.tsv' + suffix)
        # 行の出どころも同じになる(次の -update も正しくなる)
        self.assertEqual(self.own('.index/index.tsv'), self.own('.index/full.tsv'))

    def own(self, out):
        """<out>.own.bin の中身。(正確か, {(TU, ファイル, 行のハッシュ)})。"""
        with open(os.path.join(self.root, out + '.own.bin'), 'rb') as f:
            b = f.read()
        pos = 0

        def take(n):
            nonlocal pos
            pos += n
            return b[pos - n:pos]

        def u32():
            return struct.unpack('<I', take(4))[0]

        self.assertEqual(take(8), b'LTOWN001')
        take(8)  # .tu.tsv のハッシュ
        exact = take(1) == b'\x01'
        files = [take(u32()).decode('utf-8') for _ in range(u32())]
        rows = set()
        for _ in range(u32()):
            tu = os.path.basename(take(u32()).decode('utf-8'))
            for _ in range(u32()):
                f, n = u32(), u32()
                rows |= {(tu, files[f], h) for h in struct.unpack(f'<{n}Q', take(8 * n))}
        self.assertEqual(pos, len(b))
        return exact, rows

    def test_update_shared_header(self):
        # 変わっていない共有のヘッダーの行のうち、解析し直す TU だけが出していた行(実体化・マクロ由来)を消す
        h = ('#pragma once\n'
             'template <class T> struct Box { void Put(T t) { t.Draw(); } };\n'
             '#ifdef FEATURE\n'
             'void Feature();\n'
             '#else\n'
             'void NoFeature();\n'
             '#endif\n')
        self.write('h.h', h)
        self.write('a.cpp', '#define FEATURE\n#include "h.h"\nstruct A { void Draw(); };\n'
                   'void UseA(Box<A> b, A a) { b.Put(a); }\n', [])
        self.write('b.cpp', '#include "h.h"\nstruct B { void Draw(); };\n'
                   'void UseB(Box<B> b, B x) { b.Put(x); }\n', [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('A::Draw', 'call', 'h.h:2:51'))
        self.assertTrue(self.has('Feature', 'decl', 'h.h:4:6'))

        # a.cpp だけを変える。h.h の A::Draw の行と Feature の行は、a.cpp だけが出していた
        self.touch_later('a.cpp', '#include "h.h"\nstruct A { void Draw(); };\nvoid UseA(A a) { a.Draw(); }\n')
        rc, err = self.run_ltidx('-update')
        self.assertIn('1/2 TU to re-index', err)
        self.assertFalse(self.has('A::Draw', 'call', 'h.h:2:51'))
        self.assertFalse(self.has('Feature', 'decl', 'h.h:4:6'))
        self.assertTrue(self.has('B::Draw', 'call', 'h.h:2:51'))  # 残す TU(b.cpp)の行は残る
        self.assertSameAsFull()
        self.assertTrue(any(f == 'h.h' for _, f, _ in self.own('.index/index.tsv')[1]))  # 共通でない行がある

        # 1 つの TU だけが読んでいたヘッダーを、新しい TU も読むようになった後に、前からの TU だけを変える
        self.write('g.h', '#pragma once\ntemplate <class T> void Each(T t) { t.Draw(); }\n')
        self.touch_later('a.cpp', '#include "g.h"\nstruct A { void Draw(); };\nvoid UseA(A a) { Each(a); }\n')
        rc, err = self.run_ltidx('-update')
        self.assertEqual(rc, 0, err)
        os.utime(os.path.join(self.root, 'a.cpp'), (time.time() - 3600,) * 2)  # 次の -update では変わっていない
        self.write('c.cpp', '#include "g.h"\nstruct C { void Draw(); };\nvoid UseC(C c) { Each(c); }\n', [])
        rc, err = self.run_ltidx('-update')
        self.assertIn('1/3 TU to re-index', err)
        self.assertSameAsFull()
        self.touch_later('a.cpp', '#include "g.h"\nstruct A { void Draw(); };\nvoid UseA(A a) { a.Draw(); }\n')
        rc, err = self.run_ltidx('-update')
        self.assertIn('1/3 TU to re-index', err)
        self.assertFalse(self.has('A::Draw', 'call', 'g.h:2:39'))
        self.assertTrue(self.has('C::Draw', 'call', 'g.h:2:39'))
        self.assertSameAsFull()

    def test_update_without_provenance(self):
        # 行の出どころ(.own.bin)が無い・別の回のもののときは、前回の行をすべて残し(古い行が残りうる)、そう知らせる
        self.write('h.h', '#pragma once\ntemplate <class T> void Each(T t) { t.Draw(); }\n')
        self.write('a.cpp', '#include "h.h"\nstruct A { void Draw(); };\nvoid UseA(A a) { Each(a); }\n', [])
        self.write('b.cpp', '#include "h.h"\nstruct B { void Draw(); };\nvoid UseB(B b) { Each(b); }\n', [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('A::Draw', 'call', 'h.h:2:39'))
        own = os.path.join(self.root, '.index', 'index.tsv.own.bin')
        os.remove(own)
        self.touch_later('a.cpp', '#include "h.h"\nstruct A { void Draw(); };\nvoid UseA(A a) { a.Draw(); }\n')
        rc, err = self.run_ltidx('-update')
        self.assertEqual(rc, 0, err)
        self.assertIn('index.tsv.own.bin)が無い', err)
        self.assertTrue(self.has('A::Draw', 'call', 'h.h:2:39'))  # 古い行が残る(前の版と同じ)
        # 正確でない出どころは、全体を作り直すまで正確でないままにする
        rc, err = self.run_ltidx('-update')
        self.assertIn('index.tsv.own.bin)が不正確', err)
        # 別の回の索引の .own.bin は使わない
        rc, err = self.run_ltidx(out='.index/full.tsv')
        self.assertEqual(rc, 0, err)
        shutil.copyfile(os.path.join(self.root, '.index', 'full.tsv.own.bin'), own)
        rc, err = self.run_ltidx('-update')
        self.assertIn('.tu.tsv と組でない', err)
        # 作り直せば正確になる
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertFalse(self.has('A::Draw', 'call', 'h.h:2:39'))
        rc, err = self.run_ltidx('-update')
        self.assertNotIn('own.bin', err)

    def test_update_with_broken_provenance(self):
        # 壊れた .own.bin(途中で切れた・後ろに余りがある・形式の印が違う)を読み込まずに、出どころが無いときと同じく前回の行を残す
        self.write('h.h', '#pragma once\ntemplate <class T> void Each(T t) { t.Draw(); }\n')
        self.write('a.cpp', '#include "h.h"\nstruct A { void Draw(); };\nvoid UseA(A a) { Each(a); }\n', [])
        self.write('b.cpp', '#include "h.h"\nstruct B { void Draw(); };\nvoid UseB(B b) { Each(b); }\n', [])
        own = os.path.join(self.root, '.index', 'index.tsv.own.bin')
        for name, damage, why in (('cut', lambda d: d[:-5], '壊れている'),
                                  ('extra', lambda d: d + b'\0', '壊れている'),
                                  ('magic', lambda d: b'X' + d[1:], '形式が違う')):
            with self.subTest(name):
                self.write('a.cpp', '#include "h.h"\nstruct A { void Draw(); };\nvoid UseA(A a) { Each(a); }\n', [])
                rc, err = self.run_ltidx()
                self.assertEqual(rc, 0, err)
                with open(own, 'rb') as f:
                    data = f.read()
                with open(own, 'wb') as f:
                    f.write(damage(data))
                self.touch_later('a.cpp', '#include "h.h"\nstruct A { void Draw(); };\nvoid UseA(A a) { a.Draw(); }\n')
                rc, err = self.run_ltidx('-update')
                self.assertEqual(rc, 0, err)
                self.assertIn(f'index.tsv.own.bin)が{why}', err)
                self.assertTrue(self.has('A::Draw', 'call', 'h.h:2:39'))  # 古い行が残る
                self.assertTrue(self.has('A::Draw', 'call', 'a.cpp:3:20'))  # 変えた TU は作り直されている

    def test_replace_while_open(self):
        # 索引のどれかが開かれている間は差し替えを待つ。開いたままなら、新旧を混ぜずに前の索引を残す
        self.write('a.cpp', 'void A() {}\n', [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        d = os.path.join(self.root, '.index')
        names = ['index.tsv', 'index.tsv.tu.tsv', 'index.tsv.skip.tsv', 'index.tsv.deps.tsv', 'index.tsv.own.bin']

        def snapshot():
            out = {}
            for n in names:
                with open(os.path.join(d, n), 'rb') as f:
                    out[n] = f.read()
            return out

        before = snapshot()
        self.write('a.cpp', 'void A() {}\nvoid B() {}\n')
        with open(os.path.join(d, 'index.tsv.skip.tsv'), 'rb'):  # Python は FILE_SHARE_DELETE 無しで開く
            rc, err = self.run_ltidx('-update')
        self.assertEqual(rc, 2, err)
        self.assertIn('the previous index is kept', err)
        self.assertEqual(snapshot(), before)

        # 途中で閉じれば差し替わり、退避したファイルも残らない
        import threading
        f = open(os.path.join(d, 'index.tsv.skip.tsv'), 'rb')
        threading.Timer(1.0, f.close).start()
        rc, err = self.run_ltidx('-update')
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('B', 'def', 'a.cpp:2:6'))
        self.assertEqual(sorted(os.listdir(d)), sorted(names))

    def test_macros(self):
        # マクロそのもの(#define・展開・#ifdef・#undef)と、展開で捨てられた引数の範囲(dropped)
        self.write('m.h', '#pragma once\n'
                          '#define LOG(x) ((void)0)\n'                          # 2: 引数を捨てる
                          '#define STR(x) #x\n'                                 # 3: 文字列にする
                          '#define WRAP(x) x\n'                                 # 4: そのまま残す
                          '#define DECLARE_GETTER(name) int Get##name();\n'     # 5: 貼り合わせる
                          '#define INNER(v, k) k(v)\n'                          # 6
                          '#define NESTED(v) Run()\n'                           # 7
                          '#define OUTER(v) INNER(v, NESTED)\n'                 # 8: 本体が NESTED を内側の引数に渡す
                          '#define GetMessage GetMessageW\n'                    # 9: 名前を置き換える(windows.h の形)
                          'void Run();\n'                                       # 10
                          'struct Mail { int GetMessage(); DECLARE_GETTER(Size) };\n')  # 11
        self.write('a.cpp', '#include "m.h"\n'
                            'void Run() {}\n'
                            'int Main(Mail &m) {\n'
                            '  LOG(Run());\n'                                   # 4
                            '  const char *s = STR(Run);\n'                     # 5
                            '  WRAP(Run());\n'                                  # 6
                            '  OUTER(1);\n'                                     # 7
                            '#ifdef LOG\n'                                      # 8
                            '#endif\n'
                            '  return m.GetMessage() + (s ? 1 : 0);\n'          # 10
                            '}\n'
                            '#undef WRAP\n', [])                                # 12
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('LOG', 'macro', 'm.h:2:9'))
        self.assertTrue(self.has('LOG', 'expand', 'a.cpp:4:3'))
        self.assertTrue(self.has('LOG', 'ifdef', 'a.cpp:8:8'))
        self.assertTrue(self.has('WRAP', 'undef', 'a.cpp:12:8'))
        self.assertTrue(self.has('OUTER', 'expand', 'a.cpp:7:3'))
        self.assertFalse(any(r[0] in ('INNER', 'NESTED') and r[1] == 'expand' for r in self.rows()))  # 書かれていない行に載せない
        self.assertTrue(self.has('Run', 'call', 'a.cpp:7:3'))  # NESTED の本体の呼び出しは、OUTER を使った行
        self.assertTrue(self.has('Run', 'call', 'a.cpp:6:8'))
        self.assertTrue(self.has('GetMessage', 'expand', 'm.h:11:19'))
        self.assertTrue(self.has('Mail::GetMessageW', 'decl', 'm.h:11:19'))
        with open(os.path.join(self.root, '.index', 'index.tsv.skip.tsv'), encoding='utf-8') as f:
            dropped = {tuple(l.rstrip('\n').split('\t')) for l in f if '\tdropped\t' in l}
        # LOG と STR の引数の Run、貼り合わされた Size。WRAP の引数は残るので出さない
        self.assertEqual(dropped, {('a.cpp', 'dropped', '4:7', '4:10'), ('a.cpp', 'dropped', '5:23', '5:26'),
                                   ('m.h', 'dropped', '11:48', '11:52')})
        out = self.ltq('refs', 'Run')
        self.assertIn('inside macro arguments the expansion does not keep as code', out)
        self.assertIn('a.cpp:4  | LOG(Run());', out)
        self.assertNotIn('other ->', out)
        out = self.ltq('refs', 'LOG')
        self.assertIn('symbol: LOG  (a macro)', out)
        self.assertIn('macro  m.h:2  | #define LOG(x) ((void)0)', out)
        self.assertIn('expand a.cpp:4  | LOG(Run());', out)
        self.assertIn('ifdef  a.cpp:8  | #ifdef LOG', out)
        out = self.ltq('refs', 'Mail::GetMessage')  # 置き換え後の Mail::GetMessageW を答える
        self.assertIn('the macro GetMessage renames it to Mail::GetMessageW', out)
        self.assertIn('call   a.cpp:10  [in Main]  | return m.GetMessage() + (s ? 1 : 0);', out)
        self.assertIn('decl   m.h:11', out)
        self.assertIn('expand m.h:11  [-> GetMessageW]', self.ltq('refs', 'GetMessage'))
        self.assertNotIn('GetMessage (', self.ltq('refs', 'Mail::GetMessage').split('other symbols named')[-1])

    def test_macro_rename_chain_and_directive(self):
        # 置き換え後の名前もマクロなら辿る(windows.h の CreateWindow -> CreateWindowW -> CreateWindowExW の形)。
        # 指令の行(#include HDR(x))のマクロの引数は構文解析に渡らないので、捨てられた引数にしない
        self.write('empty.h', '#pragma once\n')
        self.write('a.cpp', '#define HDR(x) #x\n'
                            '#include HDR(empty.h)\n'                              # 2
                            '#define GetObj GetObjW\n'
                            '#define GetObjW GetObjExW\n'
                            '#define MakeIt MakeItW\n'
                            '#define MakeItW(a) MakeItExW(0, a)\n'
                            'struct Mail { int GetObj(); };\n'                      # 7
                            'int MakeItExW(int, int);\n'
                            'int Use(Mail &m) { return MakeIt(5) + m.GetObj(); }\n', [])  # 9
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.root, '.index', 'index.tsv.skip.tsv'), encoding='utf-8') as f:
            self.assertNotIn('\tdropped\t', f.read())
        self.assertTrue(self.has('GetObj', 'expand', 'a.cpp:7:19', caller='GetObjExW'))
        self.assertTrue(self.has('MakeIt', 'expand', 'a.cpp:9:27', caller='MakeItExW'))
        out = self.ltq('refs', 'Mail::GetObj')
        self.assertIn('the macro GetObj renames it to Mail::GetObjExW', out)
        self.assertIn('call   a.cpp:9  [in Use]', out)
        self.assertIn('where the macro MakeIt is expanded, clang sees: MakeItExW (call 1)', self.ltq('refs', 'MakeIt'))

    def test_macro_parameter_is_not_a_rename(self):
        # 本体の先頭がマクロの引数名(X マクロの X(int)、CALL(fn) の fn(42))なら、置き換え後の名前にしない。
        # `\` で続く指令の行の中のマクロの引数も、捨てられた引数にしない
        self.write('empty.h', '#pragma once\n')
        self.write('a.cpp', '#define CALL(fn) fn(42)\n'
                            '#define FOR_EACH(X) X(1) X(2)\n'
                            '#define ID(x) x\n'
                            'int F(int);\n'
                            '#define G(v) F(v)\n'
                            'int Use() { return CALL(F) + G(3); }\n'           # 6
                            '#define HDR(x) #x\n'
                            '#include \\\n'
                            '  HDR(empty.h)\n', [])                            # 9: 指令の続きの行
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('CALL', 'expand', 'a.cpp:6:20', caller=''))
        self.assertTrue(self.has('CALL', 'macro', 'a.cpp:1:9', caller=''))
        self.assertTrue(self.has('FOR_EACH', 'macro', 'a.cpp:2:9', caller=''))
        self.assertTrue(self.has('G', 'expand', 'a.cpp:6:30', caller='F'))  # 引数名でない先頭は置き換え後の名前
        with open(os.path.join(self.root, '.index', 'index.tsv.skip.tsv'), encoding='utf-8') as f:
            self.assertNotIn('\tdropped\t', f.read())

    def test_macro_edge_cases(self):
        # 関数形式のマクロの本体の呼び出しは「置き換え」ではない。引数に書いたマクロの名前は捨てられた引数にしない。
        # 関数の中の変数・引数・テンプレートの引数は行にせず local の範囲にし、呼び出し元は囲む関数にする
        self.write('a.cpp', '#define LOG_ERROR(m) Logger::Error(m)\n'
                            '#define WRAP(x) x\n'
                            '#define ONE 1\n'
                            'struct Logger { static void Error(int); };\n'
                            'struct Box { int v; };\n'
                            'void begin();\n'
                            'template <class Compare> int Pick(Compare c) { return c(1); }\n'   # 7
                            'int Main() {\n'
                            '  LOG_ERROR(WRAP(ONE));\n'                                          # 9
                            '  int begin = 2;\n'                                                 # 10
                            '  static constexpr Box k_box{3};\n'                                 # 11
                            '  return begin + k_box.v;\n'                                        # 12
                            '}\n'
                            '#define DROP(x) ((void)0)\n'
                            '#define ERR_CODE 5\n'
                            'void Quiet() { DROP(ERR_CODE); }\n', [])                            # 16
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.root, '.index', 'index.tsv.skip.tsv'), encoding='utf-8') as f:
            skips = [l.rstrip('\n').split('\t') for l in f][1:]
        # ONE は展開されて 1 になる(捨てられていない)。引数を捨てる DROP の中の ERR_CODE は展開されないので捨てられた引数
        self.assertEqual([s for s in skips if s[1] == 'dropped'], [['a.cpp', 'dropped', '16:21', '16:29']])
        self.assertIn(['a.cpp', 'local', '10:7', '10:12'], skips)
        self.assertIn(['a.cpp', 'local', '12:10', '12:15'], skips)
        self.assertIn(['a.cpp', 'local', '7:17', '7:24'], skips)  # テンプレートの引数
        self.assertFalse(any(r[2].endswith('k_box') for r in self.rows()))  # 呼び出し元は関数の中の変数ではなく Main
        self.assertTrue(self.has('Box', 'ref', 'a.cpp:11:20', caller='Main'))
        out = self.ltq('refs', 'Logger::LOG_ERROR')
        self.assertIn('no symbol in the index matches this name', out)
        self.assertNotIn('renames it', out)
        out = self.ltq('refs', 'begin')
        self.assertIn('local variables, parameters or template parameters with this name -> other symbols, not the query: 2', out)
        self.assertNotIn('other ->', out)
        self.assertIn('local variables, parameters or template parameters', self.ltq('refs', 'Compare'))
        rc, err = self.run_ltidx('-no-local')  # 出さないこともできる(.skip.tsv を小さくしたい大きなコード向け)
        self.assertEqual(rc, 0, err)
        with open(os.path.join(self.root, '.index', 'index.tsv.skip.tsv'), encoding='utf-8') as f:
            self.assertNotIn('\tlocal\t', f.read())

    def test_sources_stay_writable_while_indexing(self):
        # LLVM は大きいファイルを mmap で読み、Windows ではマップされている間そのファイルに書けない(1224)。
        # 自動更新はエージェントがソースを書き換えている最中にも走るので、解析中もソースへ書けること
        body = '#pragma once\n' + ''.join(f'inline int f{i}(int x) {{ return x * {i}; }}\n' for i in range(12000))
        self.write('big.h', body)
        for k in range(8):
            self.write(f'tu{k}.cpp', '#include "big.h"\n' + ''.join(
                f'int g{k}_{i}() {{ return f{i}({k}); }}\n' for i in range(0, 12000, 3)), [])
        os.makedirs(os.path.join(self.root, '.index'), exist_ok=True)
        p = subprocess.Popen([LTIDX, '-p', self.root, '-root', self.root, '-o', os.path.join(self.root, '.index', 'index.tsv'),
                              '-j', '4'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        writes, failed = 0, []
        while p.poll() is None:
            try:
                with open(os.path.join(self.root, 'big.h'), 'w', newline='\n') as f:
                    f.write(body)
                writes += 1
            except OSError as e:
                failed.append(repr(e))
            time.sleep(0.02)
        # 書き直すたびに big.h は一瞬空になり、そこを読んだ TU は f0 が無いと NG になる(終了コード 1)。
        # 確かめたいのは書けることなので 1 までは許し、索引を書けなかったとき(2)や落ちたときだけ失敗にする
        self.assertIn(p.returncode, (0, 1))
        self.assertTrue(os.path.exists(os.path.join(self.root, '.index', 'index.tsv')))
        self.assertGreater(writes + len(failed), 0, 'ltidx finished before the first write')
        self.assertEqual(failed, [])

    def test_root_written_differently(self):
        # ルートを 8.3 形式の短い名前で渡し、compile_commands.json は長い名前(MSBuild が返す形)。どちらでもルート配下と判定する
        import ctypes
        long_dir = os.path.realpath(os.path.join(self.root, 'a-long-directory-name'))  # 一時フォルダ自体が短い名前のことがある
        buf = ctypes.create_unicode_buffer(1024)
        os.makedirs(long_dir)
        if not ctypes.windll.kernel32.GetShortPathNameW(long_dir, buf, 1024) or buf.value.lower() == long_dir.lower():
            self.skipTest('8.3 names are not available on this volume')
        short_dir = buf.value
        self.root = long_dir
        self.write('a.cpp', 'void Run();\nvoid Main() { Run(); }\n', [])
        for root in (short_dir, long_dir):
            r = subprocess.run([LTIDX, '-p', long_dir, '-root', root, '-o', os.path.join(long_dir, 'o.tsv')],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(self.has('Run', 'call', 'a.cpp:2:15', out='o.tsv'), root)
        self.root = os.path.dirname(long_dir)  # tearDown で消す(実体のパスで同じフォルダ)

    def test_update_needs_previous_index(self):
        self.write('a.cpp', 'void A() {}\n', [])
        rc, err = self.run_ltidx('-update')
        self.assertEqual(rc, 2)
        self.assertIn('-update', err)

    def skips(self, kind, out='.index/index.tsv'):
        with open(os.path.join(self.root, out + '.skip.tsv'), encoding='utf-8') as f:
            return sorted(tuple(c[:1] + c[2:]) for c in (line.rstrip('\n').split('\t') for line in f) if c[1] == kind)

    def test_reincluded_file_is_not_inactive(self):
        # ガードが clang の「2 回目は開かない」最適化に乗らない形(ガードの外にコードがある)。2 回目は本体がまるごと飛ぶが、
        # 1 回目に解析されているので、無効な範囲にしない。どの回でも無効な #ifdef の中だけを残す
        self.write('h.h', 'struct Tag;\n#ifndef H_H\n#define H_H\nvoid Run();\n#ifdef NEVER\nvoid Off();\n#endif\n#endif\n')
        self.write('a.cpp', '#include "h.h"\n#include "h.h"\nvoid Main() { Run(); }\n', [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.skips('inactive'), [('h.h', '5:1', '7:7')])

    def test_file_read_twice_with_different_macros(self):
        # マクロを変えて 2 回読む .inc: どちらの回でも無効な部分は無い
        self.write('list.inc', '#ifdef FIRST\nvoid A();\n#else\nvoid B();\n#endif\n')
        self.write('a.cpp', '#define FIRST\n#include "list.inc"\n#undef FIRST\n#include "list.inc"\n', [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.skips('inactive'), [])
        self.assertTrue(self.has('A', 'decl', 'list.inc:2:6'))
        self.assertTrue(self.has('B', 'decl', 'list.inc:4:6'))

    def test_overlay_replaces_a_file(self):
        self.write('h.h', 'void Run();\n#ifdef NEVER\ninline void Off() { Run(); }\n#endif\n')
        self.write('a.cpp', '#include "h.h"\nvoid Main() { Run(); }\n', [])
        rep = os.path.join(self.root, 'rep.h')
        with open(rep, 'wb') as f:
            f.write(b'void Run();\n#if 1\ninline void Off() { Run(); }\n#endif\n')
        ov = os.path.join(self.root, 'overlay.tsv')
        with open(ov, 'w', encoding='utf-8', newline='\n') as f:
            f.write(os.path.join(self.root, 'h.h') + '\t' + rep + '\n')
        rc, err = self.run_ltidx('-overlay', ov)
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.has('Run', 'call', 'h.h:3:21', caller='Off'))  # 元のパスの行として出る
        self.assertEqual(self.skips('inactive'), [])
        rc, err = self.run_ltidx('-overlay', os.path.join(self.root, 'missing.tsv'))
        self.assertEqual(rc, 2)

    def test_keep_going_after_missing_include(self):
        # 致命的なエラー(見つからない #include)の後、clang は解析を続けるが、テンプレートの実体化をやめる。
        # -keep-going は、致命的なエラーをふつうのエラーとして扱い、実体化の中の呼び出しも出す
        src = ('#include "missing.h"\ntemplate <class T> struct Box { void Put() { T t; t.Go(); } };\n'
               'struct Car { void Go(); };\nvoid Main() { Box<Car> b; b.Put(); }\n')
        self.write('a.cpp', src, [])
        rc, err = self.run_ltidx()
        self.assertEqual(rc, 1)
        self.assertTrue(self.has('Box::Put', 'call', 'a.cpp:4:29'))
        self.assertFalse(self.has('Car::Go', 'call', 'a.cpp:2:53'))
        rc, err = self.run_ltidx('-keep-going')
        self.assertEqual(rc, 1)
        self.assertTrue(self.has('Car::Go', 'call', 'a.cpp:2:53', caller='Box::Put'))
        # エラーの数の上限(既定 20)も外す
        many = ''.join(f'int v{i} = Missing{i};\n' for i in range(40))
        self.write('a.cpp', many + src.split('\n', 1)[1], [])
        rc, err = self.run_ltidx()
        self.assertFalse(self.has('Car::Go', 'call', 'a.cpp:41:53'))
        rc, err = self.run_ltidx('-keep-going')
        self.assertEqual(rc, 1)
        self.assertTrue(self.has('Car::Go', 'call', 'a.cpp:41:53'))
        self.assertEqual(self.tus()['a.cpp'][2], '40')

    def touch_later(self, rel, text):
        """索引の時刻(解析を始めた時刻)より確実に新しい時刻で書く。"""
        self.write(rel, text)
        t = time.time() + 2
        os.utime(os.path.join(self.root, rel), (t, t))

    def ltq(self, *args):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ltq.main(['--root', self.root] + list(args))
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()


if __name__ == '__main__':
    unittest.main()
