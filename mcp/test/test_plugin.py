"""このリポジトリを Copilot CLI のプラグインとして入れるための定義の検査(Copilot は要らない)。
plugin.json・mcp.json(Agent Plugins 1.0)、com.github.copilot/extensions の拡張、.github/plugin/marketplace.json。

usage: python -I -m unittest discover -s mcp/test   (リポジトリのルートで)
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
SCHEMA = 'https://agent-plugins.org/schemas/1.0.0/'


def load(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return json.load(f)


class PluginTest(unittest.TestCase):
    def test_manifest(self):
        p = load('plugin.json')
        self.assertEqual(p['$schema'], SCHEMA + 'plugin.schema.json')
        # Agent Plugins 1.0 の名前の決まり(小文字・数字・- と .、英数字で始まり終わる)
        self.assertRegex(p['name'], r'^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$')
        allowed = {'$schema', 'name', 'version', 'description', 'author', 'homepage', 'repository', 'license',
                   'keywords', 'extensions'}
        self.assertLessEqual(set(p), allowed)  # 閉じた形式。ほかの欄は無視される
        self.assertRegex(p['version'], r'^\d+\.\d+\.\d+$')

    def test_versions_agree(self):
        # タグ v<版> でリリースを作るとき、CI が同じことを確かめる。get-ltidx は plugin.json の版を取ってくる
        v = load('plugin.json')['version']
        m = load(os.path.join('.github', 'plugin', 'marketplace.json'))
        entry = [x for x in m['plugins'] if x['name'] == load('plugin.json')['name']]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]['version'], v)
        with open(os.path.join(ROOT, 'mcp', 'server.py'), encoding='utf-8') as f:
            self.assertIn(f"VERSION = '{v}'", f.read())

    def test_mcp_server(self):
        m = load('mcp.json')
        self.assertEqual(m['$schema'], SCHEMA + 'mcp.schema.json')
        s = m['mcpServers']['code-ref-index']  # ツールの名前(code-ref-index-refs など)はこの名前から付く
        self.assertEqual(s['type'], 'stdio')
        script = [a for a in s['args'] if a.startswith('${PLUGIN_ROOT}/')]
        self.assertEqual(len(script), 1)
        self.assertTrue(os.path.exists(os.path.join(ROOT, script[0][len('${PLUGIN_ROOT}/'):])))

    def test_no_rule(self):
        # プラグインは指示(rule)を持たない。rule は有効なリポジトリのすべてで毎回読まれるため、使うリポジトリだけで
        # /cxxref-enable(setup-instructions.ps1)で入れる。指示の中身は scripts/cxxref-instructions.md
        self.assertFalse(os.path.exists(os.path.join(ROOT, 'com.github.copilot', 'rules')))
        with open(os.path.join(ROOT, 'scripts', 'cxxref-instructions.md'), encoding='utf-8') as f:
            text = f.read()
        self.assertFalse(text.startswith('---'))  # 前置きは setup-instructions.ps1 が足す側で付ける
        self.assertIn('`refs`', text)
        self.assertIn('`root`', text)  # プラグインのサーバーは作業フォルダを知らないので、root を渡させる

    def test_extension(self):
        # 索引を作るスラッシュコマンド /cxxref-index(モデルを呼ばない拡張)。Agent Plugins 1.0 では com.github.copilot/extensions/
        # の下に置くと読まれる【実測: 1.0.105。SDK でランタイムを動かし、出どころ plugin として見つかった】
        ext = os.path.join(ROOT, 'com.github.copilot', 'extensions', 'cxxref', 'extension.mjs')
        with open(ext, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('name: "cxxref-index"', text)
        self.assertIn('name: "cxxref-enable"', text)  # 使い方の指示を足す・消す(setup-instructions.ps1)
        for s in ('get-ltidx.ps1', 'index-project.ps1', 'setup-instructions.ps1'):  # 拡張が動かすスクリプトがプラグインの中にある
            self.assertIn(s, text)
            self.assertTrue(os.path.exists(os.path.join(ROOT, 'scripts', s)))
        node = shutil.which('node')
        if node:
            r = subprocess.run([node, '--check', ext], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            # SDK に依らない部分(プラグインの場所・ltidx の探し方・作業フォルダ・ルート)の単体テスト
            r = subprocess.run([node, '--test', os.path.join(os.path.dirname(ext), 'lib.test.mjs')],
                               capture_output=True, text=True, encoding='utf-8', errors='replace')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
