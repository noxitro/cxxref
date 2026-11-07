"""compile_commands.json の応答ファイル(@file。入れ子も)を展開し、arguments に引数を並べた compile_commands.json を書く。

UE5 の UnrealBuildTool(-mode=GenerateClangDatabase)は、clang-cl に引数を応答ファイルで渡す形で書く。
応答ファイルを展開するかどうかはツールによって違うので、比べるときは展開したものを全員に渡す。
応答ファイルの中は Windows のコマンドラインの規則(CommandLineToArgvW と同じ。clang-cl もこの規則で読む)で区切る。

--for-tools を付けると、解析のツールに渡すのに困る次の引数を外す。
- -resource-dir=...(コンパイラ自身の組み込みヘッダーの場所)。UE5 は VS 同梱の clang 22 の場所を明示するので、
  別の版の clang(ツールの libclang・clangd)が読むと、その版に無い組み込み関数(__builtin_ia32_tmmultf32ps_internal など)で
  エラーになる。外せば各ツールが自分の版のものを使う
- 依存ファイルを書き出す指定(/clang:-MD・/clang:-MF<file>)。書き出し先が無くてエラーになり、あれば写しの中に .d を書く

usage: python -I expand_rsp.py <入力 compile_commands.json> <出力 compile_commands.json> [--for-tools]
"""
import json
import os
import sys


def split_windows(s):
    """Windows のコマンドラインの規則で区切る(空白で区切り、"" で囲む。\\ の後の " の扱いは CommandLineToArgvW と同じ)。"""
    args, cur, i, quoted, have = [], [], 0, False, False
    while i < len(s):
        c = s[i]
        if c == '\\':
            n = 0
            while i < len(s) and s[i] == '\\':
                n += 1
                i += 1
            if i < len(s) and s[i] == '"':
                cur.append('\\' * (n // 2))
                if n % 2:
                    cur.append('"')
                    i += 1
                have = True
            else:
                cur.append('\\' * n)
                have = True
            continue
        if c == '"':
            if quoted and i + 1 < len(s) and s[i + 1] == '"':  # 囲みの中の "" は " 1 つ
                cur.append('"')
                i += 2
                continue
            quoted = not quoted
            have = True
            i += 1
            continue
        if c in ' \t\r\n' and not quoted:
            if have or cur:
                args.append(''.join(cur))
            cur, have = [], False
            i += 1
            continue
        cur.append(c)
        have = True
        i += 1
    if have or cur:
        args.append(''.join(cur))
    return args


def expand(args, directory, cache, depth=0):
    out = []
    for a in args:
        if a.startswith('@') and depth < 8:
            path = a[1:]
            full = os.path.normpath(os.path.join(directory, path))
            if full not in cache:
                with open(full, 'rb') as f:
                    raw = f.read()
                text = raw.decode('utf-16') if raw[:2] in (b'\xff\xfe', b'\xfe\xff') else raw.decode('utf-8-sig')
                cache[full] = split_windows(text)
            out.extend(expand(cache[full], directory, cache, depth + 1))
        else:
            out.append(a)
    return out


def main():
    src, dst = sys.argv[1], sys.argv[2]
    for_tools = '--for-tools' in sys.argv[3:]
    with open(src, encoding='utf-8') as f:
        db = json.load(f)
    cache, out, missing, dropped = {}, [], 0, 0
    for e in db:
        args = e.get('arguments') or split_windows(e['command'])
        try:
            args = expand(args, e['directory'], cache)
        except FileNotFoundError:
            missing += 1
            continue
        if for_tools:
            n = len(args)
            args = [a for a in args if not a.startswith(('-resource-dir', '/clang:-MD', '/clang:-MF', '/clang:-MMD'))]
            dropped += n - len(args)
        out.append({'directory': e['directory'], 'file': e['file'], 'arguments': args,
                    **({'output': e['output']} if 'output' in e else {})})
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f'{len(out)} entries written, {missing} skipped (response file not found), {len(cache)} response files read'
          + (f', {dropped} arguments dropped (--for-tools)' if for_tools else ''))


if __name__ == '__main__':
    main()
