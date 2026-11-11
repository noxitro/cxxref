"""MCP サーバーの動作テスト: server.py を子プロセスで起動し、stdio の JSON-RPC で一通り呼ぶ。

usage: python -I -m unittest discover -s mcp/test   (リポジトリのルートで)
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'server.py')
# 最後の行(coverage)の、探した範囲の書き方
SCOPE = ' (searched: the root, except folders that start with ".")'


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
        # 切っても、名前を含む行を全部並べた一覧のファイルの場所は残る
        path = re.search(r'Every line, one per line: "([^"]+)"', text).group(1)
        with open(path, encoding='utf-8') as f:
            self.assertIn('== coverage: all 3 lines that contain the word "Run" are listed above', f.read())

    def test_complete_list_is_written_when_lines_are_only_counted(self):
        # 結果に 1 行ずつ並べきれないとき(上限に収めるために件数にまとめたとき)は、名前を含む行を全部並べた一覧を
        # 索引の隣に書き、結果の最後の行でその場所を知らせる(名前を変えるときに、直す先を漏らさない)
        refs = os.path.join(self.root, '.index', 'refs')
        text = self.refs_text()
        self.assertTrue(text.endswith('== coverage: all 3 lines that contain the word "Run" are listed above, one per line' + SCOPE + '\n'))
        self.assertFalse(os.path.exists(refs))  # 全部並んでいれば書かない
        self.many_comments()
        self.restart(['--root', self.root, '--max-output-bytes', '1400'])
        text = self.refs_text()
        self.assertLessEqual(len(text.encode('utf-8')), 1400)
        m = re.search(r'\n== coverage: 2 of 42 lines that contain the word "Run" are listed above, one per line'
                      + re.escape(SCOPE) + r'; the rest are'
                      r' only counted\. Every line, one per line: "([^"]+)" \(read it before a rename or a signature'
                      r' change\)\n$', text)
        self.assertTrue(m, text[-400:])
        self.assertEqual(os.path.dirname(m.group(1)), refs)
        with open(m.group(1), encoding='utf-8') as f:
            listing = f.read()
        for n in range(4, 44):
            self.assertIn(f'    src/a.cpp:{n}  | // Run\n', listing)
        self.assertIn('== coverage: all 42 lines that contain the word "Run" are listed above, one per line' + SCOPE + '\n', listing)
        # 同じ問い合わせは同じファイルを書き換える(limit が違っても同じ一覧)。古い一覧は消す
        old = os.path.join(refs, 'Old-0123abcd.txt')
        with open(old, 'w') as f:
            f.write('x')
        os.utime(old, (1000, 1000))
        keep = os.path.join(refs, 'notes.txt')  # 一覧の名前でないファイルには触らない
        with open(keep, 'w') as f:
            f.write('x')
        os.utime(keep, (1000, 1000))
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run', 'limit': 5}})['result']
        self.assertIn(f'"{m.group(1)}"', r['content'][0]['text'])
        self.assertEqual(sorted(os.listdir(refs)), sorted([os.path.basename(m.group(1)), 'notes.txt']))
        # path を付けると、その先だけの一覧(別のファイル)
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run', 'path': ['src']}})['result']
        other = re.search(r'Every line, one per line: "([^"]+)"', r['content'][0]['text']).group(1)
        self.assertNotEqual(other, m.group(1))
        # 中身が同じなら書き直さない(ファイルを差し替えない)
        ino = os.stat(m.group(1)).st_ino
        self.refs_text()
        self.assertEqual(os.stat(m.group(1)).st_ino, ino)

    def test_complete_list_goes_where_the_agent_can_read_it(self):
        # 索引がルートの隠しフォルダに無いとき(作業ツリーの検索に掛かるフォルダや、ルートの外)は、<ルート>/.cxxref/refs に書く:
        # エージェントが読める作業ツリーの中で、テキスト検索には掛からない所。git には無視させる
        self.many_comments()
        self.restart(['--root', self.root, '--max-output-bytes', '1400'])
        first = re.search(r'Every line, one per line: "([^"]+)"', self.refs_text()).group(1)
        out = os.path.join(self.root, 'out')
        shutil.copytree(os.path.join(self.root, '.index'), out, ignore=shutil.ignore_patterns('refs'))
        args = ['--root', self.root, '--index', os.path.join(out, 'index.tsv'), '--max-output-bytes', '1600']
        self.restart(args)
        text = self.refs_text()
        where = re.search(r'Every line, one per line: "([^"]+)"', text).group(1)
        home = os.path.join(self.root, '.cxxref')
        self.assertEqual(os.path.dirname(where), os.path.join(home, 'refs'))
        self.assertNotIn('outside the workspace', text)
        with open(os.path.join(home, '.gitignore'), encoding='utf-8') as f:
            self.assertEqual(f.read(), '*\n')
        self.assertFalse(os.path.exists(os.path.join(out, 'refs')))
        self.assertNotEqual(os.path.basename(where), os.path.basename(first))  # 索引が違えば、別の一覧
        with open(where, encoding='utf-8') as f:  # 索引のファイル自体の行は、一覧に入らない
            self.assertIn('== coverage: all 42 lines', f.read())
        # 作業ツリーに書けないときだけ、一時フォルダに書く(作業ツリーの外なので、読めないときの手を添える)
        shutil.rmtree(home)
        with open(home, 'w') as f:
            f.write('x')  # 同じ名前のファイルがあって、フォルダを作れない
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.restart(args, env={'TMPDIR': tmp, 'TEMP': tmp, 'TMP': tmp})
        text = self.refs_text()
        where = re.search(r'Every line, one per line: "([^"]+)"', text).group(1)
        self.assertEqual(os.path.dirname(where), os.path.join(tmp, 'cxxref-refs'))
        self.assertIn('; it is outside the workspace: if you cannot read it, pass path to list one folder at a time)\n', text)

    def test_complete_lists_are_capped_in_number(self):
        # 一覧の数に上限がある(新しい順に残し、古いものから消す)
        sys.path.insert(0, os.path.dirname(SERVER))
        import server
        index = os.path.join(self.root, '.index', 'index.tsv')
        refs = os.path.join(self.root, '.index', 'refs')
        cap, server.LIST_MAX = server.LIST_MAX, 2
        try:
            made = []
            for i, name in enumerate(('A', 'B', 'C')):
                where, err = server.write_list([index], self.root, ['refs', name], f'list of {name}\n')
                self.assertIsNone(err)
                os.utime(where, (time.time() - 100 + i, time.time() - 100 + i))
                made.append(os.path.basename(where))
            where, err = server.write_list([index], self.root, ['refs', 'D'], 'list of D\n')
        finally:
            server.LIST_MAX = cap
        self.assertEqual(sorted(os.listdir(refs)), sorted([made[2], os.path.basename(where)]))

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
        self.restart(['--root', self.root, '--max-output-bytes', '1400'])
        text = self.refs_text()
        self.assertLessEqual(len(text.encode('utf-8')), 1400)
        self.assertIn('(compact output:', text)
        self.assertIn('call   src/a.cpp:3  [in Main]  | Run();', text)
        self.assertIn('inside comments -> not code; do not report these: 40', text)
        self.assertNotIn('output cut', text)

    def many_inactive(self):
        """無効な #if の中の一致が 25 行ある索引にする(1 行 100 バイトほど)。src/a.cpp の 4〜30 行目が無効。"""
        with open(os.path.join(self.root, 'src', 'a.cpp'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('void Run();\nvoid Main() {\n  Run();\n#if 0\n'
                    + ''.join(f'  Run();  /* {"x" * 80} {i} */\n' for i in range(25)) + '#endif\n}\n')
        with open(os.path.join(self.root, '.index', 'index.tsv.skip.tsv'), 'w', encoding='utf-8', newline='\n') as f:
            f.write('path\tkind\tbegin\tend\nsrc/a.cpp\tinactive\t4:1\t30:7\n')
        for p in ('index.tsv', 'index.tsv.skip.tsv', 'index.tsv.tu.tsv'):
            os.utime(os.path.join(self.root, '.index', p))

    def test_inactive_lines_are_folded_only_when_the_listing_does_not_fit(self):
        # 短い形の 1 段目は無効な #if の中の一致を 1 行ずつ出す。それで上限を超えるときだけ、2 段目(フォルダごとの件数)にする
        self.many_inactive()
        self.restart(['--root', self.root, '--max-output-bytes', '6000'])
        text = self.refs_text()
        self.assertIn('    src/a.cpp:29  [#if 0]  |', text)
        self.assertNotIn('output cut', text)
        self.restart(['--root', self.root, '--max-output-bytes', '2000'])
        text = self.refs_text()
        self.assertLessEqual(len(text.encode('utf-8')), 2000)
        self.assertIn('inactive #if, macro bodies and matches that resolve to other symbols are summarized', text)
        self.assertRegex(text, r'inside inactive #if/#ifdef branches[^\n]*: 25\n[^\n]*\n    src/  25 matches in 1 files')
        self.assertIn('ask again with --path <folder>', text)
        self.assertNotIn('output cut', text)

    def test_many_resolved_lines_are_cut_by_section_not_at_the_end(self):
        # 解決済みの行だけで上限を超える名前は、節ごとの行数を減らして返す。省いた行はフォルダごとの件数で残り、
        # 後ろの節(索引に無い一致)が末尾ごと切れて消えることがない
        os.makedirs(os.path.join(self.root, 'lib'))
        rows = ['Run\tdecl\t\tsrc/a.cpp:1:6\tvoid Run();\n']
        for rel in ('src/a.cpp', 'lib/b.cpp'):
            with open(os.path.join(self.root, rel), 'w', encoding='utf-8', newline='\n') as f:
                f.write('void Run();\nvoid Main() {\n'
                        + ''.join(f'  Run();  /* {"x" * 60} {i} */\n' for i in range(60)) + '  // Run\n}\n')
            rows += [f'Run\tcall\tMain\t{rel}:{3 + i}:3\tRun();  /* {"x" * 60} {i} */\n' for i in range(60)]
        base = os.path.join(self.root, '.index', 'index.tsv')
        with open(base, 'w', encoding='utf-8', newline='\n') as f:
            f.write(''.join(rows))
        with open(base + '.skip.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('path\tkind\tbegin\tend\nsrc/a.cpp\tcomment\t63:3\t63:9\nlib/b.cpp\tcomment\t63:3\t63:9\n')
        with open(base + '.tu.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('file\tok\terrors\tsec\trefs\tfirst_error\n'
                    + ''.join(f'{os.path.join(self.root, *rel.split("/"))}\t1\t0\t0.1\t2\t\n'
                              for rel in ('src/a.cpp', 'lib/b.cpp')))
        self.restart(['--root', self.root, '--max-output-bytes', '6000'])
        text = self.refs_text()
        self.assertLessEqual(len(text.encode('utf-8')), 6000)
        self.assertNotIn('output cut', text)
        self.assertIn('== calls (resolved by clang): 120\n', text)
        self.assertEqual(len(re.findall(r'^  call   ', text, re.M)), 25)
        self.assertIn('  ... 95 more (raise --limit, or ask with --path <folder>), by folder:\n', text)
        self.assertEqual(sum(int(n) for n in re.findall(r'^  \S+/  (\d+) matches in', text, re.M)), 95)
        self.assertIn('inside comments -> not code; do not report these: 2\n', text)  # 後ろの節も残る

    def test_path_argument(self):
        # path で、並べる行を指定のフォルダ・ファイルに絞る(フォルダごとの件数にまとめられた行を出し直す)
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run', 'path': ['src/a.cpp']}})['result']
        self.assertIn('(--path: only lines in src/a.cpp are listed and counted)', r['content'][0]['text'])
        self.assertIn('call   src/a.cpp:3  [in Main]', r['content'][0]['text'])
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run', 'path': 'elsewhere'}})['result']
        self.assertIn('== calls (resolved by clang): 0', r['content'][0]['text'])
        self.assertIn('symbol: Run\n', r['content'][0]['text'])  # 記号は索引の全体から決まる

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
        self.cfg.close()  # 監視しているフォルダと目次の写しを手放す(開いたままでは消せない)
        shutil.rmtree(self.root)

    def refs(self):
        code, text = self.server.call_tool(self.cfg, 'refs', {'name': 'Run'})
        self.assertEqual(code, 0, text)
        return text

    def test_old_downloaded_ltidx_is_told_once(self):
        # 取ってきた ltidx がプラグインより古ければ、最初の結果に 1 回だけ /cxxref-index で取り直すよう添える
        from unittest import mock
        with mock.patch.object(self.server.autoupdate, 'stale_ltidx', return_value=('0.5.1', '0.6.0')):
            first, second = self.refs(), self.refs()
        self.assertIn('ltidx 0.5.1 is older than this plugin (0.6.0)', first)
        self.assertIn('/cxxref-index', first)
        self.assertNotIn('is older than this plugin', second)

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
        self.assertIn('1 source files changed after indexing', text)
        self.assertNotIn('not read by any indexed configuration', text)

    def test_large_other_files_are_searched_in_worker_processes(self):
        """C/C++ 以外のファイルで読む量が多いときは、プロセスを分けて探す。結果はスレッドで探すときと一字一句同じ。
        プロセスを起こせないときは、スレッドで読み直す。"""
        for i in range(12):
            with open(os.path.join(self.root, 'src', f'n{i}.txt'), 'w', encoding='utf-8', newline='\n') as f:
                f.write(f'line\nRun here {i}\nRunner\n' * 3)
        want = self.refs()
        self.assertIn('src/n11.txt:5  | Run here 11', want)
        ltq = sys.modules['ltq']
        real, made = ltq.ProcessPoolExecutor, []

        def pool(*a, **k):
            made.append(k)
            return real(*a, **k)

        def broken(*a, **k):
            made.append(None)
            raise OSError('no processes')

        size, ltq.PROC_BYTES = ltq.PROC_BYTES, 1
        try:
            ltq.ProcessPoolExecutor = pool
            self.assertEqual(self.refs(), want)
            self.assertEqual(len(made), 1)
            ltq.ProcessPoolExecutor = broken
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(self.refs(), want)
            self.assertEqual(len(made), 2)
            self.assertIn('worker processes unavailable', err.getvalue())
        finally:
            ltq.PROC_BYTES, ltq.ProcessPoolExecutor = size, real

    def test_resident_parts_give_the_same_answers(self):
        """保つ部品(ソースの一覧の監視・単語のフィルタ・名前の目次)があっても無くても、結果は一字一句同じ。
        編集・新しいファイル・消したファイル・索引の作り直しのたびに比べる。"""
        plain = self.server.Config(self.root, None, None, resident=False)
        self.addCleanup(plain.close)
        b = os.path.join(self.root, 'src', 'b.cpp')

        def same(*names):
            for name in names:
                for args in ({'name': name}, ):
                    want = self.server.call_tool(plain, 'refs', args)
                    got = self.server.call_tool(self.cfg, 'refs', args)
                    self.assertEqual(got, want, name)
            want = self.server.call_tool(plain, 'check', {'locations': ['src/a.cpp:3', 'src/a.cpp:4']})
            self.assertEqual(self.server.call_tool(self.cfg, 'check', {'locations': ['src/a.cpp:3', 'src/a.cpp:4']}), want)

        same('Run', 'Main', 'Nothing')
        self.assertTrue(self.cfg._trees)
        if os.name == 'nt':  # 変更の知らせは Windows だけ。ほかでは呼ぶたびに走査する(結果の比較はどちらでも行う)
            self.assertTrue(next(iter(self.cfg._trees.values()))[0].watching)
        with open(self.src, 'w', encoding='utf-8', newline='\n') as f:  # 索引の後の編集(行が動く)
            f.write('// top\nvoid Run();\nvoid Main() {\n  Run();\n  // Run\n  Run(); Main();\n}\n')
        same('Run', 'Main')
        with open(b, 'w', encoding='utf-8', newline='\n') as f:  # 新しいファイル
            f.write('void Other() { Run(); }\n')
        same('Run', 'Other')
        os.remove(b)  # 消す
        same('Run', 'Other')
        with open(self.base, 'a', encoding='utf-8', newline='\n') as f:  # 索引の作り直し(目次も作り直す)
            f.write('Main\tcall\tMain\tsrc/a.cpp:6:10\tRun(); Main();\n')
        os.utime(self.base, (3000, 3000))
        same('Run', 'Main')
        tree = next(iter(self.cfg._trees.values()))[0]
        if os.name == 'nt':
            self.assertEqual(tree.rescans, 1)  # 最初の 1 回のほかは、知らせだけで追った


if __name__ == '__main__':
    unittest.main()
