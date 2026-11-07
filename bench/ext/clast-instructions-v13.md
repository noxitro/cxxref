# C++ のシンボルの検索(clast)

このフォルダの C++ は、libclang で解析した索引を、MCP サーバー `clast` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず clast を使ってください。

- 引けるもの: 記号の検索(`ast_search`)、定義・シグネチャー(`ast_get_symbol`)、クラスやファイルの外形(`ast_get_outline`)、参照(`ast_get_references`)、継承(`ast_get_hierarchy`)。どのツールも、引数は `params` の中に入れます
- 呼び出し箇所を探すときは、最初に `ast_get_references` を呼びます。引数は `{"params": {"name": "<修飾名>", "limit": 100, "context_lines": 0}}` で、`name` には名前空間から書いた修飾名を渡します(例: `app::render::Renderer::Update`)。`name` は部分一致で探され、当たった記号のうち 1 つ(修飾名が完全に一致するものが優先)の参照だけが返るので、素の名前 `Update` では別のクラスの記号になることがあります
- 結果の読み方:
  - 1 行目 `<修飾名> — N references`: 参照を返した記号(渡した名前と同じかを確かめます)と、件数(`limit` で切る前の数。`limit` の上限は 100)
  - 各参照は 2 行で、`<その参照が書かれている関数や変数の名前>  <path>:<line>` と、その行のソース
  - 参照には呼び出しのほか、`&Class::Method` のような関数ポインターの取得も入り、区別されません。宣言・定義は入りません。同じ行の参照は 1 つにまとめられます。生成されたファイルの行も入るので、問いの範囲に合わせて除きます
- 参照は clang が USR(記号の識別子)で解決したものなので、そのまま答えに使えます。同じ名前を rg でソースから取り直して確かめる必要はありません。コメントの中(`/* */` を含む)の同名は出てきません。rg で見つけた行が結果に無いときは、それがコメントの中か、別のクラスの同名でないかを確かめ、そうなら答えに入れません
- 迷ったとき: 0 件なら、`ast_search`(`{"params": {"query": "<クラス名> <関数名>"}}`)で記号の修飾名を確かめて渡し直します。記号があるのに 0 件のときは、テンプレートの中の呼び出し(テンプレート引数に依存する基底クラスのメンバーを呼ぶ `Base::Clear()` など)が索引に入っていないことがあります。その場合は rg で探し、ソースを読んで呼び出し先を判断します

以下は clast の作者が推奨している指示です(clast の `CLAUDE-CLAST-ADDITION.md` から、そのまま写しました)。

<!-- clast-instructions v3 -->
## C/C++/ObjC code exploration

This project has a semantic AST index (clast) for C-family source files
(.c, .cpp, .cc, .cxx, .m, .mm, .h, .hpp, .hxx). Prefer these tools over
Read/Grep when exploring indexed code:

1. `ast_search` — find symbols by keyword. Start here when you don't know the exact name.
2. `ast_get_symbol` — full definition, signature, and doc comment for a symbol.
3. `ast_get_outline` — class or file interface (signatures, no bodies).
4. `ast_get_references` — call sites / usages. Use `context_lines` for surrounding code.
5. `ast_get_hierarchy` — inheritance tree for a class.

Fall back to Read/Grep/Glob for non-C-family files, config files, or when
editing (read first, then edit). Check coverage with `ast_status`.
<!-- /clast-instructions -->
