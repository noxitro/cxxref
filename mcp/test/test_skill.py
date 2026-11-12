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
              f'python -I {cli} check \\\\evil.example\\share\\a.cpp:1', f'python -I {cli} refs @args']
        for c in ok:
            self.assertIn('"allow"', self.hook({'toolName': 'powershell', 'toolArgs': {'command': c}}), c)
        self.assertIn('"allow"', self.hook({'toolName': 'powershell', 'toolArgs': json.dumps({'command': ok[0]})}))
        for c in ng:
            self.assertEqual(self.hook({'toolName': 'powershell', 'toolArgs': {'command': c}}), '', c)
        self.assertEqual(self.hook({'toolName': 'bash', 'toolArgs': {'command': ok[0]}}), '')  # PowerShell だけ
        self.assertEqual(self.hook({'toolName': 'view', 'toolArgs': {'path': CLI}}), '')
        self.assertEqual(self.hook(b'not json'), '')


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
