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

    def test_large_check_result_is_cut(self):
        # refs 以外(短い形が無い)も、上限を超えたら末尾を切る(場所を多く渡した check)
        self.restart(['--root', self.root, '--max-output-bytes', '400'])
        locs = ['src/a.cpp:3', 'src/a.cpp:4'] * 20
        text = self.send('tools/call', {'name': 'check', 'arguments': {'locations': locs}})['result']['content'][0]['text']
        self.assertLessEqual(len(text.encode('utf-8')), 400)
        self.assertIn('src/a.cpp:3', text)
        self.assertTrue(text.endswith('... output cut at 400 bytes (the client limit). '
                                      'Ask for a narrower name, or pass path / exclude / limit to see the rest.\n'))
        self.assertEqual(os.listdir(os.path.join(self.root, '.index')).count('refs'), 0)  # 一覧のファイルは書かない

    def test_complete_list_is_written_when_lines_are_only_counted(self):
        # 結果に 1 行ずつ並べきれないとき(上限に収めるために件数にまとめたとき)は、並べなかった行の一覧を
        # 索引の隣に書き、結果の最後の行でその場所を知らせる(名前を変えるときに、直す先を漏らさない)。
        # 結果と一覧を合わせると、名前を含む行がどれも 1 回ずつ出る(一覧は、1 回で読める大きさに収める)
        refs = os.path.join(self.root, '.index', 'refs')
        text = self.refs_text()
        self.assertTrue(text.endswith('== coverage: all 3 lines that contain the word "Run" are listed above, one per line' + SCOPE + '\n'))
        self.assertFalse(os.path.exists(refs))  # 全部並んでいれば書かない
        self.many_comments()
        self.restart(['--root', self.root, '--max-output-bytes', '1400'])
        text = self.refs_text()
        self.assertLessEqual(len(text.encode('utf-8')), 1400)
        m = re.search(r'\n== coverage: 2 of 42 lines that contain the word "Run" are listed above, one per line'
                      + re.escape(SCOPE) + r'; the other 40 lines are listed, one per line, in "([^"]+)" \(1 KB: read it'
                      r' whole in one call\)\. Before a rename or a signature change, read it instead of searching the'
                      r' sources again\n$', text)
        self.assertTrue(m, text[-400:])
        self.assertEqual(os.path.dirname(m.group(1)), refs)
        with open(m.group(1), encoding='utf-8') as f:
            listing = f.read()
        for n in range(4, 44):
            self.assertIn(f'    src/a.cpp:{n}  | // Run\n', listing)
        for n in (1, 3):  # 結果に並べた行は、一覧に入れない
            self.assertNotIn(f'src/a.cpp:{n} ', listing)
            self.assertIn(f'src/a.cpp:{n} ', text)
        self.assertTrue(listing.startswith('query: Run\n  (the lines that contain the name and are not listed in the refs result,'
                                           ' one per line; with that result, every line is listed once)\n'), listing[:200])
        self.assertTrue(listing.endswith('\n== coverage: 40 lines here + 2 in the refs result = all 42 lines that contain the word'
                                         ' "Run", each listed once' + SCOPE + '\n'), listing[-200:])
        # 同じ結果は同じファイルを書き換える(limit が違っても、並べた行が同じなら同じ一覧)。古い一覧は消す
        old = os.path.join(refs, 'Old-0123abcd.txt')
        with open(old, 'w') as f:
            f.write('x')
        os.utime(old, (1000, 1000))
        keep = os.path.join(refs, 'notes.txt')  # 一覧の名前でないファイルには触らない
        with open(keep, 'w') as f:
            f.write('x')
        os.utime(keep, (1000, 1000))
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run', 'limit': 100}})['result']
        self.assertIn(f'"{m.group(1)}"', r['content'][0]['text'])
        self.assertEqual(sorted(os.listdir(refs)), sorted([os.path.basename(m.group(1)), 'notes.txt']))
        # path を付けると、その先だけの一覧(別のファイル)
        r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': 'Run', 'path': ['src']}})['result']
        other = re.search(r'listed, one per line, in "([^"]+)"', r['content'][0]['text']).group(1)
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
        first = re.search(r'listed, one per line, in "([^"]+)"', self.refs_text()).group(1)
        out = os.path.join(self.root, 'out')
        shutil.copytree(os.path.join(self.root, '.index'), out, ignore=shutil.ignore_patterns('refs'))
        args = ['--root', self.root, '--index', os.path.join(out, 'index.tsv'), '--max-output-bytes', '1600']
        self.restart(args)
        text = self.refs_text()
        where = re.search(r'listed, one per line, in "([^"]+)"', text).group(1)
        home = os.path.join(self.root, '.cxxref')
        self.assertEqual(os.path.dirname(where), os.path.join(home, 'refs'))
        self.assertNotIn('outside the workspace', text)
        with open(os.path.join(home, '.gitignore'), encoding='utf-8') as f:
            self.assertEqual(f.read(), '*\n')
        self.assertFalse(os.path.exists(os.path.join(out, 'refs')))
        self.assertNotEqual(os.path.basename(where), os.path.basename(first))  # 索引が違えば、別の一覧
        with open(where, encoding='utf-8') as f:  # 索引のファイル自体の行は、一覧に入らない
            self.assertIn(' = all 42 lines', f.read())
        # 作業ツリーに書けないときだけ、一時フォルダに書く(作業ツリーの外なので、読めないときの手を添える)
        shutil.rmtree(home)
        with open(home, 'w') as f:
            f.write('x')  # 同じ名前のファイルがあって、フォルダを作れない
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.restart(args, env={'TMPDIR': tmp, 'TEMP': tmp, 'TMP': tmp})
        text = self.refs_text()
        where = re.search(r'listed, one per line, in "([^"]+)"', text).group(1)
        self.assertEqual(os.path.dirname(where), os.path.join(tmp, 'cxxref-refs'))
        self.assertTrue(text.endswith(' (outside the workspace: if you cannot read it, pass path to list one folder at a time)\n'),
                        text[-300:])

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
        # 名前の最後の部分が空のときも、すぐに誤りを返す(探す単語が無い。前は検索が終わらなかった)
        for name in ('Foo::', '::'):
            r = self.send('tools/call', {'name': 'refs', 'arguments': {'name': name}})
            self.assertTrue(r['result']['isError'])
            self.assertIn('needs a name after the last "::"', r['result']['content'][0]['text'])
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

    def test_complete_list_has_no_server_note(self):
        # 一覧のファイルには、サーバーの注記(最初の 1 回だけ付くものがある)を入れない。同じ問い合わせなら中身が同じで、
        # ファイルを書き直さない
        from unittest import mock
        with open(self.src, 'w', encoding='utf-8', newline='\n') as f:
            f.write('void Run();\nvoid Main() {\n  Run();\n' + '  // Run\n' * 40 + '}\n')
        os.utime(self.src, (1000, 1000))
        with open(self.base + '.skip.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('path\tkind\tbegin\tend\n' + ''.join(f'src/a.cpp\tcomment\t{4 + i}:3\t{4 + i}:9\n' for i in range(40)))
        os.utime(self.base + '.skip.tsv', (2000, 2000))
        self.cfg.close()
        self.cfg = self.server.Config(self.root, None, None, max_bytes=1600)
        with mock.patch.object(self.server.autoupdate, 'stale_ltidx', return_value=('0.5.1', '0.6.0')):
            first = self.refs()
            path = self.cfg.last_list
            ino = os.stat(path).st_ino
            second = self.refs()
        self.assertIn('is older than this plugin', first)
        self.assertNotIn('is older than this plugin', second)
        self.assertEqual(self.cfg.last_list, path)
        with open(path, encoding='utf-8') as f:
            listing = f.read()
        self.assertTrue(listing.startswith('query: Run\n'), listing[:200])  # 索引の状態(index: の行)は結果にある
        self.assertNotIn('is older than this plugin', listing)
        self.assertIn(' = all 42 lines', listing)
        self.assertEqual(os.stat(path).st_ino, ino)

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
        プロセスを起こせない・子が落ちたときは、スレッドで読み直す。子の中の検索が上げた例外は、読み直さずにそのまま上げる。"""
        from concurrent.futures.process import BrokenProcessPool
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

        class Failing:  # map が例外を上げる(子が落ちた・子の中の検索が例外を上げた)
            def __init__(self, error):
                self.error = error

            def __call__(self, *a, **k):
                made.append(None)
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def map(self, fn, it):
                raise self.error

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
            ltq.ProcessPoolExecutor = Failing(BrokenProcessPool('a child died'))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(self.refs(), want)
            self.assertIn('worker processes unavailable (BrokenProcessPool', err.getvalue())
            ltq.ProcessPoolExecutor = Failing(MemoryError('in the child'))
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertRaises(MemoryError):
                self.refs()
            self.assertNotIn('worker processes unavailable', err.getvalue())
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


def nbytes(text):
    return len(text.encode('utf-8'))


class ListFilesTest(unittest.TestCase):
    """並べきれなかった行の一覧: 1 回で読める大きさのファイルに分け、結果と合わせると名前を含む行が全部出るか。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(SERVER)))
        import server  # noqa: E402
        self.server = server
        self.root = tempfile.mkdtemp()
        self.cfg = None

    def tearDown(self):
        if self.cfg:
            self.cfg.close()
        shutil.rmtree(self.root)

    def family(self, n=30, far=20, comments=4):
        """Base::Tick と、それを上書きする D00..::Tick(宣言は src/d.h、定義と Base::Tick の呼び出しは src/d.cpp)。
        実体化で別の記号に解決されたテンプレートの中の呼び出し(src/t.h)が far 行、コメントの中の一致(src/c.cpp)が comments 行。
        名前を含む行は 1 + n(d.h)+ n + 1(d.cpp)+ far + comments。"""
        src = os.path.join(self.root, 'src')
        os.makedirs(src)
        os.makedirs(os.path.join(self.root, '.index'))
        base = os.path.join(self.root, '.index', 'index.tsv')
        files = {
            'd.h': 'struct Base { virtual void Tick(int); };\n'
                   + ''.join(f'struct D{i:02} : Base {{ void Tick(int) override; }};\n' for i in range(n)),
            'd.cpp': ''.join(f'void D{i:02}::Tick(int v) {{ Base::Tick(v); }}\n' for i in range(n))
                     + 'void All(D00* d) { d->Tick(1); }\n',
            't.h': 'template <class T> void Each(T& t) {\n' + '  t.Tick(0);\n' * far + '}\n',
            'c.cpp': '// Tick in a comment\n' * comments,
        }
        for name, text in files.items():
            with open(os.path.join(src, name), 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
            os.utime(os.path.join(src, name), (1000, 1000))
        rows = [('Base::Tick', 'decl', '', 'src/d.h:1:28', 'struct Base { virtual void Tick(int); };')]
        for i in range(n):
            decl = f'struct D{i:02} : Base {{ void Tick(int) override; }};'
            body = f'void D{i:02}::Tick(int v) {{ Base::Tick(v); }}'
            rows += [(f'D{i:02}::Tick', 'decl', '', f'src/d.h:{i + 2}:26', decl),
                     (f'D{i:02}::Tick', 'override', 'Base::Tick', f'src/d.h:{i + 2}:26', decl),
                     (f'D{i:02}::Tick', 'def', '', f'src/d.cpp:{i + 1}:11', body),
                     ('Base::Tick', 'call', f'D{i:02}::Tick', f'src/d.cpp:{i + 1}:31', body)]
        rows += [('D00::Tick', 'call', 'All', f'src/d.cpp:{n + 1}:23', 'void All(D00* d) { d->Tick(1); }')]
        for k in range(far):
            rows += [('?::Tick', 'call-dep', 'Each', f'src/t.h:{k + 2}:5', 't.Tick(0);'),
                     ('Other::Tick', 'call', 'Each', f'src/t.h:{k + 2}:5', 't.Tick(0);')]
        with open(base, 'w', encoding='utf-8', newline='\n') as f:
            f.write(''.join('\t'.join(r) + '\n' for r in rows))
        with open(base + '.skip.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('path\tkind\tbegin\tend\n'
                    + ''.join(f'src/c.cpp\tcomment\t{k}:1\t{k}:21\n' for k in range(1, comments + 1)))
        with open(base + '.tu.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('file\tok\terrors\tsec\trefs\tfirst_error\n'
                    + ''.join(f'{os.path.join(src, t)}\t1\t0\t0.1\t1\t\n' for t in ('d.cpp', 'c.cpp')))
        for p in ('', '.skip.tsv', '.tu.tsv'):
            os.utime(base + p, (2000, 2000))

    def refs(self, max_bytes, part_bytes, **args):
        if self.cfg:
            self.cfg.close()
        self.cfg = self.server.Config(self.root, None, None, max_bytes=max_bytes, part_bytes=part_bytes)
        code, text = self.server.call_tool(self.cfg, 'refs', dict(name='Base::Tick', **args))
        self.assertEqual(code, 0, text)
        self.assertLessEqual(nbytes(text), max_bytes)
        return text

    def read(self, path):
        with open(path, encoding='utf-8', newline='') as f:
            return f.read()

    def positions(self, text):
        """文面に 1 行ずつ出ている位置(パス:行)。上書きの行は宣言と定義の 2 つ。"""
        return set(re.findall(r'(?:^ +(?:\w[\w-]* +)?| (?:decl|def) )(src/[\w./]+:\d+)', text, re.M))

    def test_rest_is_split_into_files_read_in_one_call(self):
        # 大きな階層の virtual: 結果には直接の呼び出しを全部と上書きを 10 個、残りの上書きは 1 回で読める大きさのファイルに。
        # 名前を含むだけの行(コメント、別の記号に解決された行)は、直す先の一覧に混ぜず、別のファイルに
        self.family()
        text = self.refs(7000, 1500)
        # 上書きの族を絞る形を、節ごとの行数を減らす形より先に試す(直接の呼び出しを削らない)
        self.assertIn('== calls (resolved by clang): 30\n', text)
        for i in range(30):
            self.assertIn(f'  call   src/d.cpp:{i + 1}  [in D{i:02}::Tick]', text)
        self.assertIn('    D09::Tick  decl src/d.h:11  def src/d.cpp:10\n    ... 20 more overrides', text)
        files = self.cfg.last_lists
        self.assertEqual([os.path.basename(p).split('.', 1)[1] for p in files], ['1.txt', '2.txt', 'more.txt'])
        self.assertEqual(self.cfg.last_list, files[0])
        m = re.search(r'\n== coverage: 42 of 86 lines that contain the word "Tick" are listed above, one per line'
                      + re.escape(SCOPE) + r'; the other 44 lines are listed, one per line, in 2 files to read whole, one call'
                      r' each, in parallel: "([^"]+)", "([^"]+)", and the lines the index does not resolve to the query \(text'
                      r' matches, other symbols with the same name\) in "([^"]+)" \(2 KB: too large to read whole; search it'
                      r' with grep, or pass path to list one folder\)\. Before a rename or a signature change, read the first 2'
                      r' instead of searching the sources again\n$', text)
        self.assertTrue(m, text[-900:])
        self.assertEqual(list(m.groups()), files)
        texts = [self.read(p) for p in files]
        for k, t in enumerate(texts):
            self.assertTrue(t.startswith(f'file {k + 1} of 3 of this list (the refs result names all of them)\n'), t[:100])
        for t in texts[:2]:
            self.assertLessEqual(nbytes(t), 1500)
        self.assertIn('    D10::Tick  decl src/d.h:12  def src/d.cpp:11\n', texts[0])
        self.assertIn('    D29::Tick  decl src/d.h:31  def src/d.cpp:30\n', texts[0])
        self.assertNotIn('D09::Tick', ''.join(texts))  # 結果に並べた上書きは、一覧に入れない
        for word in ('src/t.h', 'src/c.cpp'):
            self.assertNotIn(word, texts[0] + texts[1])
        self.assertIn('  src/t.h:21  ?::Tick, Other::Tick  | t.Tick(0);\n', texts[2])
        self.assertIn('    src/c.cpp:4  | // Tick in a comment\n', texts[2])
        self.assertTrue(texts[2].endswith('\n== coverage: 44 lines here + 42 in the refs result = all 86 lines that contain the'
                                          ' word "Tick", each listed once' + SCOPE + '\n'), texts[2][-200:])
        # 結果と一覧を合わせると、名前を含む行が全部
        self.assertEqual(len(self.positions(text)), 42)
        self.assertEqual(len(self.positions(text + ''.join(texts))), 86)

        # 結果の形が違えば(並べた行が違えば)、同じ名前でも別のファイル。先に知らせた一覧は書き換えない
        # (同じサーバーを使う別のエージェントが読んでいる途中でも、結果と一覧が食い違わない)
        self.refs(5500, 1500)
        files2 = self.cfg.last_lists
        self.assertTrue(files2)
        self.assertFalse(set(files) & set(files2))
        self.assertEqual([self.read(p) for p in files], texts)
        # 同じ結果なら同じファイル(書き直さない)。同じ一覧の、前の分け方のファイルは消す
        stale = files[0][:-len('1.txt')] + '9.txt'
        with open(stale, 'w') as f:
            f.write('x')
        ino = [os.stat(p).st_ino for p in files]
        self.assertEqual(self.refs(7000, 1500), text)
        self.assertEqual(self.cfg.last_lists, files)
        self.assertEqual([os.stat(p).st_ino for p in files], ino)
        self.assertFalse(os.path.exists(stale))
        for p in files2:
            self.assertTrue(os.path.exists(p))

    def test_rest_that_fits_is_one_file(self):
        # 残りが 1 回で読める大きさなら、1 つのファイル(名前を含むだけの行も一緒)
        self.family(n=12, far=3)
        text = self.refs(3700, 18000)
        files = self.cfg.last_lists
        self.assertEqual(len(files), 1)
        self.assertRegex(os.path.basename(files[0]), r'^Base_Tick-[0-9a-f]{8}[.]txt$')
        self.assertTrue(text.endswith(f'in "{files[0]}" (2 KB: read it whole in one call). Before a rename or a signature'
                                      ' change, read it instead of searching the sources again\n'), text[-400:])
        listing = self.read(files[0])
        self.assertTrue(listing.startswith('query: Base::Tick\n'))
        self.assertEqual(len(self.positions(text + listing)), 1 + 12 + 13 + 3 + 4)
        self.assertFalse(self.positions(text) & {f'src/t.h:{k}' for k in range(2, 5)})

    def test_result_cut_names_the_complete_list(self):
        # どの形でも上限を超えるときは末尾を切り、名前を含む行を全部並べた一覧(同じ分け方)の場所を添える
        self.family()
        text = self.refs(1500, 1500)
        files = self.cfg.last_lists
        self.assertEqual([os.path.basename(p).split('.', 1)[1] for p in files], ['1.txt', '2.txt', '3.txt', 'more.txt'])
        self.assertTrue(text.endswith('... output cut at 1500 bytes (the client limit). Every line, one per line: '
                                      + ', '.join(f'"{p}"' for p in files) + ' (4 KB: search it with grep). Ask for a narrower'
                                      ' name, or pass path / exclude / limit to see the rest.\n'), text[-700:])
        self.assertEqual(len(self.positions(''.join(self.read(p) for p in files))), 86)

    def test_plan_list(self):
        s = self.server
        intro = 'query: X\n  (intro)\n\n'
        calls = ('== calls (resolved by clang): 40\n' + ''.join(f'  call   src/a.cpp:{i}  | X();\n' for i in range(40)) + '\n')
        virt = ('== virtual dispatch\n  overridden by (...): 40\n' + ''.join(f'    D{i:02}::X  decl src/d.h:{i}\n' for i in range(40))
                + '  calls resolved to the overrides (...): 2\n    call   src/b.cpp:1  D00::X  | d->X();\n'
                + '    call   src/b.cpp:2  D01::X  | d->X();\n\n')
        word = ('== text matches of "X" not in the index: 3 of 90 word matches\n  inside comments -> not code: 3\n'
                + ''.join(f'    src/c.cpp:{i}  | // X\n' for i in range(3)) + '\n== coverage: 3 lines here\n')
        text = intro + calls + virt + word

        def data(t):  # 位置のある行(見出し・小見出し・空行でない行)
            return [x for x in t.splitlines() if ' src/' in x]

        # 収まれば 1 つ(添え字なし)
        self.assertEqual(s.plan_list(text, 100000), ([('', text, True)], False))
        # 収まらなければ、問い合わせの記号の行を 1 回で読める大きさに分け、名前を含むだけの行は別のファイルに
        plan, word_only = s.plan_list(text, 1200)
        self.assertEqual([sfx for sfx, _, _ in plan], ['.1', '.2', '.3', '.4'])
        self.assertTrue(all(ok and nbytes(t) <= 1200 for _, t, ok in plan), [nbytes(t) for _, t, _ in plan])
        for k, (_, t, _) in enumerate(plan):
            self.assertTrue(t.startswith(f'file {k + 1} of 4 of this list (the refs result names all of them)\n'))
        self.assertTrue(plan[0][1].split('\n', 1)[1].startswith(intro + '== calls (resolved by clang): 40\n  call   src/a.cpp:0 '))
        # 節の途中で分けたら、次のファイルの頭に見出し(小見出しのある節ではその小見出しも)を足す
        self.assertTrue(plan[1][1].split('\n', 1)[1].startswith('== calls (resolved by clang): 40 (continued)\n  call '))
        self.assertTrue(plan[2][1].split('\n', 1)[1].startswith(
            '== virtual dispatch (continued)\n  overridden by (...): 40 (continued)\n    D'), plan[2][1][:200])
        self.assertEqual(plan[3][1].split('\n', 1)[1], intro + word)
        self.assertEqual(data(''.join(t for _, t, _ in plan)), data(text))  # 行は落とさず、重ねず、順も変えない
        # 問い合わせの記号の行が PARTS_MAX 個に収まらなければ、残りを 1 つの大きなファイルに(名前を含むだけの行もそこへ)
        plan, word_only = s.plan_list(text, 600)
        self.assertEqual([(sfx, ok) for sfx, _, ok in plan], [('.1', True), ('.2', True), ('.3', True), ('.more', False)])
        self.assertFalse(word_only)
        self.assertTrue(all(nbytes(t) <= 600 for _, t, ok in plan if ok))
        more = plan[3][1].split('\n', 1)[1]
        self.assertTrue(more.startswith(intro + '== '), more[:100])
        self.assertIn(' (continued)\n', more[len(intro):].split('\n', 1)[0] + '\n')
        self.assertTrue(more.endswith(word))
        self.assertEqual(data(''.join(t for _, t, _ in plan)), data(text))
        # 問い合わせの記号の行が無ければ(全体が、名前を含むだけの行)、1 つの大きなファイル
        self.assertEqual(s.plan_list(intro + word, 100), ([('', intro + word, False)], True))

    def test_list_hint(self):
        # 小さいファイルは「丸ごと読む」、大きいファイルは「検索する」と言い分ける
        hint = self.server.list_hint
        tail = '. Before a rename or a signature change, read {} instead of searching the sources again'
        self.assertEqual(hint([('a', 900, True)], False, 5, False),
                         '; the other 5 lines are listed, one per line, in "a" (1 KB: read it whole in one call)' + tail.format('it'))
        self.assertEqual(hint([('a', 900, True), ('b', 900, True)], True, 5, False),
                         '; the other 5 lines are listed, one per line, in 2 files to read whole, one call each, in parallel:'
                         ' "a", "b"' + tail.format('them'))
        big = '"m" (6.0 MB: too large to read whole; search it with grep, or pass path to list one folder)'
        self.assertEqual(hint([('a', 16000, True), ('m', 6291456, False)], False, 5, False),
                         '; the other 5 lines are listed, one per line, in "a" (16 KB: read it whole in one call), and the'
                         ' remaining lines in ' + big + tail.format('the first one'))
        self.assertEqual(hint([('m', 6291456, False)], True, 5, True),
                         '; the other 5 lines the index does not resolve to the query (text matches, other symbols with the'
                         ' same name) are listed, one per line, in ' + big
                         + ' (outside the workspace: if you cannot read it, pass path to list one folder at a time)')


if __name__ == '__main__':
    unittest.main()
