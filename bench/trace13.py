"""1 回の実行で、エージェントがどの道具をどの順に使ったかを、モデルの呼び出しごとの 1 行に縮めて出す。

usage: python -I trace13.py <回の名前> [<回の名前> ...]
  v13 の回(runs13/index.jsonl)は v13 の COPILOT_HOME の写し、v12 までと段階 3 の回(runs/index.jsonl)は
  ~/.copilot の session-state から、events.jsonl を読む。記録は git に入れていない(会話の全文を含む)ので、手元で流した回だけ。

出力の例
  1  refs(EntityLogicStorage::CreateInstance) · powershell
  2  view(entity_logic_legacy.h) · view(entity_ecs_test.cpp)
  3  apply_patch(entity_logic_legacy.h) ✗ os error 1224
道具の後の括弧は対象(refs は名前、view・apply_patch はファイル名、rg・grep は検索語)。✗ は失敗した呼び出し。
行の番号は「道具を使ったモデルの呼び出し」の通し番号で、道具を使わなかった呼び出し(最後に答えを書く回など)は数えない。
そのため、行の数は記録の calls(モデルを呼んだ回数)より少ない。
"""
import json, os, re, sys

BENCH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BENCH)
import run13


def find_events(name):
    for idx, kind in ((os.path.join(run13.RUNS, 'index.jsonl'), 'v13'), (os.path.join(BENCH, 'runs', 'index.jsonl'), 'old')):
        if not os.path.exists(idx):
            continue
        for line in open(idx, encoding='utf-8-sig'):
            r = json.loads(line)
            if r.get('name') != name:
                continue
            base = os.path.join(run13.V13, 'homes', name) if kind == 'v13' else os.path.expanduser('~/.copilot')
            p = os.path.join(base, 'session-state', r['session'], 'events.jsonl')
            return r, p
    sys.exit(f'{name}: 記録が見つからない')


def short_tool(t):
    return t.removeprefix('code-ref-index-')


def target(tool, args):
    if isinstance(args, str):  # apply_patch は引数がパッチの本文そのもの
        args = {'input': args}
    if not isinstance(args, dict):
        return ''
    if tool.endswith('refs'):
        return args.get('name', '')
    if tool.endswith('check'):
        return f"{len(args.get('locations', []))} 行"
    if tool in ('view', 'read_file', 'apply_patch', 'edit', 'create'):
        p = args.get('path') or args.get('filePath') or ''
        if not p and tool == 'apply_patch':
            m = re.findall(r'\*\*\* (?:Update|Add|Delete) File: (.+)', args.get('input', '') or '')
            return ', '.join(os.path.basename(x.strip()) for x in m)
        return os.path.basename(p)
    if tool in ('rg', 'grep', 'grep_search'):
        q = args.get('pattern') or args.get('query') or ''
        return q if len(q) <= 40 else q[:37] + '...'
    if tool == 'glob':
        return args.get('pattern', '')
    return ''


def trace(name):
    r, p = find_events(name)
    starts, turns, order = {}, {}, []
    for line in open(p, encoding='utf-8'):
        e = json.loads(line)
        d = e.get('data', {})
        if e['type'] == 'tool.execution_start':
            t = d.get('toolName', '?')
            starts[d.get('toolCallId')] = (t, d.get('turnId'), target(t, d.get('arguments')))
            if d.get('turnId') not in turns:
                turns[d.get('turnId')] = []
                order.append(d.get('turnId'))
            turns[d.get('turnId')].append(d.get('toolCallId'))
        elif e['type'] == 'tool.execution_complete':
            cid = d.get('toolCallId')
            if cid in starts and not d.get('success', True):
                res = json.dumps([d.get('result'), d.get('error')], ensure_ascii=False)
                why = 'os error 1224' if 'os error 1224' in res else ''
                starts[cid] = starts[cid] + (why or 'failed',)
    print(f'# {name}  ' + ' '.join(f'{k}={r[k]}' for k in ('calls', 'input', 'sec') if r.get(k) is not None))
    for i, tid in enumerate(order, 1):
        parts = []
        for cid in turns[tid]:
            s = starts[cid]
            item = short_tool(s[0]) + (f'({s[2]})' if s[2] else '')
            if len(s) > 3:
                item += ' ✗' + (f' {s[3]}' if s[3] != 'failed' else '')
            parts.append(item)
        print(f'{i:3}  ' + ' · '.join(parts))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    for n in sys.argv[1:]:
        trace(n)
        print()
