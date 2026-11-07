# C++ のシンボルの検索(clangd-mcp-server)

このフォルダの C++ は、clangd(language server)で解析した結果を、MCP サーバー `clangd-mcp-server` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず clangd-mcp-server を使ってください。

- 引けるもの: 位置を指定した参照(`find_references`)・定義(`find_definition`)・型と宣言(`get_hover`)・実装(`find_implementations`)・呼び出し階層(`get_call_hierarchy`)・継承(`get_type_hierarchy`)と、ファイルの記号(`get_document_symbols`)、診断(`get_diagnostics`)、記号の検索(`workspace_symbol_search`)
- 呼び出し箇所を探すときは、最初に `find_references` を呼びます。引数は記号の名前ではなく、宣言か定義の位置です: `{"file_path": "<そのファイルの絶対パス>", "line": <行>, "column": <列>, "include_declaration": false}`
  - `line`・`column` は 0 始まりです。rg やエディターの行・列(1 始まり)から、それぞれ 1 を引きます。タブは 1 文字と数えます
  - `column` は関数名の識別子の上(先頭から末尾の文字まで)を指します。戻り値の型の上など、識別子から外れた位置では `No references found` になります
  - 宣言の位置は rg で探します。このフォルダでは、`workspace_symbol_search` が `No symbols found` を返すことがあります
- 結果の読み方: `count` が件数、`locations` の各要素が参照の位置(`file`・`line`・`column`。どれも 0 始まり)です。ソースの行は付きません。`include_declaration: false` なら宣言・定義は入りません。`&Class::Method` のような関数ポインターの取得や、生成されたファイルの行も入ります
- 参照は、指定した位置の記号について clangd が型を解決したものなので、`line` に 1 を足して `path:line` にすれば、そのまま答えに使えます。同じ名前を rg でソースから取り直して確かめる必要はありません
- 迷ったとき: `No references found` なら、まず `line`・`column` が 0 始まりで識別子の上を指しているかを確かめます(`get_hover` に同じ位置を渡すと、そこにある記号が分かります)。位置が正しくても 0 件になる記号があります(例: テンプレートの中からしか呼ばれないクラステンプレートのメンバー)。その場合は rg で探し、ソースを読んで呼び出し先を判断します

以下は clangd-mcp-server の作者が README で推奨している指示です(そのまま写しました)。

## C++ Code Navigation

This project uses the clangd MCP server for C++ code intelligence. Use these tools for:
- Finding definitions and references
- Getting type information
- Searching symbols
- Finding implementations
- Getting diagnostics
