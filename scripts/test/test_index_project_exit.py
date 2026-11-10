"""index-project.ps1 の終了コードのテスト(pwsh だけで走る。ltidx は、終了コードを返すだけの偽物の .cmd に差し替える)。
- ltidx が 0 か 1(解析にエラーのあった TU がある。索引はできている)なら 0 で終える
- それ以外(2、STATUS_STACK_OVERFLOW などの異常終了)なら 2 で終え、作れなかったと知らせる(/cxxref-index がこれで成否を見る)
- 環境変数 LTIDX が無いファイルを指していれば、そう知らせて止まる

usage: python -I -m unittest discover -s scripts/test   (リポジトリのルートで)
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', 'index-project.ps1')
PWSH = shutil.which('pwsh')


@unittest.skipUnless(PWSH and os.name == 'nt', 'pwsh on Windows is required')
class IndexProjectExitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cri')
        self.cdb = os.path.join(self.tmp, 'cdb')
        os.makedirs(self.cdb)
        with open(os.path.join(self.cdb, 'compile_commands.json'), 'w') as f:
            f.write('[]')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_with(self, code):
        fake = os.path.join(self.tmp, 'ltidx.cmd')
        with open(fake, 'w', newline='\r\n') as f:
            f.write(f'@exit /b {code}\n')
        return self.index(fake)

    def index(self, ltidx):
        env = dict(os.environ, LTIDX=ltidx)
        return subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT,
                               '-Root', self.tmp, '-Cdb', self.cdb], capture_output=True, text=True, errors='replace',
                              env=env, stdin=subprocess.DEVNULL)

    def test_index_built(self):
        for code in (0, 1):
            with self.subTest(code=code):
                r = self.run_with(code)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_index_not_built(self):
        for code in (2, -1073741571):  # -1073741571 = 0xC00000FD(STATUS_STACK_OVERFLOW)
            with self.subTest(code=code):
                r = self.run_with(code)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_missing_ltidx_variable(self):
        r = self.index(os.path.join(self.tmp, 'none', 'ltidx.exe'))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('LTIDX', r.stdout + r.stderr)


LTIDX = os.environ.get('LTIDX') or os.path.join(HERE, '..', '..', 'indexer', 'build', 'ltidx.exe')


@unittest.skipUnless(PWSH and os.name == 'nt' and (os.path.exists(LTIDX) or os.environ.get('CRI_REQUIRE_LTIDX')),
                     'pwsh on Windows and ltidx.exe are required')
class IndexProjectAltTest(unittest.TestCase):
    """索引を作ると、続けて差分パス(無効な #if の中の解析)が走る。-NoAlt で止められる。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cri')
        with open(os.path.join(self.tmp, 'a.cpp'), 'w', newline='\n') as f:
            f.write('void Off();\nvoid Run() {\n#ifdef FEATURE\n  Off();\n#endif\n}\n')
        d = self.tmp.replace('\\', '/')
        with open(os.path.join(self.tmp, 'compile_commands.json'), 'w') as f:
            json.dump([{'directory': d, 'file': d + '/a.cpp', 'arguments': ['cl.exe', '/c', '/std:c++17', 'a.cpp']}], f)
        self.alt = os.path.join(self.tmp, '.index', 'index.tsv.alt.tsv')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def index(self, *more):
        return subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT,
                               '-Root', self.tmp, '-Cdb', self.tmp, '-Jobs', '1', *more],
                              capture_output=True, text=True, errors='replace',
                              env=dict(os.environ, LTIDX=os.path.abspath(LTIDX)), stdin=subprocess.DEVNULL)

    def test_alt_pass_runs_after_indexing(self):
        r = self.index()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('alt pass: 1 files', r.stdout)
        with open(self.alt, encoding='utf-8') as f:
            self.assertIn('Off\tcall\tRun\ta.cpp:4:3', f.read())
        with open(os.path.join(self.tmp, '.index', 'index.tsv.recipe.json'), encoding='utf-8') as f:
            self.assertTrue(json.load(f)['alt'])

    def test_no_alt(self):
        r = self.index('-NoAlt')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('alt pass', r.stdout)
        self.assertFalse(os.path.exists(self.alt))
        with open(os.path.join(self.tmp, '.index', 'index.tsv.recipe.json'), encoding='utf-8') as f:
            self.assertFalse(json.load(f)['alt'])


if __name__ == '__main__':
    unittest.main()
