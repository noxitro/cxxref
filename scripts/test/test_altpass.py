"""altpass.py(無効な #if の中を仮に有効にして解析する差分パス)のテスト。
前半は分岐の選び方と書き換えだけ(ltidx 無しで走る)。後半は一時フォルダに小さなプロジェクトを作り、ltidx で索引にしてから
差分パスを走らせる(ltidx をビルドしてあるときだけ。場所は環境変数 LTIDX で変えられる)。

usage: python -I -m unittest discover -s scripts/test   (リポジトリのルートで)
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'query'))
import altpass  # noqa: E402
import ppcond  # noqa: E402

LTIDX = os.environ.get('LTIDX') or os.path.join(HERE, '..', '..', 'indexer', 'build', 'ltidx.exe')


def setup(src, ranges):
    """(グループ, 対象の行, 元の構成で有効な分岐)。ranges は .skip.tsv の無効な範囲 [(#if の行, #endif / #else の行)]。"""
    groups = ppcond.parse(src)
    off = altpass.lines_of(ranges)
    active = {b for g in groups for b in g.branches if b.body_first() is not None and b.body_first() not in off}
    return groups, off, active


def picked(chosen):
    return sorted((b.kind, b.line) for b in chosen.values())


class ChooseTest(unittest.TestCase):
    def test_inactive_branch_inside_include_guard(self):
        src = b'#ifndef H\n#define H\n#ifdef NEVER\nvoid Off();\n#endif\n#endif\n'
        groups, off, active = setup(src, [(3, 5)])
        chosen = altpass.choose(groups, set(off), active)
        self.assertEqual(picked(chosen), [('ifdef', 3)])  # インクルードガードには触らない
        self.assertEqual(altpass.rewrite(src, groups, chosen), b'#ifndef H\n#define H\n#if 1\nvoid Off();\n#endif\n#endif\n')

    def test_else_branch(self):
        src = b'#ifdef WIN\nvoid A();\n#else\nvoid B();\n#endif\n'
        groups, off, active = setup(src, [(3, 5)])
        chosen = altpass.choose(groups, set(off), active)
        self.assertEqual(picked(chosen), [('else', 3)])
        self.assertEqual(altpass.rewrite(src, groups, chosen), b'#if 0\nvoid A();\n#else\nvoid B();\n#endif\n')

    def test_one_branch_per_pass(self):
        src = b'#if A\nint a;\n#elif B\nint b;\n#elif C\nint c;\n#else\nint d;\n#endif\n'
        groups, off, active = setup(src, [(1, 3), (3, 5), (5, 7)])  # 有効なのは #else
        uncovered = set(off)
        chosen = altpass.choose(groups, uncovered, active)
        self.assertEqual(picked(chosen), [('if', 1)])
        self.assertEqual(altpass.rewrite(src, groups, chosen).split(b'\n')[0], b'#if 1')
        uncovered -= {2}
        chosen = altpass.choose(groups, uncovered, active)
        self.assertEqual(picked(chosen), [('elif', 3)])
        self.assertEqual(altpass.rewrite(src, groups, chosen).split(b'\n')[:3], [b'#if 0', b'int a;', b'#elif 1'])
        uncovered -= {4}
        chosen = altpass.choose(groups, uncovered, active)
        self.assertEqual(picked(chosen), [('elif', 5)])
        self.assertEqual(altpass.rewrite(src, groups, chosen),
                         b'#if 0\nint a;\n#elif 0\nint b;\n#elif 1\nint c;\n#else\nint d;\n#endif\n')
        uncovered -= {6}
        self.assertEqual(altpass.choose(groups, uncovered, active), {})

    def test_nested_inactive_branches(self):
        # 外側が無効なら、内側は外側と一緒に有効にする。内側の #else は次のパス
        src = b'#ifdef OUT\nint a;\n#ifdef IN\nint b;\n#else\nint c;\n#endif\n#endif\n'
        groups, off, active = setup(src, [(1, 8)])
        uncovered = set(off)
        chosen = altpass.choose(groups, uncovered, active)
        self.assertEqual(picked(chosen), [('ifdef', 1), ('ifdef', 3)])
        uncovered -= {2, 3, 4, 7}
        chosen = altpass.choose(groups, uncovered, active)
        self.assertEqual(picked(chosen), [('else', 5), ('ifdef', 1)])
        self.assertEqual(altpass.rewrite(src, groups, chosen), b'#if 1\nint a;\n#if 0\nint b;\n#else\nint c;\n#endif\n#endif\n')

    def test_if_zero_is_left_alone(self):
        src = b'#if 0\nold();\n#endif\n#ifdef X\nint x;\n#endif\n'
        groups, off, active = setup(src, [(1, 3), (4, 6)])
        self.assertEqual(picked(altpass.choose(groups, set(off), active)), [('ifdef', 4)])

    def test_ranges_already_parsed_by_another_tu(self):
        # .skip.tsv は TU ごとの無効な範囲の和。別の TU が解析した範囲には索引の行があるので、対象にしない
        src = b'#ifdef A\nint a;\n#elif defined(B)\nint b;\n#endif\n#ifdef C\nint c;\n#endif\n'
        groups = ppcond.parse(src)
        self.assertEqual(altpass.unparsed_ranges([(1, 5), (6, 8)], {7}, groups), [(1, 5)])
        self.assertEqual(altpass.unparsed_ranges([(1, 5), (6, 8)], {3, 5, 6, 8}, groups), [(1, 5), (6, 8)])  # 条件の行の行は別
        self.assertEqual(altpass.unparsed_ranges([(1, 5), (6, 8)], {4}, groups), [(6, 8)])

    def test_rewrite_keeps_line_count_and_line_ends(self):
        src = b'#if defined(A) && \\\r\n    defined(B)\r\nint x;\r\n#endif\r\n'
        groups, off, active = setup(src, [(1, 4)])
        out = altpass.rewrite(src, groups, altpass.choose(groups, set(off), active))
        self.assertEqual(out, b'#if 1\r\n\r\nint x;\r\n#endif\r\n')


class DefineTest(unittest.TestCase):
    def defines(self, line, known=()):
        return altpass.defines_for(ppcond.parse(line.encode() + b'\nint x;\n#endif\n')[0].branches[0], set(known))

    def test_macros_that_can_be_passed(self):
        self.assertEqual(self.defines('#ifdef FEATURE'), ['FEATURE'])
        self.assertEqual(self.defines('#if defined(FEATURE)'), ['FEATURE'])
        self.assertEqual(self.defines('#if defined FEATURE && defined(OTHER)'), ['FEATURE', 'OTHER'])
        self.assertEqual(self.defines('#if FEATURE'), ['FEATURE'])
        self.assertEqual(self.defines('#if defined(FEATURE) && !defined(OFF)'), ['FEATURE'])

    def test_conditions_that_are_not_guessed(self):
        self.assertIsNone(self.defines('#ifdef FEATURE', known={'FEATURE'}))  # ソースかコマンドラインに定義がある
        self.assertIsNone(self.defines('#ifdef _WIN64'))                      # コンパイラの予約名
        self.assertIsNone(self.defines('#ifndef FEATURE'))
        self.assertIsNone(self.defines('#if defined(A) || defined(B)'))
        self.assertIsNone(self.defines('#if VERSION >= 3'))
        self.assertIsNone(self.defines('#if FEATURE(x)'))
        self.assertIsNone(self.defines('#if defined(A) && !defined(B)', known={'B'}))
        self.assertIsNone(self.defines('#if 0'))

    def test_add_defines(self):
        e = altpass.add_defines({'file': 'a.cpp', 'arguments': ['cl.exe', '/c', 'a.cpp']}, ['A', 'B'])
        self.assertEqual(e['arguments'], ['cl.exe', '-DA', '-DB', '/c', 'a.cpp'])
        e = altpass.add_defines({'file': 'a.cpp', 'command': '"C:\\Program Files\\cl.exe" /c a.cpp -- x'}, ['A'])
        self.assertEqual(e['command'], '"C:\\Program Files\\cl.exe" -DA /c a.cpp -- x')
        src = {'file': 'a.cpp', 'command': 'cl /c a.cpp'}
        self.assertEqual(altpass.add_defines(src, []), src)


def run_altpass(*args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = altpass.main(list(args))
    return rc, buf.getvalue()


class SkipTest(unittest.TestCase):
    """差分パスを作れないときは、何もせずに 0 で終える(索引づくりを失敗にしない)。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='cri')
        self.index = os.path.join(self.root, '.index', 'index.tsv')
        os.makedirs(os.path.dirname(self.index))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_no_index(self):
        rc, out = run_altpass('--index', self.index, '--root', self.root, '--cdb', self.root)
        self.assertEqual(rc, 0)
        self.assertIn('skipped', out)
        self.assertFalse(os.path.exists(self.index + '.alt.tsv'))

    def test_no_root(self):
        rc, out = run_altpass('--index', self.index)
        self.assertEqual(rc, 2)

    @unittest.skipUnless(os.name == 'nt', 'uses a .cmd file as ltidx')
    def test_ltidx_without_overlay(self):
        for s in ('', '.skip.tsv', '.deps.tsv', '.tu.tsv'):
            with open(self.index + s, 'w'):
                pass
        fake = os.path.join(self.root, 'ltidx.cmd')
        with open(fake, 'w', newline='\r\n') as f:
            f.write('@echo usage: ltidx -p DIR -o FILE --update\n@exit /b 0\n')
        rc, out = run_altpass('--index', self.index, '--root', self.root, '--cdb', self.root, '--ltidx', fake)
        self.assertEqual(rc, 0)
        self.assertIn('no -overlay', out)
        self.assertFalse(os.path.exists(self.index + '.alt.tsv'))


@unittest.skipUnless(os.path.exists(LTIDX), 'ltidx.exe is not built')
class AltPassTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='cri')
        self.index = os.path.join(self.root, '.index', 'index.tsv')
        self.cmds = {}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, data, args=None):
        """args を渡すと、そのファイルを TU として compile_commands.json に載せる。"""
        fp = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, 'wb') as f:
            f.write(data.encode('utf-8'))
        if args is not None:
            self.cmds[rel] = args
        with open(os.path.join(self.root, 'compile_commands.json'), 'w', encoding='utf-8') as f:
            d = self.root.replace('\\', '/')
            json.dump([{'directory': d, 'file': f'{d}/{r}', 'arguments': ['cl.exe', '/c', '/std:c++17'] + a + [r]}
                       for r, a in self.cmds.items()], f)

    def build(self):
        os.makedirs(os.path.dirname(self.index), exist_ok=True)
        r = subprocess.run([LTIDX, '-p', self.root, '-root', self.root, '-o', self.index], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
        self.assertIn(r.returncode, (0, 1), r.stderr)

    def alt(self, *args):
        rc, out = run_altpass('--index', self.index, '--root', self.root, '--cdb', self.root, '--ltidx', LTIDX, '--jobs', '2',
                              *args)
        self.assertEqual(rc, 0, out)
        return out

    def rows(self):
        with open(self.index + '.alt.tsv', encoding='utf-8') as f:
            return [tuple(line.rstrip('\n').split('\t')[:4]) for line in f]

    def state(self):
        with open(self.index + '.alt.json', encoding='utf-8') as f:
            return json.load(f)

    def test_inactive_block_in_header(self):
        self.write('h.h', '#pragma once\nvoid Run();\n#ifdef NEVER\ninline void Off() { Run(); }\n#endif\n'
                          'inline void On() { Run(); }\n')
        self.write('a.cpp', '#include "h.h"\nvoid Main() { On(); }\n', [])
        self.build()
        out = self.alt()
        self.assertIn('1 files with inactive #if branches (1 analyzed, 0 unchanged)', out)
        # 元は無効だった行のものだけ。h.h:6 の On や a.cpp の行は、索引にあるので差分に入れない
        self.assertEqual(self.rows(), [('Off', 'def', '', 'h.h:4:13'), ('Run', 'call', 'Off', 'h.h:4:21')])
        u = self.state()['units']['h.h']
        self.assertEqual((u['target'], u['covered'], u['rows'], u['runs'], u['failed'], u['tu']), (1, 1, 2, 1, 0, 'a.cpp'))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.index)) if n.startswith('alt-work-')], [])

    def test_unchanged_files_are_reused(self):
        self.write('h.h', 'void Run();\n#ifdef NEVER\ninline void Off() { Run(); }\n#endif\n')
        self.write('g.h', 'void Run();\n#ifdef NEVER\ninline void Off2() { Run(); }\n#endif\n')
        self.write('a.cpp', '#include "h.h"\n#include "g.h"\n', [])
        self.build()
        self.assertIn('(2 analyzed, 0 unchanged)', self.alt())
        first = self.rows()
        self.assertEqual(len(first), 4)
        self.assertIn('(0 analyzed, 2 unchanged)', self.alt())
        self.assertEqual(self.rows(), first)
        # h.h を変えると、h.h を読む TU を使うファイルはどれも解析し直す(g.h の中の解決も変わりうる)
        self.write('h.h', 'void Run();\n#ifdef NEVER\ninline void Off() { Run(); Run(); }\n#endif\n')
        self.assertIn('(2 analyzed, 0 unchanged)', self.alt())
        self.assertEqual(len(self.rows()), 5)
        self.assertIn('(2 analyzed, 0 unchanged)', self.alt('--force'))

    def test_every_branch_gets_parsed(self):
        self.write('a.cpp', 'void A(); void B(); void C(); void D();\nvoid Main() {\n#if defined(P1)\n  A();\n#elif defined(P2)\n'
                            '  B();\n#elif defined(P3)\n  C();\n#else\n  D();\n#endif\n}\n', [])
        self.build()
        self.alt()
        self.assertEqual(self.rows(), [('A', 'call', 'Main', 'a.cpp:4:3'), ('B', 'call', 'Main', 'a.cpp:6:3'),
                                       ('C', 'call', 'Main', 'a.cpp:8:3')])
        u = self.state()['units']['a.cpp']
        self.assertEqual((u['target'], u['covered'], u['runs']), (3, 3, 3))
        self.alt('--passes', '1')
        self.assertEqual(self.rows(), [('A', 'call', 'Main', 'a.cpp:4:3')])

    def test_inactive_else(self):
        self.write('a.cpp', 'void A(); void B();\nvoid Main() {\n#ifndef OFF\n  A();\n#else\n  B();\n#endif\n}\n', [])
        self.build()
        self.alt()
        self.assertEqual(self.rows(), [('B', 'call', 'Main', 'a.cpp:6:3')])

    def test_nested_inactive_branches(self):
        # 無効な #if のすぐ中がまた #if: 両方を有効にする。中の #else は次の回
        self.write('a.cpp', 'void A(); void B(); void C();\nvoid Main() {\n#if RELEASE\n#if WIN64\n  A();\n#else\n  B();\n#endif\n'
                            '  C();\n#endif\n}\n', [])
        self.build()
        self.alt()
        self.assertEqual(self.rows(), [('A', 'call', 'Main', 'a.cpp:5:3'), ('B', 'call', 'Main', 'a.cpp:7:3'),
                                       ('C', 'call', 'Main', 'a.cpp:9:3')])
        u = self.state()['units']['a.cpp']
        self.assertEqual((u['target'], u['covered'], u['runs']), (3, 3, 2))

    def test_if_zero_is_not_parsed(self):
        self.write('a.cpp', 'void A();\nvoid Main() {\n#if 0\n  A();\n#endif\n}\n', [])
        self.build()
        self.alt()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.state()['units']['a.cpp']['runs'], 0)

    def test_macro_defined_nowhere_is_passed_as_define(self):
        # 宣言も同じマクロで囲まれている: #if を書き換えるだけでは Feat の宣言が無く、呼び出しが行にならない
        self.write('decl.h', '#ifdef FEATURE\nvoid Feat();\n#endif\n')
        self.write('a.cpp', '#include "decl.h"\nvoid Main() {\n#ifdef FEATURE\n  Feat();\n#endif\n}\n', [])
        self.build()
        self.alt('--no-define')
        self.assertEqual(self.rows(), [('?::Feat', 'dep', 'Main', 'a.cpp:4:3'), ('Feat', 'decl', '', 'decl.h:2:6')])
        self.alt()
        self.assertEqual(self.rows(), [('Feat', 'call', 'Main', 'a.cpp:4:3'), ('Feat', 'decl', '', 'decl.h:2:6')])
        self.assertEqual(self.state()['units']['a.cpp']['defines'], ['FEATURE'])

    def test_errors_in_the_flipped_code_do_not_stop_the_parse(self):
        self.write('a.cpp', 'void Run();\nvoid Main() {\n#ifdef OTHER_PLATFORM\n  Missing();\n  Run();\n#endif\n}\n'
                            '#ifdef OTHER_PLATFORM\n#include "not_here.h"\nvoid Later() { Run(); }\n#endif\n', [])
        self.build()
        self.alt()
        self.assertIn(('Run', 'call', 'Main', 'a.cpp:5:3'), self.rows())
        self.assertIn(('Run', 'call', 'Later', 'a.cpp:10:16'), self.rows())
        self.assertEqual(self.state()['units']['a.cpp']['errors'], 1)

    def test_header_no_tu_reads(self):
        self.write('h.h', 'void Run();\n#ifdef NEVER\ninline void Off() { Run(); }\n#endif\n')
        self.write('a.cpp', '#include "h.h"\n', [])
        self.build()
        self.cmds = {}
        self.write('b.cpp', 'int x;\n', [])  # compile_commands.json から a.cpp が消えた
        out = self.alt()
        self.assertIn('1 without a TU', out)
        self.assertEqual(self.rows(), [])

    def test_recipe(self):
        self.write('a.cpp', 'void A();\nvoid Main() {\n#ifdef NEVER\n  A();\n#endif\n}\n', [])
        self.build()
        with open(self.index + '.recipe.json', 'w', encoding='utf-8-sig') as f:  # index-project.ps1 は BOM 付きで書く
            json.dump({'version': 1, 'root': self.root, 'cdb': self.root, 'out': self.index, 'jobs': 2}, f)
        rc, out = run_altpass('--index', self.index, '--ltidx', LTIDX)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.rows(), [('A', 'call', 'Main', 'a.cpp:4:3')])


if __name__ == '__main__':
    unittest.main()
