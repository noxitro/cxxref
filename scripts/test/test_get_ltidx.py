"""package-ltidx.ps1 と get-ltidx.ps1 のテスト(LLVM 無しで使う配布物)。
- 作った zip を取り込むと、ltidx.exe・組み込みヘッダー・版が置かれ、その ltidx.exe で索引が作れること
- sha256 が合わない zip は取り込まず、前に置いたものを残すこと
pwsh・ビルド済みの ltidx.exe・それをビルドした LLVM(LLVM_ROOT か indexer/llvm-root.txt)が要る。ネットワークは使わない。

usage: python -I -m unittest discover -s scripts/test   (リポジトリのルートで)
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, '..')
LTIDX = os.path.join(HERE, '..', '..', 'indexer', 'build', 'ltidx.exe')
PLUGIN = os.path.join(HERE, '..', '..', 'plugin.json')
PWSH = shutil.which('pwsh')


def llvm_root():
    if os.environ.get('LLVM_ROOT'):
        return os.environ['LLVM_ROOT']
    p = os.path.join(HERE, '..', '..', 'indexer', 'llvm-root.txt')
    if os.path.exists(p):
        with open(p, 'rb') as f:
            return f.read().decode('mbcs' if os.name == 'nt' else 'utf-8').splitlines()[0].strip()
    return None


def ps(script, *args):
    return subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', os.path.join(SCRIPTS, script)]
                          + list(args), capture_output=True, text=True, errors='replace', stdin=subprocess.DEVNULL)


@unittest.skipUnless(PWSH and os.path.exists(LTIDX) and llvm_root(), 'needs pwsh, a built ltidx.exe and its LLVM')
class GetLtidxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='cri')
        # ライセンスの表示は CI が LLVM のソースから作って渡す(llvm-notices.py)。ここでは同梱されることだけを見る
        for n in ('LICENSE-LLVM', 'NOTICES'):
            with open(os.path.join(cls.tmp, n + '.txt'), 'w', encoding='utf-8') as f:
                f.write(f'{n} for the test\n')
        r = ps('package-ltidx.ps1', '-LlvmRoot', llvm_root(), '-OutDir', os.path.join(cls.tmp, 'dist'),
               '-LlvmLicense', os.path.join(cls.tmp, 'LICENSE-LLVM.txt'),
               '-ThirdPartyNotices', os.path.join(cls.tmp, 'NOTICES.txt'))
        assert r.returncode == 0, r.stdout + r.stderr
        cls.zip = os.path.join(cls.tmp, 'dist', 'cxxref-ltidx-win-x64.zip')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.dest = tempfile.mkdtemp(prefix='cri')
        self.ltidx = os.path.join(self.dest, 'ltidx')

    def tearDown(self):
        shutil.rmtree(self.dest, ignore_errors=True)

    def test_install_and_index(self):
        r = ps('get-ltidx.ps1', '-Zip', self.zip, '-Dest', self.ltidx)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(PLUGIN, encoding='utf-8') as f:
            version = json.load(f)['version']
        with open(os.path.join(self.ltidx, 'version.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), version)
        clang = os.listdir(os.path.join(self.ltidx, 'lib', 'clang'))
        self.assertTrue(os.path.exists(os.path.join(self.ltidx, 'lib', 'clang', clang[0], 'include', 'stddef.h')))
        # 二次配布の条件: cxxref・LLVM のライセンスと、ltidx.exe に埋め込まれた別ライセンスの部品の表示を同梱する
        with open(os.path.join(self.ltidx, 'THIRD-PARTY-NOTICES.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'NOTICES for the test\n')
        with open(os.path.join(self.ltidx, 'LICENSE-LLVM.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'LICENSE-LLVM for the test\n')
        self.assertTrue(os.path.exists(os.path.join(self.ltidx, 'LICENSE.txt')))
        # 置いた ltidx.exe で、組み込みヘッダーを使う TU の索引ができる
        src = os.path.join(self.dest, 'src')
        os.makedirs(src)
        with open(os.path.join(src, 'a.cpp'), 'w', newline='\n') as f:
            f.write('#include <stddef.h>\nvoid Run();\nvoid F() { size_t n = 0; Run(); }\n')
        d = src.replace('\\', '/')
        with open(os.path.join(src, 'compile_commands.json'), 'w') as f:
            json.dump([{'directory': d, 'file': f'{d}/a.cpp', 'arguments': ['cl.exe', '/c', 'a.cpp']}], f)
        out = os.path.join(src, 'index.tsv')
        r = subprocess.run([os.path.join(self.ltidx, 'ltidx.exe'), '-p', src, '-root', src, '-o', out],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out, encoding='utf-8') as f:
            self.assertIn('Run\tcall\tF\ta.cpp:3:26', f.read())

    def test_wrong_hash_keeps_the_previous_one(self):
        r = ps('get-ltidx.ps1', '-Zip', self.zip, '-Dest', self.ltidx)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        bad = os.path.join(self.dest, 'bad.zip')
        with open(self.zip, 'rb') as f, open(bad, 'wb') as g:
            g.write(f.read() + b'\0')  # 中身が 1 バイト違う
        shutil.copy(self.zip + '.sha256', bad + '.sha256')
        r = ps('get-ltidx.ps1', '-Zip', bad, '-Dest', self.ltidx)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('sha256', r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.ltidx, 'ltidx.exe')))  # 前のものが残る

    def test_old_zip_without_version_gets_one(self):
        # 版の記録(version.txt)の無い古い配布物には、取ってきた版を書く(/cxxref-index が毎回取り直さないように)
        import zipfile
        old = os.path.join(self.dest, 'old.zip')
        with zipfile.ZipFile(self.zip) as src, zipfile.ZipFile(old, 'w') as dst:
            for n in src.namelist():
                if os.path.basename(n) != 'version.txt':
                    dst.writestr(n, src.read(n))
        r = ps('get-ltidx.ps1', '-Zip', old, '-Dest', self.ltidx, '-Version', '0.4.9')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.ltidx, 'version.txt'), encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), '0.4.9')


if __name__ == '__main__':
    unittest.main()
