"""索引の自動更新のテスト: 一時フォルダの小さなプロジェクトを ltidx で索引にし、server.py を起動して、
ソースを変えた後の呼び出しで作り直されることを確かめる。ltidx.exe が要る(無ければ飛ばす)。

usage: python -I -m unittest discover -s mcp/test   (リポジトリのルートで)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, '..', 'server.py')
LTIDX = os.environ.get('LTIDX') or os.path.join(HERE, '..', '..', 'indexer', 'build', 'ltidx.exe')


# CRI_REQUIRE_LTIDX=1(CI の ltidx のジョブ)のときは飛ばさず、ltidx.exe が無ければ失敗させる
@unittest.skipUnless(os.path.exists(LTIDX) or os.environ.get('CRI_REQUIRE_LTIDX'), 'ltidx.exe is not built')
class AutoUpdateTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='cri')
        self.dir = os.path.join(self.root, '.index')
        self.index = os.path.join(self.dir, 'index.tsv')
        self.cmds = []
        self.proc = None
        self.write('h.h', '#pragma once\nvoid Shared();\n')
        self.write('a.cpp', '#include "h.h"\nvoid A() { Shared(); }\n', tu=True)
        os.makedirs(self.dir)
        r = subprocess.run([LTIDX, '-p', self.root, '-root', self.root, '-o', self.index],
                           capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(0.05)  # 以後の書き込みが、確実に索引より新しくなるように

    def tearDown(self):
        if self.proc:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
            self.proc.stdout.close()
        deadline = time.time() + 60  # 裏の作り直しが終わるのを待ってから消す
        while os.path.exists(os.path.join(self.dir, 'update.lock')) and time.time() < deadline:
            time.sleep(0.1)
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, text, tu=False):
        with open(os.path.join(self.root, rel), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        if tu and rel not in self.cmds:
            self.cmds.append(rel)
            d = self.root.replace('\\', '/')
            with open(os.path.join(self.root, 'compile_commands.json'), 'w', encoding='utf-8') as f:
                json.dump([{'directory': d, 'file': f'{d}/{r}', 'arguments': ['cl.exe', '/c', '/std:c++17', r]}
                           for r in self.cmds], f)

    def recipe(self, cdb=None):
        with open(self.index + '.recipe.json', 'w', encoding='utf-8') as f:
            json.dump({'version': 1, 'root': self.root, 'cdb': cdb or self.root, 'out': self.index, 'jobs': 2,
                       'makeCdb': None}, f)

    def serve(self, wait):
        self.proc = subprocess.Popen([sys.executable, '-I', SERVER, '--root', self.root, '--auto-update-wait', str(wait)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def refs(self, name):
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'refs', 'arguments': {'name': name}}}
        self.proc.stdin.write((json.dumps(msg) + '\n').encode('utf-8'))
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline().decode('utf-8'))['result']['content'][0]['text']

    def test_fresh_index_is_not_rebuilt(self):
        self.recipe()
        self.serve(30)
        text = self.refs('Shared')
        self.assertNotIn('auto-update', text)
        self.assertFalse(os.path.exists(os.path.join(self.dir, 'update.json')))

    def test_changed_file_is_reindexed_before_answering(self):
        self.recipe()
        self.serve(30)
        self.write('a.cpp', '#include "h.h"\nvoid A() { Shared(); }\nvoid B() { Shared(); }\n')
        text = self.refs('Shared')
        self.assertIn('auto-update: rebuilt the index for 1 changed files', text)
        self.assertIn('== calls (resolved by clang): 2', text)
        self.assertNotIn('changed after indexing', text)
        text = self.refs('Shared')  # 作り直した後は、もう作り直さない
        self.assertNotIn('auto-update', text)

    def test_inactive_code_is_parsed_by_the_rebuild(self):
        # 作り直しは差分パス(無効な #if の中の解析)まで流す。足した呼び出しが無効な #if の中でも、解決済みの行として出る
        self.recipe()
        self.serve(60)
        self.write('a.cpp', '#include "h.h"\nvoid A() { Shared(); }\n#ifdef FEATURE\nvoid B() { Shared(); }\n#endif\n')
        text = self.refs('Shared')
        self.assertIn('auto-update: rebuilt the index', text)
        self.assertIn('a.cpp:4  [in B]  [inactive: #if defined(FEATURE)]  | void B() { Shared(); }', text)
        self.assertTrue(os.path.exists(self.index + '.alt.tsv'))

    def test_new_tu_is_picked_up(self):
        self.recipe()
        self.serve(30)
        self.write('b.cpp', '#include "h.h"\nvoid B() { Shared(); }\n', tu=True)
        text = self.refs('Shared')
        self.assertIn('auto-update: rebuilt', text)
        self.assertIn('b.cpp:2', text)

    def test_no_wait_answers_now_and_later_call_uses_new_index(self):
        self.recipe()
        self.serve(0)
        self.write('a.cpp', '#include "h.h"\nvoid A() { Shared(); }\nvoid B() { Shared(); }\n')
        text = self.refs('Shared')
        self.assertIn('rebuilding the index in the background', text)
        self.assertIn('== calls (resolved by clang): 1', text)  # 今の索引で答える
        deadline = time.time() + 30
        while time.time() < deadline:
            st = os.path.join(self.dir, 'update.json')
            if os.path.exists(st) and not os.path.exists(os.path.join(self.dir, 'update.lock')):
                break
            time.sleep(0.1)
        text = self.refs('Shared')
        self.assertIn('== calls (resolved by clang): 2', text)

    def test_index_without_recipe_is_left_alone(self):
        self.serve(30)
        self.write('a.cpp', '#include "h.h"\nvoid A() { Shared(); }\nvoid B() { Shared(); }\n')
        text = self.refs('Shared')
        self.assertIn('auto-update: off for this index', text)
        self.assertIn('changed after indexing', text)

    def test_failure_is_not_retried_until_the_next_change(self):
        self.recipe(cdb=os.path.join(self.root, 'no-such-dir'))
        self.serve(30)
        self.write('a.cpp', '#include "h.h"\nvoid A() { Shared(); }\nvoid B() { Shared(); }\n')
        text = self.refs('Shared')
        self.assertIn('auto-update: rebuild failed', text)
        with open(os.path.join(self.dir, 'update.json'), encoding='utf-8') as f:
            first = json.load(f)['finished']
        text = self.refs('Shared')
        self.assertIn('auto-update: the last rebuild failed', text)
        with open(os.path.join(self.dir, 'update.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['finished'], first)  # 同じ失敗を繰り返していない


if __name__ == '__main__':
    unittest.main()
