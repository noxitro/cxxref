"""Nox の MSVC 形式 compile_commands.json を、clast(libclang)が読める形に変換する。

- --driver-mode=cl を差し込む(libclang に cl.exe 形式の引数 /I /D /std: を解釈させる)
- /Yu /Yc /Fp(PCH。PCH ファイルは無いので libclang が失敗する)を除く
- 実在しないファイルのエントリ(.msbuild-extractor-sample)を除く
使い方: python make_cdb.py <入力cdb.json> <出力ディレクトリ> [--only 部分文字列 ...]
"""
import json, os, sys

def main():
    src, outdir = sys.argv[1], sys.argv[2]
    only = []
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1:]
    entries = json.load(open(src, encoding="utf-8"))
    out, seen = [], set()
    for e in entries:
        f = e["file"]
        if not os.path.isfile(f) or f in seen:
            continue
        if only and not any(o in f.replace("\\", "/") for o in only):
            continue
        seen.add(f)
        args = e.get("arguments")
        if args is None:
            continue
        args = [a for a in args if not a.startswith(("/Yu", "/Yc", "/Fp"))]
        args.insert(1, "--driver-mode=cl")
        out.append({"file": f, "directory": e["directory"], "arguments": args})
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "compile_commands.json"), "w", encoding="utf-8") as fp:
        json.dump(out, fp, indent=1)
    print(f"{len(out)} entries -> {outdir}")

main()
