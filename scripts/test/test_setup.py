"""setup.ps1 のテスト。
- LLVM の場所が間違っているときに、ビルドに進まず、indexer/llvm-root.txt を書き換えないこと(pwsh だけで走る)
- 通しで、索引・.index/.gitignore・使い方の指示ができ、Copilot CLI への登録を必要なときだけ行うこと
  (Visual Studio・LLVM・ビルド済みの ltidx.exe が要る。Copilot CLI は偽物の copilot.cmd に差し替えるので、本物の設定には触れない)
- 引数無し(ダブルクリック)のときの問い合わせ: 空のまま Enter で前の LLVM の場所を使うこと、Y/n、
  空白と日本語を含む LLVM の場所を build.bat(cmd の set /p)が読み戻せること
  (一時フォルダに写した setup.ps1 と、偽物の build.bat・LLVM で確かめる。pwsh だけで走り、本物のビルドと llvm-root.txt には触れない)
出力の文字コードは起動のしかたで変わるので、結果はファイルで確かめる。

usage: python -I -m unittest discover -s scripts/test   (リポジトリのルートで)
"""
import ctypes
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', 'setup.ps1')
INDEXER = os.path.join(HERE, '..', '..', 'indexer')
ROOT_FILE = os.path.join(INDEXER, 'llvm-root.txt')
LTIDX = os.path.join(INDEXER, 'build', 'ltidx.exe')
SERVER = os.path.realpath(os.path.join(HERE, '..', '..', 'mcp', 'server.py'))
VSWHERE = os.path.join(os.environ.get('ProgramFiles(x86)', ''), 'Microsoft Visual Studio', 'Installer', 'vswhere.exe')
PWSH = shutil.which('pwsh')

# 呼ばれた引数を記録し、`mcp get` には FAKE_COPILOT_SERVER があれば登録済み、無ければ未登録と答える。
# `plugin list --json` には FAKE_COPILOT_PLUGINS(入っているプラグインの JSON)を返す
FAKE_COPILOT = r'''@echo off
>>"%FAKE_COPILOT_LOG%" echo %*
if "%1 %2"=="plugin list" (if defined FAKE_COPILOT_PLUGINS echo %FAKE_COPILOT_PLUGINS%) & exit /b 0
if not "%1 %2"=="mcp get" exit /b 0
if not defined FAKE_COPILOT_SERVER exit /b 1
echo code-ref-index
echo   Command: python -I %FAKE_COPILOT_SERVER%
exit /b 0
'''


def read_or_none(path):
    try:
        with open(path, 'rb') as f:
            return f.read()
    except FileNotFoundError:
        return None


def llvm_root():
    if os.environ.get('LLVM_ROOT'):
        return os.environ['LLVM_ROOT']
    data = read_or_none(ROOT_FILE)
    return data.decode('mbcs' if os.name == 'nt' else 'utf-8').splitlines()[0].strip() if data else None


class SetupBase(unittest.TestCase):
    def setUp(self):
        self.saved = read_or_none(ROOT_FILE)  # setup.ps1 が書き換えるので、終わったら戻す
        self.tmp = tempfile.mkdtemp(prefix='cri')

    def tearDown(self):
        if self.saved is None:
            if os.path.exists(ROOT_FILE):
                os.remove(ROOT_FILE)
        else:
            with open(ROOT_FILE, 'wb') as f:
                f.write(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_ps(self, *args, env=None):
        e = {k: v for k, v in os.environ.items() if k.upper() != 'LLVM_ROOT'}
        e.update(env or {})
        return subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT] + list(args),
                              capture_output=True, text=True, errors='replace', env=e, stdin=subprocess.DEVNULL)


@unittest.skipUnless(PWSH, 'pwsh is required')
class SetupRejectTest(SetupBase):
    def test_wrong_llvm_folder_is_rejected(self):
        r = self.run_ps('-LlvmRoot', self.tmp, '-NoIndex')  # LLVM ではない空のフォルダ
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('ClangConfig.cmake', r.stdout + r.stderr)
        self.assertEqual(read_or_none(ROOT_FILE), self.saved)


@unittest.skipUnless(PWSH and os.path.exists(VSWHERE) and os.path.exists(LTIDX) and llvm_root(),
                     'needs pwsh, Visual Studio, LLVM and a built ltidx.exe')
