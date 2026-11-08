"""MCP サーバーの動作テスト: server.py を子プロセスで起動し、stdio の JSON-RPC で一通り呼ぶ。

usage: python -I -m unittest discover -s mcp/test   (リポジトリのルートで)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'server.py')


class ServerTest(unittest.TestCase):
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
        self.start(['--root', self.root])

    def start(self, args, cwd=None, env=None):
        e = {k: v for k, v in os.environ.items() if k != 'CRI_MAX_OUTPUT_BYTES'}
        e.update(env or {})
        self.proc = subprocess.Popen([sys.executable, '-I', SERVER] + args, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=cwd, env=e)
        self.next_id = 0

    def restart(self, args, env=None):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.start(args, env=env)

    def many_comments(self):
        """コメント内の一致が 40 件ある索引にする。全文は約 1,700 バイト、短い形(--compact)は約 800 バイト。"""
        with open(os.path.join(self.root, 'src', 'a.cpp'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('void Run();\nvoid Main() {\n  Run();\n' + '  // Run\n' * 40 + '}\n')
        with open(os.path.join(self.root, '.index', 'index.tsv.skip.tsv'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('path\tkind\tbegin\tend\n' + ''.join(f'src/a.cpp\tcomment\t{4 + i}:3\t{4 + i}:9\n' for i in range(40)))
        for p in ('index.tsv', 'index.tsv.skip.tsv', 'index.tsv.tu.tsv'):  # 索引をソースより新しくする(作り直しを避ける)
            os.utime(os.path.join(self.root, '.index', p))

    def refs_text(self):
        return self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run'}})['result']['content'][0]['text']

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        shutil.rmtree(self.root)

    def send(self, method, params=None, notify=False):
        msg = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notify:
            self.next_id += 1
            msg['id'] = self.next_id
        self.proc.stdin.write((json.dumps(msg) + '\n').encode('utf-8'))
        self.proc.stdin.flush()
        if notify:
            return None
        reply = json.loads(self.proc.stdout.readline().decode('utf-8'))
        self.assertEqual(reply['id'], self.next_id)
        return reply

    def test_session(self):
        r = self.send('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                     'clientInfo': {'name': 'test', 'version': '0'}})
        self.assertEqual(r['result']['serverInfo']['name'], 'code-ref-index')
        self.assertIn('tools', r['result']['capabilities'])
        self.send('notifications/initialized', notify=True)  # 通知には返事をしない(次の返事の id で確かめる)

        names = [t['name'] for t in self.send('tools/list')['result']['tools']]
        self.assertEqual(names, ['refs', 'check'])

        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run'}})['result']
        self.assertFalse(r['isError'])
        text = r['content'][0]['text']
        self.assertIn('== calls (resolved by clang): 1\n  call   src/a.cpp:3  [in Main]', text)
        self.assertIn('inside comments -> not code; do not report these: 1', text)

        r = self.send('tools/call', {'name': 'check', 'arguments': {'locations': ['src/a.cpp:4:6']}})['result']
        self.assertIn('inside comment 4:3-4:9', r['content'][0]['text'])

        r = self.send('tools/call', {'name': 'nope', 'arguments': {}})
        self.assertEqual(r['error']['code'], -32602)
        r = self.send('no/such/method')
        self.assertEqual(r['error']['code'], -32601)

    def test_missing_index_is_a_tool_error(self):
        shutil.rmtree(os.path.join(self.root, '.index'))
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run'}})['result']
        self.assertTrue(r['isError'])
        self.assertIn('index not found', r['content'][0]['text'])
        # 作り方を、このサーバーの隣の scripts の絶対パスで添える(プラグインとして入れたときに場所が分かるように)
        scripts = os.path.normpath(os.path.join(os.path.dirname(SERVER), '..', 'scripts'))
        self.assertIn(f'"{os.path.join(scripts, "index-project.bat")}" -Root "{self.root}"', r['content'][0]['text'])

    def test_large_result_is_shortened(self):
        # クライアントの上限(Copilot CLI は 20,480 バイト)を超える結果は、短い形にし、それでも超えれば末尾を切る
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.start(['--root', self.root, '--max-output-bytes', '400'])
        text = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run'}})['result']['content'][0]['text']
        self.assertLessEqual(len(text.encode('utf-8')), 400)
        self.assertTrue(text.startswith('index: .index/index.tsv'))  # 先頭(索引の状態)は残す
        self.assertIn('output cut at 400 bytes', text)  # 短い形の中身は ltq のテストで確かめる

    def test_plugin_server_finds_the_workspace_from_the_copilot_session(self):
        # プラグインとして起動されると、作業フォルダはプラグインの場所(PLUGIN_ROOT)になる。Copilot CLI のセッションの
        # 記録(session-state/<id>/workspace.yaml の cwd)から作業フォルダを取る
        plugin = tempfile.mkdtemp(prefix='cri')
        try:
            home = os.path.join(plugin, 'home')
            os.makedirs(os.path.join(home, 'session-state', 'abc-123'))
            with open(os.path.join(home, 'session-state', 'abc-123', 'workspace.yaml'), 'w', encoding='utf-8') as f:
                f.write(f'id: abc-123\ncwd: {self.root}\nclient_name: github/cli\n')
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
            self.proc.stdout.close()
            self.start([], cwd=plugin, env={'PLUGIN_ROOT': plugin, 'COPILOT_HOME': home, 'COPILOT_AGENT_SESSION_ID': 'abc-123'})
            self.assertIn('call   src/a.cpp:3  [in Main]', self.refs_text())
        finally:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
            self.proc.stdout.close()
            shutil.rmtree(plugin)
            self.start(['--root', self.root])  # tearDown が閉じる

    def as_plugin(self, session_cwd=None, quote=False):
        """プラグインとして起動したサーバー(作業フォルダが PLUGIN_ROOT)にする。session_cwd を渡すと Copilot のセッションの記録を置く。
        終わったら restore_default() を呼ぶ。戻り値はプラグインのフォルダ(消すのは restore_default)。"""
        plugin = tempfile.mkdtemp(prefix='cri')
        home = os.path.join(plugin, 'home')
        env = {'PLUGIN_ROOT': plugin, 'COPILOT_HOME': home, 'COPILOT_AGENT_SESSION_ID': 'abc-123'}
        if session_cwd:
            os.makedirs(os.path.join(home, 'session-state', 'abc-123'))
            v = "'" + session_cwd.replace("'", "''") + "'" if quote else session_cwd  # YAML の単一引用符
            with open(os.path.join(home, 'session-state', 'abc-123', 'workspace.yaml'), 'w', encoding='utf-8') as f:
                f.write(f'id: abc-123\ncwd: {v}\n')
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.start([], cwd=plugin, env=env)
        self._plugin = plugin
        return plugin

    def restore_default(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        shutil.rmtree(self._plugin)
        self.start(['--root', self.root])  # tearDown が閉じる

    def test_relative_root_and_quoted_session_cwd(self):
        # 相対の root は、プラグインの場所ではなく作業中のフォルダから解決する。セッションの記録の cwd は YAML の引用符を外す
        parent = os.path.dirname(self.root)
        self.as_plugin(parent, quote=True)
        try:
            r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run', 'root': os.path.basename(self.root)}})
            self.assertIn('call   src/a.cpp:3  [in Main]', r['result']['content'][0]['text'])
        finally:
            self.restore_default()

    def test_unknown_workspace_does_not_suggest_indexing_the_plugin(self):
        # 作業中のフォルダが分からない(セッションの記録が無い)ときは、プラグインの場所の索引づくりを勧めず、root を渡させる
        plugin = self.as_plugin()
        try:
            text = self.refs_text()
            self.assertIn('could not tell which folder you are working in', text)
            self.assertNotIn(f'-Root "{plugin}"', text)
        finally:
            self.restore_default()

    def test_refs_without_name_is_a_tool_error(self):
        # 引数の足りない呼び出しは、「未知のツール」(JSON-RPC の誤り)ではなく、ツールの誤りとして返す
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'root': self.root}})
        self.assertNotIn('error', r)
        self.assertTrue(r['result']['isError'])
        self.assertIn('needs "name"', r['result']['content'][0]['text'])
        self.assertIn('error', self.send('tools/call', {'name': 'nope', 'arguments': {}}))  # 本当に未知のツール

    def test_root_argument(self):
        # 作業フォルダが分からないクライアント向けに、ツールの引数 root でルートを渡せる
        elsewhere = tempfile.mkdtemp(prefix='cri')
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
            self.proc.stdout.close()
            self.start([], cwd=elsewhere)
            r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run', 'root': self.root}})['result']
            self.assertFalse(r['isError'], r['content'][0]['text'])
            self.assertIn('call   src/a.cpp:3  [in Main]', r['content'][0]['text'])
            r = self.send('tools/call', {'name': 'check', 'arguments': {'locations': ['src/a.cpp:3'], 'root': self.root}})
            self.assertIn('src/a.cpp:3', r['result']['content'][0]['text'])
        finally:
            self.proc.stdin.close()  # サーバーの作業フォルダを消せるように止める
            self.proc.wait(timeout=10)
            self.proc.stdout.close()
            shutil.rmtree(elsewhere)
            self.start(['--root', self.root])  # tearDown が閉じる

    def test_compact_form_that_fits_is_not_cut(self):
        # 全文が上限を超え、短い形なら収まるときは、短い形をそのまま返す(切らない)
        self.many_comments()
        self.restart(['--root', self.root, '--max-output-bytes', '1000'])
        text = self.refs_text()
        self.assertLessEqual(len(text.encode('utf-8')), 1000)
        self.assertIn('(compact output:', text)
        self.assertIn('call   src/a.cpp:3  [in Main]  | Run();', text)
        self.assertIn('inside comments -> not code; do not report these: 40', text)
        self.assertNotIn('output cut', text)

    def test_limit_from_the_environment_and_zero_means_no_limit(self):
        self.many_comments()
        self.restart(['--root', self.root], env={'CRI_MAX_OUTPUT_BYTES': '1000'})
        self.assertIn('(compact output:', self.refs_text())
        # 0 なら全文のまま(コメント内の一致も 1 件ずつ出る)
        self.restart(['--root', self.root, '--max-output-bytes', '0'], env={'CRI_MAX_OUTPUT_BYTES': '1000'})
        text = self.refs_text()
        self.assertNotIn('(compact output:', text)
        self.assertIn('src/a.cpp:43', text)
        self.assertGreater(len(text.encode('utf-8')), 1000)

    def test_root_is_found_from_a_subfolder(self):
        # --root を省き、ルートより下のフォルダで起動しても、親の .index を引く
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.start([], cwd=os.path.join(self.root, 'src'))
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run'}})['result']
        self.assertFalse(r['isError'], r['content'][0]['text'])
        text = r['content'][0]['text']
        first = text.splitlines()[0]
        self.assertIn('the index is in this parent folder', first)
        self.assertEqual(os.path.normcase(first.split(' (')[0][len('root: '):]), os.path.normcase(self.root))
        self.assertIn('call   src/a.cpp:3  [in Main]', text)  # パスはルートからの相対のまま


class ReuseTest(unittest.TestCase):
    """同じプロセスの中で、開いた索引を呼び出しの間で使い回すか(索引が変わったら開き直すか)。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(SERVER)))
        import server  # noqa: E402  (ltq と autoupdate も入る)
        self.server = server
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, 'src'))
        os.makedirs(os.path.join(self.root, '.index'))
        self.src = os.path.join(self.root, 'src', 'a.cpp')
        self.base = os.path.join(self.root, '.index', 'index.tsv')
        with open(self.src, 'w', encoding='utf-8', newline='\n') as f:
            f.write('void Run();\nvoid Main() {\n  Run();\n  // Run\n}\n')
        os.utime(self.src, (1000, 1000))
        with open(self.base, 'w', encoding='utf-8', newline='\n') as f:
            f.write('Run\tdecl\t\tsrc/a.cpp:1:6\tvoid Run();\nRun\tcall\tMain\tsrc/a.cpp:3:3\tRun();\n')
        with open(self.base + '.skip.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('path\tkind\tbegin\tend\nsrc/a.cpp\tcomment\t4:3\t4:9\n')
        with open(self.base + '.tu.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write(f'file\tok\terrors\tsec\trefs\tfirst_error\n{self.src}\t1\t0\t0.1\t2\t\n')
        with open(self.base + '.deps.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write(f'tu\tfile\n{self.src}\tsrc/a.cpp\n')
        for p in ('', '.skip.tsv', '.tu.tsv', '.deps.tsv'):
            os.utime(self.base + p, (2000, 2000))
        self.cfg = server.Config(self.root, None, None)  # 自動更新はしない

    def tearDown(self):
        shutil.rmtree(self.root)

    def refs(self):
        code, text = self.server.call_tool(self.cfg, 'refs', {'name': 'Run'})
        self.assertEqual(code, 0, text)
        return text

    def test_index_is_reused_while_unchanged(self):
        self.refs()
        first = self.cfg.opened[2]
        # ソースを変えても(索引はそのまま)、同じ Index を使い、ソースの状態は読み直す
        with open(self.src, 'w', encoding='utf-8', newline='\n') as f:
            f.write('void Run();\nvoid Main() {\n  Run();\n  // Run\n  Run();\n}\n')
        text = self.refs()
        self.assertIs(self.cfg.opened[2], first)
        self.assertIn('1 source files changed after indexing', text)
        self.assertIn('src/a.cpp:5', text)  # 索引の後に足した行がテキスト一致に出る
        # 索引が変われば開き直す
        with open(self.base, 'a', encoding='utf-8', newline='\n') as f:
            f.write('Run\tcall\tMain\tsrc/a.cpp:5:3\tRun();\n')
        os.utime(self.base, (3000, 3000))
        text = self.refs()
        self.assertIsNot(self.cfg.opened[2], first)
        self.assertIn('== calls (resolved by clang): 2', text)

    def test_seen_cache_is_written_and_follows_deps(self):
        self.refs()
        cache = self.base + '.seen.cache'
        with open(cache, 'rb') as f:
            self.assertTrue(f.read().startswith(b'cxxref-seen 1 '))
        # .deps.tsv が変われば一覧を作り直す: 索引より新しいヘッダーが「変わったファイル」になる
        h = os.path.join(self.root, 'src', 'a.h')
        with open(h, 'w', encoding='utf-8') as f:
            f.write('#pragma once\n')
        with open(self.base + '.deps.tsv', 'a', encoding='utf-8', newline='\n') as f:
            f.write(f'{self.src}\tsrc/a.h\n')
        os.utime(self.base + '.deps.tsv', (2000, 2000))  # 大きさだけが変わる
        text = self.refs()
        self.assertIn('    src/a.h\n', text)
        self.assertNotIn('not read by any indexed configuration', text)


if __name__ == '__main__':
    unittest.main()
