"""1 つの TU を、マクロを -D で足して読み直す(反転との比較用)。
usage: python -I dtest.py <ルート> <compile_commands.json> <作業フォルダ> <TU の相対パス> <マクロ>...
"""
import json, os, sys

root, cdbp, work, rel = sys.argv[1:5]
root = os.path.abspath(root)
want = os.path.normcase(os.path.join(root, rel.replace('/', os.sep)))
for e in json.load(open(cdbp, encoding='utf-8')):
    p = os.path.normcase(os.path.normpath(os.path.join(e.get('directory', ''), e['file'])))
    if p == want:
        break
else:
    sys.exit('TU が無い')
c = e['command']
k = c.rfind(' -c ')
e['command'] = c[:k] + ''.join(' -D' + m for m in sys.argv[5:]) + c[k:]
os.makedirs(work, exist_ok=True)
json.dump([e], open(os.path.join(work, 'compile_commands.json'), 'w', encoding='utf-8'))