class SetupFlowTest(SetupBase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, 'bin')
        self.src = os.path.join(self.tmp, 'src')
        self.log = os.path.join(self.tmp, 'copilot.log')
        os.makedirs(self.bin)
        os.makedirs(self.src)
        with open(os.path.join(self.bin, 'copilot.cmd'), 'w', newline='\r\n') as f:
            f.write(FAKE_COPILOT)
        with open(os.path.join(self.src, 'CMakeLists.txt'), 'w') as f:
            f.write('cmake_minimum_required(VERSION 3.20)\nproject(p CXX)\nadd_executable(p a.cpp)\n')
        with open(os.path.join(self.src, 'a.cpp'), 'w') as f:
            f.write('void f() {}\nint main() { f(); }\n')

    def setup(self, *args, registered=None):
        env = {'PATH': self.bin + os.pathsep + os.environ['PATH'], 'FAKE_COPILOT_LOG': self.log}
        if registered:
            env['FAKE_COPILOT_SERVER'] = registered
        r = self.run_ps('-LlvmRoot', llvm_root(), *args, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.log) as f:
            return [c for c in f.read().splitlines() if c != 'plugin list --json']

    def test_index_instructions_and_register(self):
        calls = self.setup('-Root', self.src, '-Instructions', '-RegisterCopilot')
        with open(os.path.join(self.src, '.index', 'index.tsv'), encoding='utf-8') as f:
            self.assertIn('f\tcall\tmain\ta.cpp:2:14', f.read())
        with open(os.path.join(self.src, '.index', '.gitignore'), encoding='utf-8') as f:
            self.assertEqual(f.read().splitlines()[-1], '*')
        with open(os.path.join(self.src, '.github', 'copilot-instructions.md'), encoding='utf-8') as f:
            self.assertIn('cxxref:begin', f.read())
        self.assertEqual(calls[0], 'mcp get code-ref-index')
        self.assertIn(f'mcp add code-ref-index -- python -I "{SERVER}"', calls)

    def test_already_registered_is_left_alone(self):
        calls = self.setup('-NoIndex', '-RegisterCopilot', registered=SERVER)
        self.assertEqual(calls, ['mcp get code-ref-index'])

    def test_other_registration_needs_the_switch(self):
        # 別の場所の server.py で登録されていても、-RegisterCopilot が無ければ登録し直さない(引数のときは尋ねない)
        calls = self.setup('-NoIndex', registered=r'D:\old\mcp\server.py')
        self.assertEqual(calls, ['mcp get code-ref-index'])
        self.assertFalse(os.path.exists(os.path.join(self.src, '.index')))


# llvm-root.txt を本物の build.bat と同じく set /p で読み、LLVM に見えるかを read.txt に書く(ファイルが無ければ none)
FAKE_BUILD = r'''@echo off
set LLVM_ROOT=
if not exist "%~dp0llvm-root.txt" (echo none>"%~dp0read.txt" & exit /b 0)
set /p LLVM_ROOT=<"%~dp0llvm-root.txt"
if exist "%LLVM_ROOT%\lib\cmake\clang\ClangConfig.cmake" (echo ok>"%~dp0read.txt") else (chcp>"%~dp0read.txt")
exit /b 0
'''


def oem_name(*names):
    """cmd のコードページ(OEM)で表せる最初の名前。日本語版なら日本語、英語版(437)なら é を使う。
    このプロセスのコンソールが OEM と別のコードページ(chcp で変えた・CI のランナーなど)なら、cmd は llvm-root.txt を
    そのコードページで読むので、ASCII でない名前は確かめられない。そのときは最後の名前(ASCII)を使う。
    set /p が使うのは出力側のコードページ。出力を受け取られた pwsh が [Console]::OutputEncoding を UTF-8 にすると、
    出力側だけが 65001 のまま残る(先に走る SetupFlowTest の index-project.ps1 など)ので、両方を見る。"""
    k = ctypes.windll.kernel32
    oem = k.GetOEMCP()
    if k.GetConsoleCP() not in (0, oem) or k.GetConsoleOutputCP() not in (0, oem):  # 0 はコンソールが無い(cmd は OEM の新しいコンソールで動く)
        return names[-1]
    for n in names:
        try:
            n.encode('oem')
            return n
        except UnicodeEncodeError:
            pass
    raise AssertionError(names)


