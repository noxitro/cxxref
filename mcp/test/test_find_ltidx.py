"""MCP サーバーが ltidx.exe を探す順のテスト(ltidx は要らない)。
環境変数 LTIDX、このリポジトリでビルドしたもの、配布物を取ってきたもの(%LOCALAPPDATA%\\cxxref\\ltidx)の順。

usage: python -I -m unittest discover -s mcp/test   (リポジトリのルートで)
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import autoupdate  # noqa: E402


class FindLtidxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cri')
        self.plugin = os.path.join(self.tmp, 'plugin', 'mcp')  # プラグインとして入れた写し(ビルドした ltidx が無い)
        os.makedirs(self.plugin)
        self.app = os.path.join(self.tmp, 'appdata')
        self.got = os.path.join(self.app, 'cxxref', 'ltidx', 'ltidx.exe')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def find(self, **env):
        e = {k: v for k, v in os.environ.items() if k != 'LTIDX'}
        e.update({'LOCALAPPDATA': self.app}, **env)
        with mock.patch.object(autoupdate, 'HERE', self.plugin), mock.patch.dict(os.environ, e, clear=True):
            return autoupdate.find_ltidx()

    def touch(self, p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'w').close()

    def test_missing_localappdata_is_not_searched_relative_to_cwd(self):
        # LOCALAPPDATA が無いとき、配布物の場所を作業フォルダからの相対パス(cxxref/ltidx/ltidx.exe)で探さない
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp)
            self.touch(os.path.join(self.tmp, 'cxxref', 'ltidx', 'ltidx.exe'))
            e = {k: v for k, v in os.environ.items() if k not in ('LTIDX', 'LOCALAPPDATA')}
            with mock.patch.object(autoupdate, 'HERE', self.plugin), mock.patch.dict(os.environ, e, clear=True):
                self.assertIsNone(autoupdate.find_ltidx())
        finally:
            os.chdir(cwd)

    def test_missing_ltidx_variable_message(self):
        # LTIDX が無いファイルを指すときは、get-ltidx ではなく LTIDX を直すよう知らせる(取ってきても直らない)
        with mock.patch.dict(os.environ, {'LTIDX': r'D:\x\ltidx.exe'}):
            self.assertIn('LTIDX', autoupdate.no_ltidx())
            self.assertNotIn('get-ltidx', autoupdate.no_ltidx())

    def test_order(self):
        self.assertIsNone(self.find())
        self.touch(self.got)
        self.assertEqual(self.find(), os.path.normpath(self.got))  # 配布物を取ってきたもの
        built = os.path.join(self.tmp, 'plugin', 'indexer', 'build', 'ltidx.exe')
        self.touch(built)
        self.assertEqual(self.find(), os.path.normpath(built))  # ビルドしたものが先
        self.assertEqual(self.find(LTIDX=r'D:\x\ltidx.exe'), r'D:\x\ltidx.exe')  # 環境変数は無くてもそのまま使う

    def stale(self, **env):
        e = {k: v for k, v in os.environ.items() if k != 'LTIDX'}
        e.update({'LOCALAPPDATA': self.app}, **env)
        with mock.patch.object(autoupdate, 'HERE', self.plugin), mock.patch.dict(os.environ, e, clear=True):
            return autoupdate.stale_ltidx()

    def test_stale_downloaded_ltidx(self):
        # 取ってきた ltidx の版(version.txt)がプラグイン(plugin.json)と違えば知らせる。プラグインを更新しても入れ替わらないので
        with open(os.path.join(self.tmp, 'plugin', 'plugin.json'), 'w', encoding='utf-8') as f:
            f.write('{"name": "cxxref", "version": "0.6.0"}')
        self.assertIsNone(self.stale())  # ltidx が無い
        self.touch(self.got)
        self.assertEqual(self.stale(), ('unknown', '0.6.0'))  # version.txt の無い古い配布物
        with open(os.path.join(os.path.dirname(self.got), 'version.txt'), 'w', encoding='utf-8') as f:
            f.write('0.5.1\n')
        self.assertEqual(self.stale(), ('0.5.1', '0.6.0'))
        with open(os.path.join(os.path.dirname(self.got), 'version.txt'), 'w', encoding='utf-8') as f:
            f.write('0.6.0\n')
        self.assertIsNone(self.stale())  # 同じ版
        self.assertIsNone(self.stale(LTIDX=self.got))  # 環境変数で決めたものは見ない
        with open(os.path.join(os.path.dirname(self.got), 'version.txt'), 'w', encoding='utf-8') as f:
            f.write('0.5.1\n')
        self.touch(os.path.join(self.tmp, 'plugin', 'indexer', 'build', 'ltidx.exe'))
        self.assertIsNone(self.stale())  # ビルドしたものを使うときは見ない


if __name__ == '__main__':
    unittest.main()
