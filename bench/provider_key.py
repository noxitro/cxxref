"""Copilot CLI の providers.json から、名前で選んだ提供元の apiKey を標準出力に出す(COPILOT_PROVIDER_API_KEY_COMMAND 用)。

キーをファイルや環境変数に写さずに済ませるためのもの。キー以外は何も出さない。
usage: python -I provider_key.py <提供元の名前> [providers.json のパス(既定 ~/.copilot/providers.json)]
"""
import json
import os
import sys

name = sys.argv[1]
path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.expanduser('~'), '.copilot', 'providers.json')
with open(path, encoding='utf-8-sig') as f:
    providers = json.load(f).get('providers', [])
for p in providers:
    if p.get('name') == name and p.get('apiKey'):
        sys.stdout.write(p['apiKey'])
        sys.exit(0)
sys.exit(f'provider {name!r} with apiKey not found in {path}')