@unittest.skipUnless(PWSH and os.name == 'nt', 'pwsh on Windows is required')
class SetupPromptTest(unittest.TestCase):
    def setUp(self):
        # 標準入力の文字コードは pwsh と Python で揃わないことがある(英語版の Windows など)ので、入力する場所は ASCII にし、
        # ASCII でない場所は llvm-root.txt と引数で渡す。8.3 形式の短い名前(RUNNER~1)は長い名前にしておく
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='cri setup '))
        self.repo = os.path.join(self.tmp, 'repo')
        for d in ('scripts', 'indexer', 'mcp', 'bin'):
            os.makedirs(os.path.join(self.repo, d))
        shutil.copy(SCRIPT, os.path.join(self.repo, 'scripts'))
        with open(os.path.join(self.repo, 'indexer', 'build.bat'), 'w', newline='\r\n') as f:
            f.write(FAKE_BUILD)
        open(os.path.join(self.repo, 'mcp', 'server.py'), 'w').close()
        with open(os.path.join(self.repo, 'bin', 'copilot.cmd'), 'w', newline='\r\n') as f:
            f.write(FAKE_COPILOT)
        self.root_file = os.path.join(self.repo, 'indexer', 'llvm-root.txt')
        self.log = os.path.join(self.tmp, 'copilot.log')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_llvm(self, name):
        p = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(p, 'lib', 'cmake', 'clang'))
        open(os.path.join(p, 'lib', 'cmake', 'clang', 'ClangConfig.cmake'), 'w').close()
        return p

    def setup(self, *args, answers=(), env=None):
        """setup.ps1 を args で起動し、問い合わせに answers を順に答える。(結果, build.bat が読めたか, 偽物の copilot に渡った引数)。"""
        e = {k: v for k, v in os.environ.items() if k.upper() != 'LLVM_ROOT'}
        e.update({'PATH': os.path.join(self.repo, 'bin') + os.pathsep + os.environ['PATH'], 'FAKE_COPILOT_LOG': self.log})
        e.update(env or {})
        r = subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                            os.path.join(self.repo, 'scripts', 'setup.ps1')] + list(args),
                           input=''.join(x + '\n' for x in answers), capture_output=True, text=True, errors='replace', env=e)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.repo, 'indexer', 'read.txt')) as f:
            read = f.read().strip()
        with open(self.log, encoding='oem') as f:
            calls = f.read().splitlines()
        self.assertEqual(calls[0], 'plugin list --json')  # 先にプラグインが入っているかを見る
        return r, read, calls[1:]

    def saved_root(self):
        with open(self.root_file, encoding='oem') as f:
            return f.read()

    def test_typed_path_with_quotes_and_no_registration(self):
        # LLVM の場所(空白を含み、引用符と末尾の \ 付き)、ルートは空で索引を飛ばす、登録は n
        llvm = self.fake_llvm('LLVM dir')
        r, read, calls = self.setup(answers=[f'"{llvm}\\"', '', 'n'])
        self.assertEqual(read, 'ok')  # build.bat が llvm-root.txt から LLVM を読み戻せた
        self.assertEqual(self.saved_root(), llvm + '\n')
        self.assertEqual(calls, ['mcp get code-ref-index'])
        self.assertFalse(os.path.exists(os.path.join(self.repo, '.index')))

    def test_empty_enter_reuses_the_saved_path_and_registers_by_default(self):
        llvm = self.fake_llvm(oem_name('LLVM の 場所', 'LLVM é dossier', 'LLVM saved dir'))  # 空白と ASCII でない文字
        with open(self.root_file, 'w', encoding='oem', newline='\r\n') as f:
            f.write(llvm + '\n')
        r, read, calls = self.setup(answers=['', '', ''])  # 3 つとも空のまま Enter(登録の Y/n は Y)
        self.assertEqual(read, 'ok')
        self.assertEqual(self.saved_root(), llvm + '\n')
        server = os.path.realpath(os.path.join(self.repo, 'mcp', 'server.py'))
        self.assertEqual(calls[0], 'mcp get code-ref-index')
        self.assertIn(f'mcp add code-ref-index -- python -I "{server}"', calls)

    def test_path_the_code_page_cannot_hold_is_not_written(self):
        # OEM で表せない文字の場所は、書くと ? に化けて build.bat が別の場所を読むので、llvm-root.txt に書かずに知らせる
        llvm = self.fake_llvm('LLVM \U0001F600')
        r, read, calls = self.setup('-LlvmRoot', llvm, '-NoIndex')
        self.assertEqual(read, 'none')
        self.assertFalse(os.path.exists(self.root_file))
        self.assertIn(f'OEM {ctypes.windll.kernel32.GetOEMCP()}', r.stdout)

    def test_registration_is_skipped_when_the_plugin_is_installed(self):
        # cxxref プラグインが MCP サーバーを持っているので、入っていれば登録も問い合わせもしない
        llvm = self.fake_llvm('LLVM dir')
        r, read, calls = self.setup('-LlvmRoot', llvm, '-NoIndex', '-RegisterCopilot',
                                    env={'FAKE_COPILOT_PLUGINS': '[{"name":"cxxref","enabled":true,"source":"x"}]'})
        self.assertEqual(calls, [])
        # 無効にしてあれば、ふだんどおり登録する
        os.remove(self.log)
        r, read, calls = self.setup('-LlvmRoot', llvm, '-NoIndex', '-RegisterCopilot',
                                    env={'FAKE_COPILOT_PLUGINS': '[{"name":"cxxref","enabled":false,"source":"x"}]'})
        self.assertEqual(calls[0], 'mcp get code-ref-index')
        self.assertTrue(any(c.startswith('mcp add code-ref-index') for c in calls), calls)

if __name__ == '__main__':
    unittest.main()
