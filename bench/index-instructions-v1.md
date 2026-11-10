# C++ 参照索引

`.index/index.tsv` は、clang(libtooling)で全 TU を解析して作った C++ の参照索引です。C++ の呼び出し元・参照・定義を探すときは、ソースを grep する前に、まずこの索引を rg で引いてください。

- 形式: 1 行 1 参照のタブ区切り。列は `修飾名`、`役割`(def / decl / call / write / ref)、`呼び出し元の修飾名`、`path:line:col`、`ソース行`
- 修飾名にテンプレート引数は付きません(`a::Foo<int>::Bar` は `a::Foo::Bar`)
- テンプレートの実体化の中の呼び出しも、解決先の関数で載っています
- 例: `rg "^a::JobSystem::Initialize\tcall\t" .index/index.tsv`
- 修飾名が分からないときは、定義から候補を探します: `rg "::Initialize\tdef\t" .index/index.tsv`
- 索引は Debug|x64 の解析結果です。解析に失敗した TU は `.index/index.tsv.tu.tsv` に `ok=0` で載っており、その TU だけにある参照は索引にありません
