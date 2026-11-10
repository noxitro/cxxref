"""runs/index.jsonl の各実行について、Copilot の記録から往復・ツール・トークン・正答を集計する。

usage: python -I collect.py [bench_dir]
出力: runs/summary.tsv と、条件ごとの平均を標準出力に。
"""
import collections, json, os, re, sqlite3, statistics, sys

bench = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(__file__))
home = os.path.expanduser("~/.copilot")
# tasks.json と tasks-<名前>.json(公開しないコードベースのものは local\ に置く)。id はファイルをまたいで一意にする
tasks, task_src = {}, {}
for d in (bench, os.path.join(bench, "local")):
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not re.fullmatch(r"tasks(-[\w-]+)?\.json", fn):
            continue
        path = os.path.join(d, fn)
        with open(path, encoding="utf-8") as f:
            for t in json.load(f):
                if t["id"] in tasks:
                    sys.exit(f"問題の id {t['id']} が {task_src[t['id']]} と {path} の両方にある")
                tasks[t["id"]], task_src[t["id"]] = t, path
db = sqlite3.connect("file:" + os.path.join(home, "session-store.db") + "?mode=ro", uri=True)
LOC = re.compile(r"(?<![\w/.-])([\w.-]+(?:/[\w.-]+)*\.(?:h|hh|hpp|hxx|c|cc|cpp|cxx|inl|ipp|inc|ixx|cppm|tpp|txx|m|mm|vcxproj|props|targets|cmake|json|hlsl)):(\d+)")  # C++ 以外は rg 向きの問題(R3)用


# 列挙の行(1 行に 1 か所。先頭の - * 番号 ` は許し、後ろは括弧書き・— ・# などの注記だけを許す)。説明の文の中で「除いた」と挙げた行は数えない
LIST_LINE = re.compile(r"^\s*(?:[-*]\s+|\d+[.)]\s+)?`?([\w.-]+(?:/[\w.-]+)*\.[A-Za-z]+):(\d+)`?(?:\s*(?:[(（—#]|//|-\s).*)?\s*$")


def normalize(path, truth_paths):
    # 書式違反で先頭のディレクトリを落とした回答(T1-D-r1 の runtime/)も中身で採点する
    if path in truth_paths:
        return path
    cands = [p for p in truth_paths if p.endswith("/" + path)]
    return cands[0] if len(cands) == 1 else path

rows, no_task = [], collections.Counter()
for line in open(os.path.join(bench, "runs", "index.jsonl"), encoding="utf-8-sig"):
    r = json.loads(line)
    if r["rep"] == 0:
        continue  # 試運転
    if r["task"] not in tasks:
        no_task[r["task"]] += 1  # 問題ファイルが手元に無い(local\ を置いていない)
        continue
    ev = [json.loads(l) for l in open(os.path.join(home, "session-state", r["session"], "events.jsonl"), encoding="utf-8")]
    tools = collections.Counter(e["data"]["toolName"] for e in ev if e["type"] == "tool.execution_start")
    result_chars = 0
    for e in ev:
        if e["type"] == "tool.execution_complete":
            res = e["data"].get("result")
            result_chars += len(json.dumps(res, ensure_ascii=False)) if res is not None else 0
    answer = [e["data"].get("content", "") for e in ev if e["type"] == "assistant.message"][-1:] or [""]
    truth = set(tasks[r["task"]]["truth"])
    truth_paths = {t.rsplit(":", 1)[0] for t in truth}
    got = {f"{normalize(p, truth_paths)}:{n}" for p, n in LOC.findall(answer[0].replace("\\", "/"))}
    listed = {f"{normalize(m.group(1), truth_paths)}:{m.group(2)}" for m in map(LIST_LINE.match, answer[0].replace("\\", "/").splitlines()) if m}
    u = db.execute(
        "select count(*), sum(input_tokens), sum(cache_read_tokens), sum(output_tokens), sum(total_nano_aiu)/1e9 "
        "from assistant_usage_events where session_id=?", (r["session"],)).fetchone()
    calls, inp, cache, out, credits = (u[0], u[1] or 0, u[2] or 0, u[3] or 0, u[4] or 0.0)
    rows.append({
        "name": r["name"], "round": r.get("round", "v1"), "task": r["task"], "cond": r["cond"], "sec": r["sec"],
        "model_calls": calls, "tool_calls": sum(tools.values()), "view": tools["view"], "lsp": tools["lsp"],
        "shell": tools["powershell"],
        "mcp": sum(n for t, n in tools.items() if t.startswith(("code-ref-index", "serena", "clang-index-mcp", "clast", "clangd-mcp", "mcp-language-server"))),
        "search": tools["rg"] + tools["grep"] + tools["glob"], "tool_result_chars": result_chars,
        "input_tokens": inp, "cache_read": cache, "uncached_input": inp - cache, "output_tokens": out,
        "credits": round(credits, 4), "tp": len(got & truth), "fp": len(got - truth), "fn": len(truth - got),
        "correct": int(got == truth), "missed": " ".join(sorted(truth - got)), "extra": " ".join(sorted(got - truth)),
        # v12 から: 最後の列挙の行だけで採点したもの(説明の文で除いた行を挙げても誤りにしない)
        "correct_list": int(listed == truth), "list_fp": len(listed - truth), "list_fn": len(truth - listed),
        "list_extra": " ".join(sorted(listed - truth)),
    })

keys = list(rows[0].keys()) if rows else []
with open(os.path.join(bench, "runs", "summary.tsv"), "w", encoding="utf-8") as f:
    f.write("\t".join(keys) + "\n")
    for x in rows:
        f.write("\t".join(str(x[k]) for k in keys) + "\n")

for t, n in sorted(no_task.items()):
    print(f"注意: 問題 {t} が問題ファイルに無いので、{n} 回の実行を集計から外した", file=sys.stderr)
print("cond  n  correct  list  model_calls  tool_calls  view  input_tokens  uncached  output  credits  sec")
for rd, c in sorted({(x["round"], x["cond"]) for x in rows}):
    xs = [x for x in rows if x["cond"] == c and x["round"] == rd]
    m = lambda k: statistics.mean(x[k] for x in xs)
    print(f"{rd}-{c:4} {len(xs):2}  {sum(x['correct'] for x in xs):2}/{len(xs):<2}  {sum(x['correct_list'] for x in xs):2}/{len(xs):<2} {m('model_calls'):8.1f}  {m('tool_calls'):9.1f}  {m('view'):4.1f}"
          f"  {m('input_tokens'):11.0f}  {m('uncached_input'):8.0f}  {m('output_tokens'):6.0f}  {m('credits'):.3f}  {m('sec'):4.0f}")
for x in rows:
    print(f"{x['name']:10} ok={x['correct']} tp={x['tp']} fp={x['fp']} fn={x['fn']} calls={x['model_calls']} tools={x['tool_calls']}"
          f"(view {x['view']}, lsp {x['lsp']}, search {x['search']}, shell {x['shell']}, mcp {x['mcp']}) in={x['input_tokens']} out={x['output_tokens']} cr={x['credits']}")
