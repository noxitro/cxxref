# C++ のシンボルの検索(clangd-mcp)

このフォルダの C++ は、clangd(language server)で解析した結果を、MCP サーバー `clangd-mcp` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず clangd-mcp を使ってください。

- 引けるもの: 名前を指定した参照(`find_references`)、定義(`get_definition`)、記号の検索(`find_symbol`)、型(`get_type_info`)、仮想関数の実装(`find_implementations`)、呼び出し元・呼び出し先(`get_callers`・`get_callees`)、継承(`get_type_hierarchy`)と、ファイルの記号(`list_file_symbols`)
- 呼び出し箇所を探すときは、最初に `find_references` を呼びます。引数 `symbol_name` には `クラス名::関数名` か、名前空間から書いた修飾名を渡します(例: `{"symbol_name": "Renderer::Update"}`、`{"symbol_name": "app::net::Socket::Send"}`)。素の名前(`Send`)を渡すと、同じ名前の記号のうち最初に見つかった 1 つが使われるので、別のクラスの記号の参照や 0 件になることがあります
- 結果の読み方:
  - 1 行目 `Found N reference(s) to '<名前>':` の名前は素の名前で、どのクラスの記号かは書かれていません
  - 続いてファイルごとに `<path>:` と、`<行>:<列>  <その行のソース>`(1 始まり)が並びます
  - 宣言と定義の行(関数の宣言・定義そのもの)も入るので、呼び出し箇所を答えるときは除きます。`&Class::Method` のような関数ポインターの取得や、生成されたファイルの行も入ります
- 参照は clangd が型を解決したものなので、宣言・定義の行を除けば、そのまま答えに使えます。同じ名前を rg でソースから取り直して確かめる必要はありません。コメントの中(`/* */` を含む)の同名は出てきません
- どの記号の参照かを確かめるときは、`find_symbol`(`{"query": "<find_references に渡したのと同じ文字列>"}`)を呼びます。修飾名を渡したときは、先頭に出る記号(`[Method] Send (in app::net::Socket)` のように属するクラス付き)が `find_references` の使った記号です
- 迷ったとき: `No symbols found matching` なら、その形では記号が見つかっていません。クラステンプレート(特に部分特殊化)のメンバーは、`クラス名::関数名` の形では見つからないことがあります。`find_symbol` で名前を変えて探し、それでも目的の記号の参照が引けなければ、rg で探してソースを読みます。結果の前に `Warning: clangd is still indexing the project.` が付いたときは、結果が欠けていることがあります
