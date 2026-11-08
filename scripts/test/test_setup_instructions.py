"""setup-instructions.ps1 のテスト。一時フォルダの .github/copilot-instructions.md(プロジェクト)と
.github/instructions/cxxref.instructions.md(ローカル。git から外す)に足して消し、元に戻ることを確かめる。
pwsh が要る(無ければ飛ばす)。ローカルの git の確かめは git も要る。

usage: python -I -m unittest discover -s scripts/test   (リポジトリのルートで)
"""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, '..', 'setup-instructions.ps1')
PWSH = shutil.which('pwsh')
GIT = shutil.which('git')


@unittest.skipUnless(PWSH, 'pwsh is required')
class SetupInstructionsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='cri')
        self.file = os.path.join(self.root, '.github', 'copilot-instructions.md')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_ps(self, action, scope=None, root=None):
        r = subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT,
                            '-Root', root or self.root, '-Action', action] + (['-Scope', scope] if scope else []),
                           capture_output=True, text=True, errors='replace')  # 出力の文字コードは起動のしかたで変わるので、中身はファイルで確かめる
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def write(self, data):
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, 'wb') as f:
            f.write(data)

    def read(self):
        with open(self.file, 'rb') as f:
            return f.read()

    def test_new_file_is_created_and_removed(self):
        self.run_ps('Add')
        text = self.read().decode('utf-8')
        self.assertTrue(text.startswith('<!-- cxxref:begin'))
        self.assertIn('`refs`', text)
        self.assertNotIn('\r\n', text)
        self.run_ps('Remove')
        self.assertFalse(os.path.exists(self.file))  # 足したときに作ったファイルなので消す

    def test_round_trip_keeps_other_text(self):
        orig = '﻿# 既存の指示\r\n\r\n- 日本語で答える\r\n'.encode('utf-8')
        self.write(orig)
        self.run_ps('Add')
        text = self.read()
        self.assertTrue(text.startswith(orig))
        self.assertNotIn(b'\n', text.replace(b'\r\n', b''))  # 元の CRLF に合わせる
        self.run_ps('Remove')
        self.assertEqual(self.read(), orig)

    def test_add_twice_replaces(self):
        self.write(b'# A\n')
        self.run_ps('Add')
        once = self.read()
        self.write(once.replace(b'`refs`', b'`old`'))  # 古い中身が入っている状態
        self.run_ps('Add')
        self.assertEqual(self.read(), once)

    def test_block_in_the_middle(self):
        self.write(b'# A\n')
        self.run_ps('Add')
        with open(self.file, 'ab') as f:
            f.write(b'\n# B\n')  # 足した後にユーザーが書き足した
        self.run_ps('Remove')
        self.assertEqual(self.read(), b'# A\n\n# B\n')

    def test_remove_when_absent(self):
        self.write(b'# A\n')
        self.run_ps('Remove')
        self.assertEqual(self.read(), b'# A\n')


@unittest.skipUnless(PWSH and GIT, 'pwsh and git are required')
class LocalInstructionsTest(unittest.TestCase):
    """ローカル: .github/instructions/cxxref.instructions.md を置き、.git/info/exclude で git から外す"""
    run_ps = SetupInstructionsTest.run_ps

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='cri')
        self.git('init', '-q', '.')
        self.local = os.path.join(self.root, '.github', 'instructions', 'cxxref.instructions.md')
        self.project = os.path.join(self.root, '.github', 'copilot-instructions.md')
        self.exclude = os.path.join(self.root, '.git', 'info', 'exclude')
        with open(self.exclude, 'rb') as f:
            self.orig_exclude = f.read()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        return subprocess.run([GIT, '-C', self.root, *args], capture_output=True, text=True, check=True).stdout

    def test_local_is_hidden_from_git_and_removed(self):
        self.run_ps('Add', 'Local')
        with open(self.local, encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(text.startswith('---\napplyTo: "**"\n---\n'))  # 前置きで、どのファイルでも効かせる
        self.assertIn('`refs`', text)
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), '')  # git status に出ない
        self.run_ps('Add', 'Local')  # 2 回足しても、除外の行は 1 組だけ
        with open(self.exclude, encoding='utf-8') as f:
            self.assertEqual(f.read().count('/.github/instructions/cxxref.instructions.md'), 1)
        self.run_ps('Remove')
        self.assertFalse(os.path.exists(os.path.join(self.root, '.github')))  # 足したときに作ったフォルダも消す
        with open(self.exclude, 'rb') as f:
            self.assertEqual(f.read(), self.orig_exclude)

    def test_local_in_subfolder(self):
        sub = os.path.join(self.root, 'src')
        os.makedirs(sub)
        self.run_ps('Add', 'Local', root=sub)
        with open(self.exclude, encoding='utf-8') as f:
            self.assertIn('/src/.github/instructions/cxxref.instructions.md', f.read())  # git のルートからのパス
        self.assertEqual(self.git('status', '--porcelain', '--untracked-files=all'), '')
        self.run_ps('Remove', 'Local', root=sub)
        self.assertEqual(os.listdir(sub), [])

    def test_project_replaces_local_and_local_skips_when_project(self):
        self.run_ps('Add', 'Local')
        self.run_ps('Add', 'Project')
        self.assertTrue(os.path.exists(self.project))
        self.assertFalse(os.path.exists(self.local))  # 二重にならないよう、手元だけの分は消す
        self.run_ps('Add', 'Local')
        self.assertFalse(os.path.exists(self.local))  # プロジェクトに入っていれば、ローカルには足さない
        self.run_ps('Remove', 'Local')
        self.assertTrue(os.path.exists(self.project))  # Local だけを消すときは、プロジェクトに触らない
        self.run_ps('Remove')
        self.assertFalse(os.path.exists(self.project))

    def test_tracked_file_is_refused(self):
        os.makedirs(os.path.dirname(self.local))
        with open(self.local, 'w', encoding='utf-8') as f:
            f.write('<!-- cxxref:local -->\n')
        self.git('add', '.')
        r = subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT,
                            '-Root', self.root, '-Action', 'Add', '-Scope', 'Local'], capture_output=True)
        self.assertNotEqual(r.returncode, 0)  # コミットされたファイルは git から外せない

    def test_foreign_file_is_kept(self):
        os.makedirs(os.path.dirname(self.local))
        with open(self.local, 'w', encoding='utf-8') as f:
            f.write('# 自分で書いた指示\n')
        r = subprocess.run([PWSH, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', SCRIPT,
                            '-Root', self.root, '-Action', 'Remove'], capture_output=True)
        self.assertNotEqual(r.returncode, 0)
        with open(self.local, encoding='utf-8') as f:
            self.assertEqual(f.read(), '# 自分で書いた指示\n')


if __name__ == '__main__':
    unittest.main()
