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
              f'python -I {cli} refs X\t--root C:\\other', f'python -I {cli} refs X,--root,C:\\evil',
              # ' の中の ": 5.1 は、空白がすべて " の対の内側にある引数を包まずに渡し、\" で語が分かれる
              # 【実測: 5.1.26100 で --root と C:\evil が別の引数になった】
              f'python -I {cli} refs X \'--exclude=\\"a --root C:\\evil --exclude=b\\"\'',
              f'python -I {cli} check \'a.cpp:1" --root C:\\evil "b.cpp:2\'', f'python -I {cli} refs \'a"b\'',
              # python がバッチの shim だと cmd.exe を通る。引用符の中でも cmd が解釈する記号
              f'python -I {cli} refs "X&calc"', f'python -I {cli} refs "a>C:\\out.txt"', f'python -I {cli} refs "%PATH%"',
              f'python -I {cli} refs \'a|b\'', f'python -I {cli} refs "a^b"', f'python -I {cli} refs \'Vec<int>::push\'',
              # PowerShell が書き換える裸の語: - 1 つで始まる(. で分ける・: で次とつなぐ)、~ で始まる(ホームに展開)
              f'python -I {cli} refs X -a.b', f'python -I {cli} refs X -x: y', f'python -I {cli} refs ~',
              f'python -I {cli} refs X --path ~\\src', f'python -I {cli} refs -']
        ok += [f'python -I {cli} refs "Foo::operator()"', f'python -I {cli} refs \'operator bool\'',
               f'python -I {cli} refs Foo::~Foo', f'python -I {cli} refs "~Foo"', f'python -I {cli} refs "-a.b"',
               f'python -I {cli} refs X --path=src/a.b --exclude=~x',
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

    def test_allowed_words_reach_python_unchanged(self):
        # hook が通す形は、PowerShell(5.1 と 7)が python に渡す引数と、hook の分けた語が同じでなければならない
        # (違うと、hook が見ていない --root などを差し込める)。入っている PowerShell で実際に渡して確かめる
        sys.path.insert(0, os.path.dirname(HOOK))
        try:
            import cxxref_hook
        finally:
            sys.path.remove(os.path.dirname(HOOK))
        tails = ['refs World::BuildQuery', 'refs "Foo::operator()" --path "my src/a b" --limit 5',
                 "refs 'operator bool' \"--exclude=x,y\" --exclude third_party", 'refs Foo::~Foo "~Foo" "-a.b" --path=src/a.b',
                 'check a.cpp:3 b.h:10:2 "c d.cpp:1" src/日本語/e.cpp:2', "refs 'a$b`c' \"it's\" --path=a\\b\\ --",
                 'refs "a b\\c" \'[x] (y) {z} #1 @2 !3 ;4 ,5 =6 *7 ?8\' --exclude=~x a:~ --x:y.z']
        shells = [s for s in (shutil.which('powershell'), shutil.which('pwsh')) if s]
        if not shells:
            self.skipTest('no PowerShell')
        d = tempfile.mkdtemp()
        try:
            dump = os.path.join(d, 'dump.py')
            with open(dump, 'w', encoding='utf-8') as f:
                f.write('import json, sys\nsys.stdout.reconfigure(encoding="utf-8")\nprint(json.dumps(sys.argv[1:]))\n')
            lines = [f'python -I "{dump}" {t}' for t in tails]
            want = [cxxref_hook.split_words(c)[3:] for c in lines]
            for t in tails:  # 同じ語の並びを、hook は(自分の CLI に対してなら)通す
                self.assertTrue(cxxref_hook.allowed(f'python -I "{CLI}" {t}'), t)
            script = os.path.join(d, 'run.ps1')
            with open(script, 'w', encoding='utf-8-sig', newline='\r\n') as f:  # BOM: 5.1 は BOM 無しを ANSI で読む
                f.write('\n'.join(lines) + '\n')
            for shell in shells:
                p = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script], capture_output=True,
                                   timeout=120, stdin=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING='utf-8'))
                got = [json.loads(line) for line in p.stdout.decode('utf-8', 'replace').splitlines() if line.startswith('[')]
                self.assertEqual(got, want, (shell, p.stderr.decode('utf-8', 'replace')[:500]))
        finally:
            shutil.rmtree(d)

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
        hint, left = self.au.rebuild_hint(self.recipe('Release', cdb=cdb, jobs=3, alt=False), self.repo)
        # PowerShell でも cmd でも動く形(.bat を引用符付きで先頭に置くと、PowerShell では構文エラーになる)
        script = os.path.join(ROOT, 'scripts', 'index-project.ps1')
        self.assertTrue(hint.startswith(f'pwsh -NoProfile -ExecutionPolicy Bypass -File "{script}" -Root "{self.repo}" '), hint)
        self.assertIn('-Name Release', hint)
        self.assertIn(f'-Cdb "{cdb}"', hint)
        self.assertIn('-Jobs 3 -NoAlt', hint)
        self.assertTrue(hint.endswith(' -Update'))
        self.assertEqual(left, [])
        mk = {'Root': self.repo, 'Configuration': 'Release', 'Platform': 'x64', 'Out': cdb, 'CMakeArgs': '', 'Project': 'a.sln'}
        hint, left = self.au.rebuild_hint(self.recipe('index', makeCdb=mk), self.repo)
        self.assertNotIn('-Name', hint)
        self.assertIn('-Configuration "Release"', hint)
        self.assertIn('-Project "a.sln"', hint)
        self.assertNotIn('-CMakeArgs', hint)
        self.assertNotIn('-Cdb', hint)
        self.assertNotIn('-NoAlt', hint)
        self.assertEqual(left, [])
        # ルートの compile_commands.json を使った索引は、-Cdb を渡さなくても同じものを使う
        self.assertNotIn('-Cdb', self.au.rebuild_hint(self.recipe('index'), self.repo)[0])

    def test_rebuild_hint_follows_a_moved_folder(self):
        # 索引のフォルダを移した・別の PC から写したとき、記録の絶対パスは前の場所を指している
        old = os.path.join(os.path.dirname(self.repo), 'was-here')
        hint, _ = self.au.rebuild_hint(self.recipe('index', root=old, cdb=os.path.join(old, 'build')), self.repo)
        self.assertIn(f'-Cdb "{os.path.join(self.repo, "build")}"', hint)
        self.assertNotIn('was-here', hint)
        self.assertNotIn('-Cdb', self.au.rebuild_hint(self.recipe('index', root=old, cdb=old), self.repo)[0])
        mk = {'Root': old, 'Configuration': 'Debug', 'Platform': 'x64', 'Out': os.path.join(old, '.index', 'cdb-Debug-x64'),
              'CMakeArgs': '-DA=1 -DB=2', 'Project': os.path.join(old, 'game', 'game.sln')}
        hint, _ = self.au.rebuild_hint(self.recipe('index', root=old, makeCdb=mk), self.repo)
        self.assertIn(f'-Project "{os.path.join(self.repo, "game", "game.sln")}"', hint)
        self.assertIn('-CMakeArgs "-DA=1 -DB=2"', hint)
        outside = os.path.join(os.path.dirname(self.repo), 'elsewhere', 'cdb')  # ルートの外は、そのまま
        self.assertIn(f'-Cdb "{outside}"', self.au.rebuild_hint(self.recipe('index', root=old, cdb=outside), self.repo)[0])

    def test_rebuild_hint_never_carries_shell_syntax(self):
        # 記録は登録の無い(信頼していない)もの。値をそのままコマンドに埋めると、貼った shell が解釈する
        for bad in ('$(calc)', 'a`nb', 'x"; calc; "', 'a\u201d; calc; \u201cb', '%TEMP%', 'a & calc', 'a|b', "a'b", 'a;b',
                    'a\u00a0-Update', '\\\\evil.example\\share', 'C:\\build\\'):
            hint, left = self.au.rebuild_hint(self.recipe('index', cdb=bad), self.repo)
            self.assertEqual(left, ['-Cdb'], bad)
            self.assertNotIn('-Cdb', hint, bad)
            self.assertNotIn('calc', hint, bad)
            mk = {'Root': self.repo, 'Configuration': 'Debug', 'Platform': 'x64', 'Out': 'o', 'CMakeArgs': bad, 'Project': bad}
            hint, left = self.au.rebuild_hint(self.recipe('index', makeCdb=mk), self.repo)
            self.assertEqual(left, ['-Project', '-CMakeArgs'], bad)
            self.assertNotIn('calc', hint, bad)
        # 索引のファイル名(-Name)も記録と同じく信頼しない。index-project が作らない名前なら、コマンドを出さない
        self.assertEqual(self.au.rebuild_hint(self.recipe('a & calc'), self.repo), (None, []))
        self.assertEqual(self.au.rebuild_hint(self.recipe('index'), self.repo + '$(calc)'), (None, []))
        # 記録が辞書でなくても落ちない
        index = self.recipe('index')
        with open(index + '.recipe.json', 'w', encoding='utf-8') as f:
            f.write('[1]')
        self.assertTrue(self.au.rebuild_hint(index, self.repo)[0].endswith(' -Update'))

    def note(self, paths):
        """登録の無い索引 paths について、refresh が出力の先頭に足す注記(索引は古いものとする)。"""
        class Stale:
            def freshness(self):
                return ['a.cpp'], [], []
        sys.path.insert(0, os.path.join(ROOT, 'query'))  # refresh が ltq を読む
        try:
            idx = Stale()
            got, text = self.au.refresh(idx, paths, self.repo, 0, trusted_only=True)
        finally:
            sys.path.remove(os.path.join(ROOT, 'query'))
        self.assertIs(got, idx)
        return text

    def test_note_gives_one_command_per_line(self):
        # 構成が 2 つあるとき、; でつなぐと cmd では動かない。1 行に 1 つ出す
        text = self.note([self.recipe('Debug'), self.recipe('Release')])
        self.assertIn('not built on this PC', text)
        lines = [line for line in text.splitlines() if line.startswith('pwsh ')]
        self.assertEqual(len(lines), 2, text)
        self.assertTrue(lines[0].endswith('-Name Debug -Jobs 1 -Update') and lines[1].endswith('-Name Release -Jobs 1 -Update'), text)
        self.assertNotIn('; ', ''.join(lines))
        self.assertTrue(text.endswith('\n'))

    def test_note_says_what_it_left_out(self):
        text = self.note([self.recipe('index', cdb='$(calc)')])
        self.assertNotIn('calc', text)
        self.assertIn('the recorded -Cdb could not be shown safely', text)
        self.assertEqual(len([line for line in text.splitlines() if line.startswith('pwsh ')]), 1, text)
        # コマンドを 1 つも作れないとき(索引の名前が index-project の作らないもの)は、作り方だけを言う
        text = self.note([self.recipe('a & calc')])
        self.assertNotIn('pwsh ', text)
        self.assertNotIn('calc', text)
        self.assertIn('index-project.bat', text)

    def cdb(self, entries, rsp=None, raw=None, files=None):
        """entries(か raw の中身)の compile_commands.json を置いたフォルダ。rsp は inc.rsp の中身、files は {相対パス: 中身}。
        raw の {d} は、そのフォルダ(JSON の文字列の形)に置き換える。"""
        d = tempfile.mkdtemp(dir=self.repo)
        if raw is not None:
            data = (raw if isinstance(raw, bytes) else raw.encode('utf-8')).replace(b'{d}', json.dumps(d)[1:-1].encode())
        else:
            data = json.dumps([dict({'directory': d, 'file': 'a.cpp'}, **e) for e in entries]).encode('utf-8')
        with open(os.path.join(d, 'compile_commands.json'), 'wb') as f:
            f.write(data)
        for rel, body in dict(files or {}, **({'inc.rsp': rsp} if rsp else {})).items():
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), 'wb') as f:
                f.write(body if isinstance(body, bytes) else body.encode('utf-8'))
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

    def test_win_tokens(self):
        # ltidx(LLVM の TokenizeWindowsCommandLine)と同じ分け方【実測: ltidx 0.5.7 で、どれもこのとおりに効いた】
        t = lambda s: list(self.au._win_tokens(s))
        self.assertEqual(t('clang++ -DMA""RK "-DB" -D"C" -c a.cpp'), ['clang++', '-DMARK', '-DB', '-DC', '-c', 'a.cpp'])
        self.assertEqual(t('a\tb\r\nc\0d\x0ce\u00a0f'), ['a', 'b', 'c', 'd\x0ce\u00a0f'])  # FF と NBSP は区切りでない
        self.assertEqual(t('@"in c.rsp" "@in c.rsp" \'-DA\''), ['@in c.rsp', '@in c.rsp', "'-DA'"])
        self.assertEqual(t(r'-DQ=\"x\" "-DR=""y""" C:\a\b\ -DS\\"t u" ""'), ['-DQ="x"', '-DR="y"', 'C:\\a\\b\\', '-DS\\t u', ''])

    def test_unsafe_cdb_reads_like_ltidx(self):
        # 語の分け方・応答ファイルの読み方が ltidx と違うと、見落とす【実測: ltidx 0.5.7 は、下のどれでも引数を効かせた】
        u16 = b'\xff\xfe' + '-fplugin=p.dll'.encode('utf-16-le')
        cases = [
            dict(entries=[{'command': 'clang++ -f""plugin=p.dll a.cpp'}]),        # 語の途中の引用符
            dict(entries=[{'command': 'clang++ -"fplugin="p.dll a.cpp'}]),
            dict(entries=[{'command': 'clang++ -DX\0-fplugin=p.dll a.cpp'}]),      # NUL は区切り
            dict(entries=[{'command': 'clang++ -Wp,-load,p.dll a.cpp'}]),
            dict(entries=[{'command': 'clang++ @"inc.rsp" a.cpp'}], rsp='-fplugin=p.dll'),   # @ の後ろの引用符
            dict(entries=[{'command': 'clang++ "@in c.rsp" a.cpp'}], files={'in c.rsp': '-fplugin=p.dll'}),
            dict(entries=[{'command': 'clang++ @inc\u00a0x.rsp a.cpp'}], files={'inc\u00a0x.rsp': '-fplugin=p.dll'}),
            dict(entries=[{'command': 'clang++ @inc.rsp a.cpp'}], rsp='-f""plugin=p.dll'),
            dict(entries=[{'command': 'clang++ @inc.rsp a.cpp'}], rsp=u16),         # BOM 付きの UTF-16
            dict(entries=[{'command': 'clang++ @inc.rsp a.cpp'}], rsp='-DX\0-fplugin=p.dll'),
            dict(entries=[{'command': 'clang++ @inc.rsp a.cpp'}], rsp='@b.rsp', files={'b.rsp': '@c.rsp', 'c.rsp': '--config=x.cfg'}),
            dict(entries=[{'arguments': ['clang++', '@inc.rsp\0junk', 'a.cpp']}], rsp='-fplugin=p.dll'),  # NUL で切る
            dict(entries=[{'arguments': ['clang++', 'a.cpp'], 'command': 'clang++ -fplugin=p.dll a.cpp'}]),
            dict(entries=[{'command': '@inc.rsp a.cpp'}], rsp='clang++ -fplugin=p.dll'),                  # 先頭の語も
            # compile_commands.json は YAML として読まれる。厳密な JSON でなければ、見られないので安全でないとする
            dict(raw='[{"directory": "{d}", "file": "a.cpp", "command": "clang++ -fplugin=p.dll a.cpp"},]'),
            dict(raw="[{'directory': '{d}', 'file': 'a.cpp', 'command': 'clang++ -fplugin=p.dll a.cpp'}]"),
            dict(raw='# c\n[{"directory": "{d}", "file": "a.cpp", "command": "clang++ -fplugin=p.dll a.cpp"}]'),
            dict(raw='- directory: "{d}"\n  file: a.cpp\n  command: clang++ -fplugin=p.dll a.cpp\n'),
            dict(raw='[{"directory": "{d}", "file": "a.cpp", "command": "clang++ -fplugin=p.dll a.cpp", "command": "clang++ a.cpp"}]'),
            dict(raw=b'[{"directory": "{d}", "file": "a.cpp", "command": "clang++ \xff a.cpp"}]'),  # UTF-8 でない
            # コンパイラの隣の設定ファイル(引数に無くても読まれる)。ccache などの後ろのコンパイラも
            # (/ で区切る: ltidx は / も区切りとして読み、テストは \ を区切りにしない OS でも流れる)
            dict(entries=[{'command': 'tools/clang++.exe a.cpp'}], files={'tools/clang++.cfg': '-fplugin=p.dll'}),
            dict(entries=[{'arguments': ['ccache', 'tools/g++', 'a.cpp']}], files={'tools/x86_64-pc-windows-msvc.cfg': '-DA'}),
            dict(entries=[{'directory': 'rel', 'command': 'clang++ @inc.rsp a.cpp'}]),  # どこから探すか決まらない
        ]
        for c in cases:
            self.assertIsNotNone(self.au.unsafe_cdb(self.cdb(c.get('entries'), c.get('rsp'), c.get('raw'), c.get('files'))), c)
        ok = [
            dict(entries=[{'command': '"C:\\Program Files\\LLVM\\bin\\clang++.exe" -DS="a b" -I"C:\\inc dir" -c a.cpp'}]),
            dict(entries=[{'command': 'clang++ @inc.rsp @"inc.rsp" a.cpp', 'arguments': ['clang++', '@inc.rsp', 'a.cpp']}],
                 rsp=b'\xef\xbb\xbf-Iinc -DA="x y"\r\n@b.rsp', files={'b.rsp': '-DB @inc.rsp'}),  # 循環は ltidx が誤りにする
            dict(entries=[{'command': 'clang++ @missing.rsp -DX=@inc.rsp a.cpp'}], rsp='-fplugin=p.dll'),  # 語の頭の @ だけ
            dict(entries=[{'command': 'tools\\cl.exe -Iplugin -DLOADER a.cpp'}], files={'tools/openssl.cfg': 'x', 'clang.cfg': 'x'}),
            dict(raw='\ufeff[{"directory": "{d}", "file": "a.cpp", "command": "clang++ a.cpp"}]'),
            dict(raw='{"not": "an array"}'),  # ltidx が誤りにする
        ]
        for c in ok:
            self.assertIsNone(self.au.unsafe_cdb(self.cdb(c.get('entries'), c.get('rsp'), c.get('raw'), c.get('files'))), c)

    def test_unsafe_cdb_refuses_arguments_that_write_files(self):
        # 書き先を選べるファイルを書かせる引数【実測: ltidx 0.5.7 で、どれも指定した場所にファイルを作った】
        for cmd in ('clang++ --serialize-diagnostics x.dia a.cpp', 'clang++ -Xclang -serialize-diagnostic-file -Xclang x a.cpp',
                    'clang++ -H -Xclang -header-include-file -Xclang x a.cpp', 'clang++ -Xclang -diagnostic-log-file -Xclang x a.cpp',
                    'clang++ -Xclang -dependency-dot -Xclang x a.cpp', 'clang++ -Xclang=-dependency-file -Xclang=x a.cpp',
                    'clang++ -Xclang -stats-file=x a.cpp', 'clang++ -save-stats=cwd a.cpp', 'clang++ -gen-cdb-fragment-path x a.cpp',
                    'clang++ --write-dependencies -MF x a.cpp', 'clang++ --write-user-dependencies a.cpp',
                    'clang++ -fcrash-diagnostics-dir=x a.cpp', 'clang++ -Wp,-MD,x.d a.cpp', 'clang++ -Wp,-MMD,x.d a.cpp',
                    'clang-cl /clang:-MD /clang:-MFx.d a.cpp', 'clang-cl -clang:-MD a.cpp',
                    'clang-cl -clang:--serialize-diagnostics -clang:x a.cpp', 'clang-cl -clang:-fplugin=p.dll a.cpp',
                    'clang++ -Xclang -MF -Xclang x a.cpp', 'clang++ -Xpreprocessor -MD a.cpp',
                    'clang++ -Xclang -dump-minimization-hints=x a.cpp', 'clang++ -fmodules -fmodules-cache-path=x a.cpp'):
            self.assertIsNotNone(self.au.unsafe_cdb(self.cdb([{'command': cmd}])), cmd)
        # 応答ファイルの最後の -Xclang は、その次の語に効く(応答ファイルを置き換えてから並びとして見る)
        self.assertIsNotNone(self.au.unsafe_cdb(self.cdb([{'command': 'clang++ @inc.rsp -MF x a.cpp'}], rsp='-DA -Xclang')))
        # ltidx が落とす形(前に何も付かない -o・-MD・-MF・-MJ)と MSVC の出力先は、普通のプロジェクトにあるので止めない
        for cmd in ('clang++ -MD -MT a.o -MF a.d -c a.cpp -o a.o', 'clang++ -MMD -MFa.d -MJ a.json -c a.cpp',
                    'cl.exe /nologo /MD /Fo:a.obj /Fdx.pdb /Fpa.pch /Yupch.h /showIncludes /c a.cpp',
                    'clang++ -Xclang -fno-pch-timestamp -ftime-trace -c a.cpp'):
            self.assertIsNone(self.au.unsafe_cdb(self.cdb([{'command': cmd}])), cmd)

    def test_script_command(self):
        cmd = self.au.script_command('index-project', ('Root', self.repo))
        script = os.path.join(ROOT, 'scripts', 'index-project.ps1')
        self.assertEqual(cmd, f'pwsh -NoProfile -ExecutionPolicy Bypass -File "{script}" -Root "{self.repo}"')
        self.assertIsNone(self.au.script_command('index-project', ('Root', 'C:\\a$(calc)')))
        self.assertIsNone(self.au.script_command('index-project', ('Root', 'C:\\')))  # \ で終わると 5.1 は引用符を消す

    def test_unsafe_cdb_reads_each_response_file_once(self):
        # CMake の Ninja は、ターゲットの全 TU に同じ応答ファイルを付ける。TU の数だけ読み直さない
        d = self.cdb([{'command': 'clang++ @inc.rsp a.cpp', 'file': f'{i}.cpp'} for i in range(50)], rsp='-Iinc')
        opened, real = [], self.au._rsp_text
        self.au._rsp_text = lambda p: opened.append(p) or real(p)
        try:
            self.assertIsNone(self.au.unsafe_cdb(d))
        finally:
            self.au._rsp_text = real
        self.assertEqual(len(opened), 1)

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
