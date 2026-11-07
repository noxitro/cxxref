# C++ のシンボルの検索(clast)

このフォルダの C++ は、libclang で解析した索引を、MCP サーバー `clast` のツールで引けます。C++ の呼び出し箇所・参照・定義を探すときは、ソースを grep する前に、まず clast を使ってください。

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
