"""skill 版(skills/cxxref・mcp/cxxref_cli.py・com.github.copilot/hooks)の検査(Copilot は要らない)。

usage: python -I -m unittest discover -s mcp/test   (リポジトリのルートで)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
CLI = os.path.join(ROOT, 'mcp', 'cxxref_cli.py')
HOOK = os.path.join(ROOT, 'scripts', 'cxxref_hook.py')
SKILL = os.path.join(ROOT, 'skills', 'cxxref', 'SKILL.md')


def frontmatter(text):
    m = re.match(r'---\n(.*?)\n---\n', text, re.S)
    return dict(line.split(': ', 1) for line in m.group(1).splitlines()) if m else {}


class SkillTest(unittest.TestCase):
    def test_skill_manifest(self):
        with open(SKILL, encoding='utf-8') as f:
            text = f.read()
        fm = frontmatter(text)
        self.assertEqual(fm.get('name'), 'cxxref')
        # 説明文は、skill を使わない回でもモデルを呼ぶたびに送られる。短く保つ(Copilot の上限は 1,024 文字)
        self.assertLess(len(fm.get('description', '')), 500)
        # 本文が示す CLI の場所(SKILL.md のフォルダからの相対)が実在する
        self.assertIn(r'\..\..\mcp\cxxref_cli.py', text)
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(SKILL), '..', '..', 'mcp', 'cxxref_cli.py')))

    def test_hooks_manifest(self):
        # Agent Plugins 1.0 の $schema を持つプラグインの hooks は com.github.copilot/hooks/hooks.json だけが読まれる
        # 【実測: Copilot CLI 1.0.109 のログ。ルートの hooks.json は読まれない】
        self.assertFalse(os.path.exists(os.path.join(ROOT, 'hooks.json')))
        with open(os.path.join(ROOT, 'com.github.copilot', 'hooks', 'hooks.json'), encoding='utf-8') as f:
            h = json.load(f)
        self.assertEqual(h['version'], 1)
        self.assertEqual(set(h['hooks']), {'sessionStart', 'preToolUse'})
        for event in h['hooks'].values():
            script = re.search(r'\$\{PLUGIN_ROOT\}/([^"]+)', event[0]['command']).group(1)
            self.assertTrue(os.path.exists(os.path.join(ROOT, script)))
        self.assertTrue(h['hooks']['sessionStart'][0]['command'].endswith(' session'))

    def test_session_note_only_with_index(self):
        # 索引のあるリポジトリ(とその下のフォルダ)でだけ 1 文を足す。無いリポジトリでは何も足さない(上乗せ 0)
        root = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(root, 'src'))
            run = lambda cwd: subprocess.run([sys.executable, '-I', HOOK, 'session'], capture_output=True, timeout=30,
                                             input=json.dumps({'cwd': cwd, 'source': 'new'}).encode()).stdout.decode()
            self.assertEqual(run(os.path.join(root, 'src')), '')
            os.makedirs(os.path.join(root, '.index'))
            with open(os.path.join(root, '.index', 'index.tsv'), 'w', encoding='utf-8') as f:
                f.write('')
            out = json.loads(run(os.path.join(root, 'src')))
            self.assertIn('cxxref skill', out['additionalContext'])
            self.assertLess(len(out['additionalContext']), 300)
        finally:
            shutil.rmtree(root)

    def hook(self, payload):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
        p = subprocess.run([sys.executable, '-I', HOOK], input=data, capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        return p.stdout.decode('utf-8')

    def test_hook_allows_only_the_cli(self):
        cli = f'"{os.path.join(ROOT, "skills", "cxxref", "..", "..", "mcp", "cxxref_cli.py")}"'
        ok = [f'python -I {cli} refs World::BuildQuery', f'python.exe -I {cli} check a.cpp:3 b.h:10:2',
              f'python -I {cli} refs "ns::File::Read" --path runtime/core --limit 50']
        rel = os.path.relpath(CLI, os.path.dirname(HOOK))
        ng = [f'python -I {cli} refs X; Remove-Item foo', f'python -I {cli} refs X | Out-File a',
              f'python -I {cli} refs $(rm x)', f'python -I {cli} build X', f'python -c "import os" {cli} refs X',
              f'node {cli} refs X', 'python -I "C:\\tmp\\mcp\\cxxref_cli.py" refs X', f'python -I {cli} refs X\nrm x',
              # PowerShell は NBSP・全角空白も区切りに使う(Fable のレビューで、どちらも任意のコマンドが通った)
              f'cmd\u00a0/c\u00a0del\u00a0x\u00a0/python -I {cli} refs X', f'python\u3000evil.py\u3000/python -I {cli} refs X',
              f'.\\python -I {cli} refs X', f'C:\\Temp\\evil\\python.exe -I {cli} refs X',  # フォルダ付きの python
              f'python {cli} refs X', f'python -I -E {cli} refs X',  # -I が無い・ほかのフラグ
              f'python -I "{rel}" refs X',  # 相対パス
              f'python -I {cli} refs X --root C:\\other', f'python -I {cli} refs X --root=C:\\other',
              f'python -I {cli} check \\\\evil.example\\share\\a.cpp:1', f'python -I {cli} refs @args',
              # --root の省略形(argparse は既定で受ける。CLI でも切ってある)
              f'python -I {cli} refs X --roo C:\\other', f'python -I {cli} refs X --r=C:\\other',
              f'python -I {cli} check a.cpp:1 -r C:\\other',
              f'python -I {cli} refs X --path=\\\\evil.example\\share\\a',  # = の後ろの UNC
              # 古い渡し方(Windows PowerShell 5.1)では "" と \" で語が分かれる・空の語が消える
              f'python -I {cli} refs "X"" --root C:\\evil --path ""b c"', f'python -I {cli} refs "a b\\" --path x',
              f'python -I {cli} refs "" X', f'python -I {cli} refs "X"Y',
              f'python -I {cli} refs "$(rm x)"', f'python -I {cli} refs "`n"',  # " の中の展開
              f'python -I {cli} refs \u201cX\u201d', f'python -I {cli} refs X \u2013\u2013root C:\\other',  # 全角の引用符・ダッシュ
              f'python -I {cli} refs X\t--root C:\\other', f'python -I {cli} refs X,--root,C:\\evil']
        ok += [f'python -I {cli} refs "Foo::operator()"', f'python -I {cli} refs \'Vec<int>::push\'',
               f'python -I {cli} refs X --path src/\u65e5\u672c\u8a9e', f'python -I {cli} refs X --path "my src/a b"',
               f'python -I {cli} refs ns::Run --limit 5 --exclude third_party']
        for c in ok:
            self.assertIn('"allow"', self.hook({'toolName': 'powershell', 'toolArgs': {'command': c}}), c)
        self.assertIn('"allow"', self.hook({'toolName': 'powershell', 'toolArgs': json.dumps({'command': ok[0]})}))
        for c in ng:
            self.assertEqual(self.hook({'toolName': 'powershell', 'toolArgs': {'command': c}}), '', c)
        self.assertEqual(self.hook({'toolName': 'bash', 'toolArgs': {'command': ok[0]}}), '')  # PowerShell だけ
        self.assertEqual(self.hook({'toolName': 'view', 'toolArgs': {'path': CLI}}), '')
        self.assertEqual(self.hook(b'not json'), '')

    def test_hook_allows_a_plugin_folder_with_non_ascii_letters(self):
        # ユーザー名が日本語の PC では、プラグインの場所(%USERPROFILE%\.copilot\installed-plugins)に ASCII でない文字が入る
        base = tempfile.mkdtemp()
        try:
            plugin = os.path.join(base, '\u592a\u90ce', 'cxxref')
            os.makedirs(os.path.join(plugin, 'scripts'))
            os.makedirs(os.path.join(plugin, 'mcp'))
            shutil.copy2(HOOK, os.path.join(plugin, 'scripts'))
            open(os.path.join(plugin, 'mcp', 'cxxref_cli.py'), 'w').close()
            cli = os.path.join(plugin, 'mcp', 'cxxref_cli.py')
            data = json.dumps({'toolName': 'powershell', 'toolArgs': {'command': f'python -I "{cli}" refs X'}}).encode('utf-8')
            p = subprocess.run([sys.executable, '-I', os.path.join(plugin, 'scripts', 'cxxref_hook.py')], input=data,
                               capture_output=True, timeout=30)
            self.assertIn('"allow"', p.stdout.decode('utf-8'), p.stderr)
        finally:
            shutil.rmtree(base)


class TrustTest(unittest.TestCase):
    """作り直しの記録の登録(autoupdate.trust_recipe)と、作り直しの前の compile_commands.json の確かめ(unsafe_cdb)。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, 'mcp'))
        import autoupdate
        self.au = autoupdate
        self.home = tempfile.mkdtemp()
        self.repo = tempfile.mkdtemp()
        self.old = os.environ.get('LOCALAPPDATA')
        os.environ['LOCALAPPDATA'] = self.home
        os.makedirs(os.path.join(self.repo, '.index'))

    def tearDown(self):
        if self.old is None:
            os.environ.pop('LOCALAPPDATA', None)
        else:
            os.environ['LOCALAPPDATA'] = self.old
        shutil.rmtree(self.home)
        shutil.rmtree(self.repo)
        sys.path.remove(os.path.join(ROOT, 'mcp'))

    def recipe(self, name, **extra):
        index = os.path.join(self.repo, '.index', name + '.tsv')
        rec = dict({'version': 1, 'root': self.repo, 'cdb': self.repo, 'out': index, 'jobs': 1, 'makeCdb': None}, **extra)
        with open(index + '.recipe.json', 'w', encoding='utf-8') as f:
            json.dump(rec, f)
        return index

    def test_each_index_is_recorded_on_its_own(self):
        # 1 つのファイルを読んで足して書き戻すと、同時に登録した片方が消える。記録 1 つに 1 ファイルなら消えない
        a, b = self.recipe('Debug'), self.recipe('Release')
        self.assertTrue(self.au.trust_recipe(a))
        self.assertTrue(self.au.trust_recipe(b))
        self.assertTrue(self.au.trusted_recipe(a) and self.au.trusted_recipe(b))
        with open(b + '.recipe.json', 'a', encoding='utf-8') as f:
            f.write(' ')
        self.assertFalse(self.au.trusted_recipe(b))
        self.assertTrue(self.au.trusted_recipe(a))

    def test_reads_the_0_5_10_file(self):
        a = self.recipe('index')
        os.makedirs(os.path.join(self.home, 'cxxref'))
        with open(os.path.join(self.home, 'cxxref', 'trusted-recipes.json'), 'w', encoding='utf-8') as f:
            json.dump({self.au._recipe_key(a): self.au._recipe_digest(a)}, f)
        self.assertTrue(self.au.trusted_recipe(a))

    def test_rebuild_hint_keeps_the_configuration(self):
        # -Root だけを案内すると、-Name や作り元を付けて作った索引は、既定の構成を最初から作るだけになる
        cdb = os.path.join(self.repo, 'build')
        hint = self.au.rebuild_hint(self.recipe('Release', cdb=cdb), self.repo)
        self.assertIn('-Name Release', hint)
        self.assertIn(f'-Cdb "{cdb}"', hint)
        self.assertTrue(hint.endswith(' -Update'))
        mk = {'Root': self.repo, 'Configuration': 'Release', 'Platform': 'x64', 'Out': cdb, 'CMakeArgs': '', 'Project': 'a.sln'}
        hint = self.au.rebuild_hint(self.recipe('index', makeCdb=mk), self.repo)
        self.assertNotIn('-Name', hint)
        self.assertIn('-Configuration "Release"', hint)
        self.assertIn('-Project "a.sln"', hint)
        self.assertNotIn('-CMakeArgs', hint)
        self.assertNotIn('-Cdb', hint)

    def cdb(self, entries, rsp=None):
        d = tempfile.mkdtemp(dir=self.repo)
        with open(os.path.join(d, 'compile_commands.json'), 'w', encoding='utf-8') as f:
            json.dump([dict(e, directory=d, file='a.cpp') for e in entries], f)
        if rsp:
            with open(os.path.join(d, 'inc.rsp'), 'w', encoding='utf-8') as f:
                f.write(rsp)
        return d

    def test_unsafe_cdb(self):
        self.assertIsNone(self.au.unsafe_cdb(self.cdb([{'command': 'clang++ -c a.cpp -DX=1'}])))
        self.assertIsNone(self.au.unsafe_cdb(self.cdb([{'arguments': ['clang++', '@inc.rsp', 'a.cpp']}], rsp='-Iinc -DA')))
        self.assertIsNone(self.au.unsafe_cdb(os.path.join(self.repo, 'missing')))
        for e, rsp in [({'command': 'clang++ -fplugin=C:\\x\\p.dll a.cpp'}, None),
                       ({'arguments': ['clang++', '-Xclang', '-load', '-Xclang', 'p.dll', 'a.cpp']}, None),
                       ({'command': 'clang-cl /clang:-fpass-plugin=p.dll a.cpp'}, None),
                       ({'arguments': ['clang++', '--config=evil.cfg', 'a.cpp']}, None),
                       ({'command': 'clang++ "-fplugin=C:\\a b\\p.dll" a.cpp'}, None),
                       ({'arguments': ['clang++', '@inc.rsp', 'a.cpp']}, '-Iinc\n-fplugin=p.dll\n'),
                       ({'arguments': ['clang++', '@\\\\evil.example\\share\\x.rsp', 'a.cpp']}, None)]:
            self.assertIsNotNone(self.au.unsafe_cdb(self.cdb([e], rsp)), e)

    def test_rebuild_refuses_an_unsafe_cdb(self):
        # ltidx を探す前に止める(ltidx が無くても、この理由で失敗する)
        d = self.cdb([{'command': 'clang++ -fplugin=p.dll a.cpp'}])
        err = self.au.run_recipe({'cdb': d, 'root': self.repo, 'out': os.path.join(self.repo, '.index', 'index.tsv')},
                                 False, None)
        self.assertIn('-fplugin=p.dll', err)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, 'src'))
        os.makedirs(os.path.join(self.root, '.index'))
        with open(os.path.join(self.root, 'src', 'a.cpp'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('void Run();\nvoid Main() {\n  Run();\n  // Run\n}\n')
        base = os.path.join(self.root, '.index', 'index.tsv')
        with open(base, 'w', encoding='utf-8', newline='\n') as f:
            f.write('Run\tdecl\t\tsrc/a.cpp:1:6\tvoid Run();\n'
                    'Run\tcall\tMain\tsrc/a.cpp:3:3\tRun();\n')
        with open(base + '.skip.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('path\tkind\tbegin\tend\nsrc/a.cpp\tcomment\t4:3\t4:9\n')
        with open(base + '.tu.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write(f'file\tok\terrors\tsec\trefs\tfirst_error\n{os.path.join(self.root, "src", "a.cpp")}\t1\t0\t0.1\t2\t\n')

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_cli(self, *args, cwd=None, env=None):
        e = dict(os.environ, **(env or {'CRI_AUTO_UPDATE': '0'}))
        if env:
            e.pop('CRI_AUTO_UPDATE', None)
        p = subprocess.run([sys.executable, '-I', CLI] + list(args), cwd=cwd or self.root, env=e,
                           capture_output=True, timeout=120)
        return p.returncode, p.stdout.decode('utf-8')

    def test_refs_from_subfolder(self):
        # 作業フォルダから親へさかのぼって索引を探す(MCP サーバーと同じ)
        code, text = self.run_cli('refs', 'Run', cwd=os.path.join(self.root, 'src'))
        self.assertEqual(code, 0, text)
        self.assertIn('src/a.cpp:3', text)
        self.assertIn('comment', text)

    def test_check(self):
        code, text = self.run_cli('check', 'src/a.cpp:4')
        self.assertEqual(code, 0, text)
        self.assertIn('comment', text)

    def test_rebuilds_only_recipes_built_on_this_pc(self):
        # 索引の後にソースが変わった。作り直しの記録はあるが、この PC で登録していない
        base = os.path.join(self.root, '.index', 'index.tsv')
        with open(base + '.recipe.json', 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'root': self.root, 'cdb': os.path.join(self.root, 'cdb'), 'out': base, 'jobs': 1,
                       'makeCdb': None, 'alt': False}, f)
        later = os.path.getmtime(base) + 60
        os.utime(os.path.join(self.root, 'src', 'a.cpp'), (later, later))
        home = tempfile.mkdtemp()
        env = {'LOCALAPPDATA': home, 'CRI_AUTO_UPDATE_WAIT': '20', 'LTIDX': os.path.join(home, 'missing', 'ltidx.exe')}
        try:
            code, text = self.run_cli('refs', 'Run', env=env)
            self.assertEqual(code, 0, text)
            self.assertIn('auto-update: off for this index (it was not built on this PC', text)
            self.assertFalse(os.path.exists(os.path.join(self.root, '.index', 'update.json')))  # 作り直しを始めていない
            # index-project が書いた直後と同じく登録すると、作り直しを始める(ltidx が無いので失敗として返る)
            p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'mcp', 'autoupdate.py'), '--trust', base],
                               env=dict(os.environ, **env), capture_output=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            code, text = self.run_cli('refs', 'Run', env=env)
            self.assertNotIn('not built on this PC', text)
            self.assertIn('auto-update: rebuild failed', text)
            # 記録を書き換えると、登録は外れる
            with open(base + '.recipe.json', 'a', encoding='utf-8') as f:
                f.write(' ')
            code, text = self.run_cli('refs', 'Run', env=env)
            self.assertIn('not built on this PC', text)
        finally:
            shutil.rmtree(home)

    def test_root_is_not_abbreviated(self):
        # hook は --root を断る。argparse の省略(--roo・--r=)で同じことができないようにする
        for opt in (['--roo', self.root], [f'--r={self.root}']):
            code, _ = self.run_cli('refs', 'Run', *opt)
            self.assertEqual(code, 2, opt)

    def test_error_is_one_line(self):
        # 索引の読み込みの失敗などは、トレースバックではなく 1 行の誤りにする(MCP サーバーと同じ)
        sys.path.insert(0, os.path.join(ROOT, 'mcp'))
        try:
            import io
            import contextlib
            import cxxref_cli
            def boom(*_):
                raise ValueError('broken index')
            old, cxxref_cli.server.call_tool = cxxref_cli.server.call_tool, boom
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    code = cxxref_cli.main(['refs', 'Run'])
            finally:
                cxxref_cli.server.call_tool = old
        finally:
            sys.path.remove(os.path.join(ROOT, 'mcp'))
        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().startswith('ValueError: broken index\n'), out.getvalue())

    def test_no_index(self):
        empty = tempfile.mkdtemp()
        try:
            code, text = self.run_cli('refs', 'Run', cwd=empty)
        finally:
            shutil.rmtree(empty)
        self.assertNotEqual(code, 0)
        self.assertIn('No cxxref index', text)


if __name__ == '__main__':
    unittest.main()
