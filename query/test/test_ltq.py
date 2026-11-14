"""ltq の回帰テスト(合成した小さな索引で、構成の統合と鮮度の扱いを確かめる)。

usage: python -I -m unittest discover -s query/test   (リポジトリのルートで)
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ltq  # noqa: E402

# 最後の行(coverage)の、探した範囲の書き方
SCOPE = ' (searched: the root, except folders that start with ".")'

A_CPP = """\
void Run();
void Main() {
  Run();
#ifdef DEBUG_ONLY
  Run();
#endif
  // Run(); in a comment
}
"""


class LtqTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, 'src'))
        os.makedirs(os.path.join(self.root, '.index'))
        self.t0 = time.time() - 1000
        self.write('src/a.cpp', A_CPP, self.t0 - 10)

    def tearDown(self):
        shutil.rmtree(self.root)

    def write(self, rel, text, mtime=None):
        fp = os.path.join(self.root, rel)
        with open(fp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        if mtime is not None:
            os.utime(fp, (mtime, mtime))

    def index(self, name, rows, skips, tus=('src/a.cpp',), mtime=None, deps=None, cp=None, alt=None):
        """deps は {TU: [読んだファイル]}(.deps.tsv)、cp は TU ごとに記録するコードページ。省くと古い形式の索引になる。"""
        base = os.path.join(self.root, '.index', name + '.tsv')
        with open(base, 'w', encoding='utf-8', newline='\n') as f:
            for sym, role, caller, loc, src in rows:
                f.write(f'{sym}\t{role}\t{caller}\t{loc}\t{src}\n')
        with open(base + '.skip.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('path\tkind\tbegin\tend\n')
            for path, kind, b, e in skips:
                f.write(f'{path}\t{kind}\t{b}\t{e}\n')
        with open(base + '.tu.tsv', 'w', encoding='utf-8', newline='\n') as f:
            f.write('file\tok\terrors\tsec\trefs\tfirst_error' + ('\tcommand_hash\tsource_cp\n' if cp else '\n'))
            for t in tus:
                c = cp.get(t) if isinstance(cp, dict) else cp  # cp は全 TU 共通か {TU: コードページ}
                f.write(f'{os.path.join(self.root, t)}\t1\t0\t0.1\t1\t' + (f'\t0\t{c}\n' if cp else '\n'))
        files = [base, base + '.skip.tsv', base + '.tu.tsv']
        if deps is not None:
            with open(base + '.deps.tsv', 'w', encoding='utf-8', newline='\n') as f:
                f.write('tu\tfile\n')
                for t, ds in deps.items():
                    for d in ds:
                        f.write(f'{os.path.join(self.root, t)}\t{d}\n')
            files.append(base + '.deps.tsv')
        if alt is not None:
            # 差分パス(無効な #if の中を解析した行)の <索引>.alt.tsv
            with open(base + '.alt.tsv', 'w', encoding='utf-8', newline='\n') as f:
                for sym, role, caller, loc, src in alt:
                    f.write(f'{sym}\t{role}\t{caller}\t{loc}\t{src}\n')
            files.append(base + '.alt.tsv')
        elif os.path.exists(base + '.alt.tsv'):
            os.remove(base + '.alt.tsv')
        t = self.t0 if mtime is None else mtime
        for p in files:
            os.utime(p, (t, t))

    def ltq(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ltq.main(['--root', self.root] + list(args))
        self.assertEqual(code, 0, out.getvalue())
        return out.getvalue()

    # 共通の行: 3 行目の呼び出しと定義の宣言
    COMMON = [('Run', 'decl', '', 'src/a.cpp:1:6', 'void Run();'),
              ('Run', 'call', 'Main', 'src/a.cpp:3:3', 'Run();')]
    COMMENT = ('src/a.cpp', 'comment', '7:3', '7:24')

    def two_configs(self):
        # debug は 5 行目を解析し、release では 4〜6 行目が無効な #if
        self.index('index', self.COMMON + [('Run', 'call', 'Main', 'src/a.cpp:5:3', 'Run();')], [self.COMMENT])
        self.index('release', self.COMMON, [self.COMMENT, ('src/a.cpp', 'inactive', '4:1', '6:7')])

    def test_single_index_unchanged_header(self):
        self.index('index', self.COMMON, [self.COMMENT])
        out = self.ltq('refs', 'Run')
        self.assertIn('index: .index/index.tsv (built', out)
        self.assertNotIn('configurations', out)
        self.assertNotIn('only in', out)
        self.assertIn('src/a.cpp:3  [in Main]  | Run();', out)
        # 5 行目は索引に無く、無効な #if の範囲も無いので「その他」
        self.assertIn('other -> not explained by the index', out)

    def test_merge_marks_rows_only_in_some_configs(self):
        self.two_configs()
        out = self.ltq('refs', 'Run')
        self.assertIn('index: 2 configurations, merged', out)
        self.assertIn('src/a.cpp:3  [in Main]  | Run();', out)  # 両方にある行は印なし
        self.assertIn('src/a.cpp:5  [in Main]  [only in index]  | Run();', out)
        self.assertIn('== calls (resolved by clang): 2', out)
        self.assertIn('inside comments', out)

    def test_inactive_only_when_inactive_in_every_config(self):
        # 5 行目がどちらの索引にも無い。release だけで無効なら inactive とは言えない
        self.index('index', self.COMMON, [self.COMMENT])
        self.index('release', self.COMMON, [self.COMMENT, ('src/a.cpp', 'inactive', '4:1', '6:7')])
        out = self.ltq('refs', 'Run')
        self.assertNotIn('inside inactive #if', out)
        self.assertIn('other -> not explained by the index', out)
        # 両方で無効なら inactive
        self.index('index', self.COMMON, [self.COMMENT, ('src/a.cpp', 'inactive', '4:1', '6:7')])
        out = self.ltq('refs', 'Run')
        self.assertIn('inside inactive #if/#ifdef branches', out)

    def test_inactive_in_some_configs_keeps_other_kinds(self):
        # debug では捨てられたマクロの引数、release では無効な #if。どの構成でもコンパイルされないので other に落とさない
        self.index('index', self.COMMON, [self.COMMENT, ('src/a.cpp', 'dropped', '5:3', '5:6')])
        self.index('release', self.COMMON, [self.COMMENT, ('src/a.cpp', 'inactive', '4:1', '6:7')])
        out = self.ltq('refs', 'Run')
        self.assertNotIn('other ->', out)
        self.assertIn('inside inactive #if', out)
        # debug でコンパイルされる(どの理由も無い)なら、release の無効な #if だけでは理由にしない
        self.index('index', self.COMMON, [self.COMMENT])
        out = self.ltq('refs', 'Run')
        self.assertIn('other ->', out)
        # どちらの構成でも捨てられた引数なら dropped
        self.index('index', self.COMMON, [self.COMMENT, ('src/a.cpp', 'dropped', '5:3', '5:6')])
        self.index('release', self.COMMON, [self.COMMENT, ('src/a.cpp', 'dropped', '5:3', '5:6')])
        out = self.ltq('refs', 'Run')
        self.assertIn('inside macro arguments the expansion does not keep as code', out)
        # 片方が local なら、無効な #if を外した残りの local で決める
        self.index('index', self.COMMON, [self.COMMENT, ('src/a.cpp', 'local', '5:3', '5:6')])
        self.index('release', self.COMMON, [self.COMMENT, ('src/a.cpp', 'inactive', '4:1', '6:7')])
        out = self.ltq('refs', 'Run')
        self.assertIn('local variables, parameters or template parameters with this name', out)

    def test_unknown_range_kind_is_listed_as_other(self):
        # 新しい ltidx が、この ltq の知らない種類の範囲を書いていても、エラーにせず「その他」に並べる
        # (0.5.1 のサーバーは、後の版の索引の local で KeyError になり、refs が 1 つも返らなかった)
        self.index('index', self.COMMON, [self.COMMENT, ('src/a.cpp', 'future-kind', '5:3', '5:6')])
        out = self.ltq('refs', 'Run')
        self.assertIn('other ->', out)
        self.assertIn('src/a.cpp:5', out)

    def test_compact_lists_dropped_lines(self):
        # 捨てられたマクロの引数(属性やログの引数)は答えに入りうるので、短い形でもフォルダの件数にまとめない
        self.index('index', self.COMMON, [self.COMMENT])
        skips = [self.COMMENT]
        for i in range(12):
            self.write(f'src/m{i}.cpp', 'ATTR(Run())\n', self.t0 - 10)
            skips.append((f'src/m{i}.cpp', 'dropped', '1:6', '1:9'))
        self.index('index', self.COMMON, skips, tus=['src/a.cpp'] + [f'src/m{i}.cpp' for i in range(12)])
        out = self.ltq('refs', 'Run', '--compact')
        self.assertIn('inside macro arguments the expansion does not keep as code', out)
        for i in range(12):
            self.assertIn(f'src/m{i}.cpp:1  | ATTR(Run())', out)

    def test_relocate_after_edit(self):
        self.index('index', self.COMMON + [('Run', 'call', 'Main', 'src/a.cpp:5:3', 'Run();')], [self.COMMENT])
        # 先頭に 2 行足し、5 行目(今は 7 行目)を書き換える
        edited = '// x\n// y\n' + A_CPP.replace('#ifdef DEBUG_ONLY\n  Run();', '#ifdef DEBUG_ONLY\n  Run(); // edited')
        self.write('src/a.cpp', edited)
        out = self.ltq('refs', 'Run')
        self.assertIn('1 source files changed after indexing', out)
        self.assertIn('src/a.cpp:5  [in Main]  [moved from line 3]', out)
        self.assertIn('src/a.cpp:7  [in Main]  [line changed or removed after indexing; estimated from line 5]', out)
        self.assertIn('== calls (resolved by clang): 2', out)  # 同じ文字列の 2 行を 1 行にまとめない
        # コメントの範囲も 2 行ずれる(7 行目のコードをコメントと誤らない)
        self.assertIn('inside comments -> not code; do not report these: 1\n'
                      '    src/a.cpp:9  [file changed after indexing]  | // Run(); in a comment', out)
        self.assertNotIn('other ->', out)
        out = self.ltq('check', 'src/a.cpp:5', 'src/a.cpp:9')
        self.assertIn('indexed: call Run at col 3  [in Main]  [moved from line 3]', out)
        self.assertIn('inside comment 9:3-9:24  (shifted with the nearby indexed lines)', out)

    def test_block_deletion(self):
        # 3 つの関数が同じ文字列の行を持つ。先頭の関数を消しても、残りの行を取り違えない
        body = ''.join(f'void {f}() {{\n  Run();\n  x = 1;\n}}\n' for f in 'ABC')
        self.write('src/b.cpp', body, self.t0 - 10)
        rows = []
        for i, f in enumerate('ABC'):
            rows += [(f, 'def', '', f'src/b.cpp:{4 * i + 1}:6', f'void {f}() {{'),
                     ('Run', 'call', f, f'src/b.cpp:{4 * i + 2}:3', 'Run();')]
        self.index('index', rows, [], tus=('src/b.cpp',))
        self.write('src/b.cpp', body.split('}\n', 1)[1])
        out = self.ltq('refs', 'Run')
        self.assertIn('src/b.cpp:2  [in B]  [moved from line 6]', out)
        self.assertIn('src/b.cpp:6  [in C]  [moved from line 10]', out)
        self.assertRegex(out, r'src/b.cpp:\d+  \[in A\]  \[line changed or removed after indexing')

    def test_header_without_comments_is_tracked(self):
        # コメントが無く TU の主ファイルでもないヘッダー(索引の行だけがある)も、変わったファイルとして扱う
        self.write('src/h.h', 'void Run();\n', self.t0 - 10)
        self.index('index', self.COMMON + [('Run', 'decl', '', 'src/h.h:1:6', 'void Run();')], [self.COMMENT])
        self.write('src/h.h', '#pragma once\nvoid Run();\n')
        out = self.ltq('refs', 'Run')
        self.assertIn('    src/h.h\n', out)
        self.assertIn('src/h.h:2  [moved from line 1]  | void Run();', out)
        self.assertNotIn('not read by any indexed configuration', out)

    def test_newer_config_wins(self):
        # debug は編集前、release は編集後に作り直した。debug の推定と release の確かな行が同じ参照なら release を採る
        self.write('src/n.cpp', 'void Main() {\n  Go();\n}\n', self.t0 - 10)
        self.index('index', [('Main', 'def', '', 'src/n.cpp:1:6', 'void Main() {'),
                             ('Go', 'call', 'Main', 'src/n.cpp:2:3', 'Go();')], [], tus=('src/n.cpp',))
        self.write('src/n.cpp', '// top\nvoid Main() {\n  Go(); // now\n}\n')
        self.index('release', [('Main', 'def', '', 'src/n.cpp:2:6', 'void Main() {'),
                               ('Go', 'call', 'Main', 'src/n.cpp:3:3', 'Go(); // now')], [],
                   tus=('src/n.cpp',), mtime=time.time() + 10)
        out = self.ltq('refs', 'Go')
        self.assertIn('== calls (resolved by clang): 1\n  call   src/n.cpp:3  [in Main]  | Go(); // now', out)
        # 推定した行が別の行に落ちても、release に同じ参照があれば捨てる
        self.index('index', [('Main', 'def', '', 'src/n.cpp:1:6', 'void Main() {'),
                             ('Go', 'call', 'Main', 'src/n.cpp:5:3', 'Go();')], [], tus=('src/n.cpp',))
        out = self.ltq('refs', 'Go')
        self.assertIn('== calls (resolved by clang): 1\n', out)

    def test_new_and_deleted_files(self):
        self.write('src/b.cpp', 'void Run() {}\n', self.t0 - 10)
        self.index('index', self.COMMON + [('Run', 'def', '', 'src/b.cpp:1:6', 'void Run() {}')], [self.COMMENT],
                   tus=('src/a.cpp', 'src/b.cpp'))
        os.remove(os.path.join(self.root, 'src', 'b.cpp'))
        self.write('src/c.cpp', 'void Other() {}\n')
        out = self.ltq('refs', 'Run')
        # 名前を含まない新しいファイルは件数だけ(refs のたびに関係の無いファイルを並べない)
        self.assertIn('1 source files modified after indexing are not read by any indexed configuration;'
                      ' none of them contains the name', out)
        self.assertNotIn('src/c.cpp', out)
        self.write('src/d.cpp', 'void G() { Run(); }\n')
        out = self.ltq('refs', 'Run')
        self.assertIn('2 source files modified after indexing are not read by any indexed configuration'
                      ' (new files? rebuild the index if they are compiled). Those that contain the name (1):\n'
                      '    src/d.cpp\n    ... 1 more that do not contain the name', out)
        self.assertIn('1 indexed TU files no longer exist: src/b.cpp', out)
        self.assertIn('src/b.cpp:1  [file deleted after indexing]', out)

    def test_virtual_dispatch(self):
        # Derived::Draw は Base::Draw を上書きし、Leaf::Draw は Derived::Draw を上書きする
        src = ('struct Base { virtual void Draw(); };\n'
               'struct Derived : Base { void Draw() override; };\n'
               'struct Leaf : Derived { void Draw() override; };\n'
               'void Call(Base* b, Derived* d, Leaf* l) { b->Draw(); d->Draw(); l->Draw(); }\n')
        self.write('src/v.cpp', src, self.t0 - 10)
        self.index('index', [
            ('Base::Draw', 'decl', '', 'src/v.cpp:1:28', 'struct Base { virtual void Draw(); };'),
            ('Derived::Draw', 'decl', '', 'src/v.cpp:2:30', 'struct Derived : Base { void Draw() override; };'),
            ('Derived::Draw', 'override', 'Base::Draw', 'src/v.cpp:2:30', 'struct Derived : Base { void Draw() override; };'),
            ('Leaf::Draw', 'decl', '', 'src/v.cpp:3:29', 'struct Leaf : Derived { void Draw() override; };'),
            ('Leaf::Draw', 'override', 'Derived::Draw', 'src/v.cpp:3:29', 'struct Leaf : Derived { void Draw() override; };'),
            ('Base::Draw', 'call', 'Call', 'src/v.cpp:4:46', src.splitlines()[3]),
            ('Derived::Draw', 'call', 'Call', 'src/v.cpp:4:57', src.splitlines()[3]),
            ('Leaf::Draw', 'call', 'Call', 'src/v.cpp:4:68', src.splitlines()[3]),
        ], [], tus=('src/v.cpp',))
        out = self.ltq('refs', 'Leaf::Draw')
        self.assertIn('== calls (resolved by clang): 1\n', out)
        self.assertIn('the query overrides Base::Draw, Derived::Draw.', out)
        self.assertIn(': 2 (2 [dynamic])\n    call   src/v.cpp:4  Base::Draw  [in Call]  [dynamic]', out)
        self.assertIn('[dynamic] calls to those may run the query at runtime', out)
        self.assertIn('  (virtual calls, resolved by clang: 1 [dynamic]: no qualifier; the override for the receiver\'s'
                      ' runtime type runs)\n', out)
        self.assertNotIn('(exact:', out)  # 仮想関数では、形ごとの数の行が代わり
        self.assertNotIn('overridden by', out)
        self.assertNotIn('other symbols named', out)  # 上書きの関係にある記号は virtual dispatch の節に出す
        out = self.ltq('refs', 'Base::Draw')
        self.assertIn('== calls (resolved by clang): 1\n', out)
        self.assertNotIn('the query overrides', out)
        # 上書き 1 つが 1 行(宣言と定義の場所)。その下に、上書きへ解決された呼び出し
        self.assertIn('change them with the query): 2\n    Derived::Draw  decl src/v.cpp:2\n    Leaf::Draw  decl src/v.cpp:3\n', out)
        self.assertIn('  calls resolved to the overrides (they call the override, not the query; a signature change reaches'
                      ' them too): 2 (2 [dynamic])\n    call   src/v.cpp:4  Derived::Draw  [in Call]  [dynamic]', out)
        self.assertIn('(the query or one of its 2 overrides)', out)
        self.assertNotIn('\n  override ', out.split('== virtual dispatch')[0])  # override 行は他の節に混ぜない
        out = self.ltq('check', 'src/v.cpp:2')
        self.assertIn('indexed: override Derived::Draw at col 30  [overrides Base::Draw]', out)

    def test_virtual_call_kinds(self):
        # 仮想関数の呼び出しの形: 限定名が無ければ [dynamic](実行時に受け手の型の上書きが動く)、Base::f()・Super::f() は
        # [static](その関数そのもの)。上書きの中からなら [super](上書きから基底の実装を呼ぶもの)。索引の印(call-dynamic・
        # call-static)があればそれで、無ければ(前の ltidx の索引)行の文面で決める(1 行に名前が 2 つあればソースの行と列で)。
        # 仮想でない関数の呼び出しには付けない
        lines = ['struct Base { virtual void Tick(int); void Plain(); };',
                 'struct Mid : Base { void Tick(int n) override { Base::Tick(n); } };',
                 'struct Leaf : Mid { using Super = Mid; void Tick(int n) override { Super::Tick(n); Tick(0); } };',
                 'void Run(Base& b, Base* p) { b.Tick(1); p->Plain(); }',
                 'void Old(Base* p) { p->Tick(2); }',
                 'void Mixed(Base* p) { p->Tick(3); p->Base::Tick(4); }',
                 '#define CALL_TICK(p) p->Tick(5)',
                 'void Macro(Base* p) { CALL_TICK(p); }']
        self.write('src/k.cpp', '\n'.join(lines) + '\n', self.t0 - 10)
        self.index('index', [
            ('Base::Tick', 'decl', '', 'src/k.cpp:1:28', lines[0]),
            ('Base::Plain', 'decl', '', 'src/k.cpp:1:45', lines[0]),
            ('Mid::Tick', 'def', '', 'src/k.cpp:2:26', lines[1]),
            ('Mid::Tick', 'override', 'Base::Tick', 'src/k.cpp:2:26', lines[1]),
            ('Base::Tick', 'call', 'Mid::Tick', 'src/k.cpp:2:55', lines[1]),
            ('Leaf::Tick', 'def', '', 'src/k.cpp:3:45', lines[2]),
            ('Leaf::Tick', 'override', 'Mid::Tick', 'src/k.cpp:3:45', lines[2]),
            ('Mid::Tick', 'call-static', 'Leaf::Tick', 'src/k.cpp:3:75', lines[2]),  # 印があれば文面を見ない
            ('Leaf::Tick', 'call-dynamic', 'Leaf::Tick', 'src/k.cpp:3:84', lines[2]),
            ('Base::Tick', 'call-dynamic', 'Run', 'src/k.cpp:4:32', lines[3]),
            ('Base::Tick', 'call', 'Run', 'src/k.cpp:4:32', lines[3]),  # -update で残った前の ltidx の行(同じ呼び出し)
            ('Base::Plain', 'call', 'Run', 'src/k.cpp:4:45', lines[3]),
            ('Base::Tick', 'call', 'Old', 'src/k.cpp:5:24', lines[4]),  # 前の ltidx の行(印が無い)
            ('Base::Tick', 'call', 'Mixed', 'src/k.cpp:6:44', lines[5]),  # 名前が 2 つある。ソースの行と列で決める
            ('Base::Tick', 'call', 'Macro', 'src/k.cpp:8:23', lines[7]),  # マクロの中。行に名前が無い
        ], [], tus=('src/k.cpp',))
        out = self.ltq('refs', 'Base::Tick')
        self.assertIn('  call   src/k.cpp:2  [in Mid::Tick]  [super]  | struct Mid', out)
        self.assertIn('  call   src/k.cpp:4  [in Run]  [dynamic]  | void Run', out)  # 役割の -dynamic は見せない
        self.assertEqual(out.count('src/k.cpp:4  [in Run]'), 1)  # 印の有無だけ違う同じ呼び出しは 1 行
        self.assertIn('  call   src/k.cpp:5  [in Old]  [dynamic]  | void Old', out)
        self.assertIn('  call   src/k.cpp:6  [in Mixed]  [static]  | void Mixed', out)
        self.assertIn('  call   src/k.cpp:8  [in Macro]  | void Macro', out)
        self.assertIn('  (virtual calls, resolved by clang: 2 [dynamic]: no qualifier; the override for the receiver\'s'
                      ' runtime type runs (the query or one of its 2 overrides); 1 [static]: Base::f(), the query itself runs;'
                      ' 1 [super]: Super::f() or Base::f() inside an override, calling up to the query; 1 not told apart)\n',
                      out)
        # 上書きへの呼び出し: Super::Tick は Leaf::Tick(Mid::Tick の上書き)の中から。クラスの中の Tick(0) は this 越し
        self.assertIn('too): 2 (1 [dynamic]; 1 [super])\n', out)
        self.assertIn('    call   src/k.cpp:3  Mid::Tick  [in Leaf::Tick]  [super]', out)
        self.assertIn('    call   src/k.cpp:3  Leaf::Tick  [in Leaf::Tick]  [dynamic]', out)
        # 仮想でない関数
        out = self.ltq('refs', 'Base::Plain')
        self.assertIn('  call   src/k.cpp:4  [in Run]  | void Run', out)
        self.assertNotIn('virtual calls', out)
        # 上書きの側から問うと、基底への呼び出しも形で分ける(Base::Tick(n) は問い合わせを呼ばない)
        out = self.ltq('refs', 'Mid::Tick')
        self.assertIn('they are not counted above: 5 (2 [dynamic]; 1 [static]; 1 [super]; 1 not told apart)\n', out)

    def family(self, n=12, far=0):
        """Base::Tick と、それを上書きする D00..::Tick(宣言は src/d.h、定義と Base::Tick の呼び出しは src/d.cpp)。
        far 行の、別の記号に解決されたテンプレートの中の呼び出し(src/t.h)も足す。"""
        self.write('src/d.h', 'struct Base { virtual void Tick(int); };\n'
                   + ''.join(f'struct D{i:02} : Base {{ void Tick(int) override; }};\n' for i in range(n)), self.t0 - 10)
        self.write('src/d.cpp', ''.join(f'void D{i:02}::Tick(int v) {{ Base::Tick(v); }}\n' for i in range(n))
                   + 'void All(D00* d) { d->Tick(1); }\n', self.t0 - 10)
        self.write('src/t.h', 'template <class T> void Each(T& t) {\n' + '  t.Tick(0);\n' * far + '}\n', self.t0 - 10)
        rows = [('Base::Tick', 'decl', '', 'src/d.h:1:28', 'struct Base { virtual void Tick(int); };')]
        for i in range(n):
            decl = f'struct D{i:02} : Base {{ void Tick(int) override; }};'
            body = f'void D{i:02}::Tick(int v) {{ Base::Tick(v); }}'
            rows += [(f'D{i:02}::Tick', 'decl', '', f'src/d.h:{i + 2}:26', decl),
                     (f'D{i:02}::Tick', 'override', 'Base::Tick', f'src/d.h:{i + 2}:26', decl),
                     (f'D{i:02}::Tick', 'def', '', f'src/d.cpp:{i + 1}:11', body),
                     ('Base::Tick', 'call', f'D{i:02}::Tick', f'src/d.cpp:{i + 1}:31', body)]
        rows += [('D00::Tick', 'call', 'All', f'src/d.cpp:{n + 1}:23', 'void All(D00* d) { d->Tick(1); }')]
        for k in range(far):
            rows += [('?::Tick', 'call-dep', 'Each', f'src/t.h:{k + 2}:5', 't.Tick(0);'),
                     ('Other::Tick', 'call', 'Each', f'src/t.h:{k + 2}:5', 't.Tick(0);')]
        self.index('index', rows, [], tus=('src/d.cpp',))

    def test_override_family_is_one_line_per_override(self):
        # 上書き 1 つを 1 行にして、宣言と定義の場所を並べる(引数を変えるときの直す先。別の節に 1 行ずつ出すと倍になる)
        self.family()
        out = self.ltq('refs', 'Base::Tick')
        self.assertIn('change them with the query): 12\n    D00::Tick  decl src/d.h:2  def src/d.cpp:1\n', out)
        self.assertIn('    D11::Tick  decl src/d.h:13  def src/d.cpp:12\n', out)
        self.assertIn('reaches them too): 1 (1 [dynamic])\n    call   src/d.cpp:13  D00::Tick  [in All]  [dynamic]'
                      '  | void All(D00* d) { d->Tick(1); }\n', out)
        # 上書きの中の Base::Tick(v) は、上書きから基底の実装を呼ぶもの(実行時の型に関係なく、問い合わせそのものが動く)
        self.assertIn('  call   src/d.cpp:1  [in D00::Tick]  [super]  | void D00::Tick(int v)', out)
        self.assertIn('  (virtual calls, resolved by clang: 12 [super]: Super::f() or Base::f() inside an override, calling'
                      ' up to the query)\n', out)
        self.assertNotIn('more overrides', out)
        # 上書きの宣言と定義の行は、1 行ずつ並べた行に数える(名前を含む行は 1 + 12 + 12 + 1 で、全部)
        self.assertTrue(out.endswith('\n== coverage: all 26 lines that contain the word "Tick" are listed above, one per line'
                                     + SCOPE + '\n'), out[-300:])
        # --family: 並べる上書きの数と、上書きへの呼び出しの数。他の節はそのまま
        out = self.ltq('refs', 'Base::Tick', '--family', '2')
        self.assertIn('    D01::Tick  decl src/d.h:3  def src/d.cpp:2\n    ... 10 more overrides (the last line says where the'
                      ' rest is listed, with the calls to them; asking refs about each override adds nothing)\n', out)
        self.assertNotIn('D02::Tick  decl', out)
        self.assertIn('== calls (resolved by clang): 12\n', out)
        self.assertIn('  call   src/d.cpp:12  [in D11::Tick]', out)
        self.assertIn('\n== coverage: 16 of 26 lines', out)  # 並べなかったのは、上書き 10 個の宣言の行(定義は呼び出しと同じ行)
        self.assertTrue(out.endswith(ltq.ALL_HINT + '\n'))
        # 短い形は 10 まで(--family が勝つ)、--all は全部
        out = self.ltq('refs', 'Base::Tick', '--compact')
        self.assertIn('    D09::Tick  decl src/d.h:11  def src/d.cpp:10\n    ... 2 more overrides', out)
        self.assertIn('D11::Tick  decl', self.ltq('refs', 'Base::Tick', '--compact', '--family', '12'))
        self.assertIn('D11::Tick  decl', self.ltq('refs', 'Base::Tick', '--all', '--family', '1'))

    def refs_and_rest(self, *args):
        """refs の結果と、その結果に並べなかった行の一覧(MCP サーバーがファイルに書くもの)。"""
        idx = ltq.Index(ltq.find_indexes(self.root), self.root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ltq.main(['--root', self.root, 'refs'] + list(args), idx=idx), 0)
        shown, n_all, n_listed = idx.last_refs
        rest = io.StringIO()
        with contextlib.redirect_stdout(rest):
            self.assertEqual(ltq.main(['--root', self.root, 'refs'] + list(args) + ['--all'], idx=idx, skip=shown), 0)
        return out.getvalue(), rest.getvalue(), n_all, n_listed

    def positions(self, text, family=True):
        """文面に 1 行ずつ出ている位置(パス:行)。上書きの行は宣言と定義の 2 つ(family が偽なら入れない)。"""
        if not family:
            return re.findall(r'^ +(?:\w[\w-]* +)?((?:src|lib)/[\w./]+:\d+)', text, re.M)
        return re.findall(r'(?:^ +(?:\w[\w-]* +)?| (?:decl|def) )((?:src|lib)/[\w./]+:\d+)', text, re.M)

    def test_rest_lists_each_line_the_result_left_out_once(self):
        # 残りの一覧(skip): 結果が 1 行ずつ並べた行を省いて、残りを全部並べる。結果と合わせると、名前を含む行がどれも 1 回ずつ出る
        self.family(far=3)
        self.write('src/c.cpp', '// Tick in a comment\n' * 4, self.t0 - 10)
        self.index('index2', [], [('src/c.cpp', 'comment', f'{k}:1', f'{k}:21') for k in range(1, 5)], tus=('src/c.cpp',))
        out, rest, n_all, n_listed = self.refs_and_rest('Base::Tick', '--compact', '--compact', '--family', '2', '--limit', '3')
        self.assertEqual(n_all, 26 + 3 + 4)
        self.assertLess(n_listed, n_all)
        self.assertTrue(rest.startswith('query: Base::Tick\n  (the lines that contain the name and are not listed in the refs'
                                        ' result, one per line; with that result, every line is listed once)\n'), rest[:300])
        self.assertNotIn('\nindex: ', rest)  # 索引の状態は結果にある
        a, b = self.positions(out), self.positions(rest)
        self.assertEqual(len(set(a)), n_listed)
        self.assertEqual(len(set(a) | set(b)), n_all)
        # 上書きの行(宣言と定義の場所を 1 行に並べる。定義の行は、呼び出しの行と同じことがある)のほかは、重ならない
        a, b = self.positions(out, False), self.positions(rest, False)
        self.assertFalse(set(a) & set(b))
        self.assertEqual(len(b), len(set(b)))
        self.assertIn('    D02::Tick  decl src/d.h:4  def src/d.cpp:3\n', rest)
        self.assertNotIn('D01::Tick  decl', rest)
        self.assertIn('    src/c.cpp:4  | // Tick in a comment\n', rest)
        self.assertTrue(rest.endswith(f'\n== coverage: {n_all - n_listed} lines here + {n_listed} in the refs result = all'
                                      f' {n_all} lines that contain the word "Tick", each listed once' + SCOPE + '\n'), rest[-300:])
        # 問い合わせの記号に結び付いた節が先、名前を含むだけの行の節(ltq.WORD_ONLY)が後ろ
        heads = [x for x in rest.splitlines() if x.startswith('== ')]
        word = [x.startswith(ltq.WORD_ONLY) for x in heads[:-1]]
        self.assertEqual(word, sorted(word), heads)
        # 実体化で別の記号に解決されたテンプレートの中の行は、名前を含むだけの行の側に並べる(直す先の一覧に混ぜない)
        self.assertIn('3 of them are resolved by instantiations to other symbols -> probably not the query: Other::Tick (3)'
                      ' (listed one per line under "lines the index resolves to other symbols")\n', rest)
        tail = rest[rest.index('== lines the index resolves to other symbols named "Tick"'):]
        self.assertIn('  src/t.h:3  ?::Tick, Other::Tick  | t.Tick(0);\n', tail)
        self.assertNotIn('src/t.h', rest[:rest.index('== lines the index resolves to other symbols')])
        # 全部並んだ結果の残りは、空(見出しと coverage だけ)
        out, rest, n_all, n_listed = self.refs_and_rest('Base::Tick', '--all')
        self.assertEqual(n_all, n_listed)
        self.assertEqual(self.positions(rest), [])
        self.assertIn(f'== coverage: 0 lines here + {n_all} in the refs result = all {n_all} lines', rest)

    def test_shift_jis_source(self):
        # ltidx は UTF-8 でないソースを記録したコードページ(932)で UTF-8 に直して解析する。列番号は直した後のバイト位置
        src = 'void Run();\nvoid Main() {\n  // 表\n  Run();\n  const char* s = "表"; Run();\n  /* 表表 */ Run();\n}\n'
        fp = os.path.join(self.root, 'src', 's.cpp')
        with open(fp, 'wb') as f:
            f.write(src.encode('cp932'))
        os.utime(fp, (self.t0 - 10, self.t0 - 10))
        self.write('src/u1.cpp', '\n', self.t0 - 10)
        self.write('src/u2.cpp', '\n', self.t0 - 10)
        # /utf-8 の TU(65001)が多くても、Shift_JIS で読んだ TU があればそのコードページで直す
        self.index('index', [('Run', 'decl', '', 'src/s.cpp:1:6', 'void Run();'),
                             ('Run', 'call', 'Main', 'src/s.cpp:4:3', 'Run();')],
                   [('src/s.cpp', 'comment', '3:3', '3:9'), ('src/s.cpp', 'comment', '6:3', '6:15')],
                   tus=('src/s.cpp', 'src/u1.cpp', 'src/u2.cpp'),
                   cp={'src/s.cpp': 932, 'src/u1.cpp': 65001, 'src/u2.cpp': 65001},
                   deps={'src/s.cpp': ['src/s.cpp']})
        out = self.ltq('refs', 'Run')
        self.assertNotIn('configurations', out)  # .deps.tsv を構成として拾わない
        # 5・6 行目は索引から外してあるので「その他」に出る。6 行目の Run は UTF-8 の列で 16 なのでコメント(3〜15)の外。
        # Shift_JIS のバイト位置(14)で比べると、コメントの中と誤る
        self.assertIn('other -> not explained by the index; read them if they matter: 2\n'
                      '    src/s.cpp:5  | const char* s = "表"; Run();\n'
                      '    src/s.cpp:6  | /* 表表 */ Run();', out)
        self.assertNotIn('inside comments', out)
        # 「表」の 2 バイト目は `\`(0x5C)。直さずに読むと、索引の後に変わったファイルの行の付け直しも外れる
        with open(fp, 'wb') as f:
            f.write(('// top\n' + src).encode('cp932'))
        out = self.ltq('refs', 'Run')
        self.assertIn('src/s.cpp:5  [in Main]  [moved from line 4]', out)
        self.assertEqual(ltq.to_utf8('表'.encode('cp932'), 'cp932'), '表'.encode('utf-8'))
        self.assertEqual(ltq.to_utf8('表'.encode('utf-16'), None), '表'.encode('utf-8'))
        self.assertEqual(ltq.to_utf8(b'\x95\x5c', None), b'\x95\x5c')  # コードページの記録が無い古い索引ではそのまま

    def test_deps_count_headers_without_rows(self):
        # 参照もコメントも無いヘッダーも、.deps.tsv にあれば「どの構成も読んでいない新しいファイル」にしない
        self.write('src/empty.h', '#pragma once\n', self.t0 - 10)
        self.index('index', self.COMMON, [self.COMMENT], deps={'src/a.cpp': ['src/a.cpp', 'src/empty.h']})
        self.write('src/empty.h', '#pragma once\n// edited\n')
        out = self.ltq('refs', 'Run')
        self.assertIn('1 source files changed after indexing, none of them contains the name', out)
        self.assertNotIn('not read by any indexed configuration', out)
        out = self.ltq('check', 'src/empty.h:1')  # 問われたファイルは並べる
        self.assertIn('    src/empty.h\n', out)
        self.assertNotIn('not read by any indexed configuration', out)
        self.assertIn('index: .index/index.tsv (built', out)  # .deps.tsv を 2 つ目の構成として拾わない
        self.assertNotIn('configurations', out)

    def test_deps_list_is_cached_by_size_and_mtime(self):
        # .deps.tsv の大きさと更新日時が合う間は、隣の .seen.cache の一覧を使う(.deps.tsv は読まない)
        self.write('src/empty.h', '#pragma once\n// edited\n')  # 索引より新しい
        self.index('index', self.COMMON, [self.COMMENT], deps={'src/a.cpp': ['src/a.cpp']})
        dp = os.path.join(self.root, '.index', 'index.tsv.deps.tsv')
        self.assertEqual(ltq.deps_paths(dp), {b'src/a.cpp'})
        cache = os.path.join(self.root, '.index', 'index.tsv.seen.cache')
        with open(cache, 'rb') as f:
            key = f.readline()
        with open(cache, 'wb') as f:  # 鍵はそのままで、一覧だけを書き換える
            f.write(key + b'src/a.cpp\nsrc/empty.h\n')
        out = self.ltq('refs', 'Run')
        self.assertIn('1 source files changed after indexing', out)  # 一覧にあるので「新しいファイル」ではない
        self.assertNotIn('not read by any indexed configuration', out)
        os.utime(dp, (self.t0 + 1, self.t0 + 1))  # 更新日時が変われば .deps.tsv を読み直す
        self.assertEqual(ltq.deps_paths(dp), {b'src/a.cpp'})

    def test_grep_lines(self):
        # 改行で始まる needle は行頭に当たる。ファイルが改行で始まっても止まらない。CRLF の行も改行を除いて返す
        fp = os.path.join(self.root, 'g.tsv')
        with open(fp, 'wb') as f:
            f.write(b'\nLoad\tx\r\nfoo Load\tz\nLoad\ty')
        self.assertEqual(ltq.grep_lines(fp, [b'\nLoad\t']), [b'Load\tx', b'Load\ty'])
        self.assertEqual(ltq.grep_lines(fp, [b'Load\t', b'\nLoad\t']), [b'Load\tx', b'foo Load\tz', b'Load\ty'])
        with open(fp, 'wb') as f:
            f.write(b'Load\ta\n')
        self.assertEqual(ltq.grep_lines(fp, [b'\nLoad\t']), [b'Load\ta'])
        with open(fp, 'wb'):
            pass
        self.assertEqual(ltq.grep_lines(fp, [b'Load']), [])

    def test_explicit_index_option_is_repeatable(self):
        self.two_configs()
        d = os.path.join(self.root, '.index')
        out = self.ltq('--index', os.path.join(d, 'release.tsv'), 'refs', 'Run')
        self.assertIn('index: .index/release.tsv', out)
        self.assertNotIn('only in', out)
        out = self.ltq('--index', os.path.join(d, 'index.tsv'), '--index', os.path.join(d, 'release.tsv'), 'refs', 'Run')
        self.assertIn('[only in index]', out)

    def test_hint_for_files_not_in_the_index(self):
        # 索引に無いファイル(別プラットフォーム向けの写し)の一致に、同じ形の行の解決先を添える
        self.write('src/w.cpp', 'void A(); void F(Dev* g) {\n  g->Make(1);\n  q.Make();\n}\n', self.t0 - 10)
        os.makedirs(os.path.join(self.root, 'src', 'mac'))
        self.write('src/mac/m.mm', 'void F(Dev* g) {\n  g->Make(2);\n  q.Make();\n  Make();\n}\n', self.t0 - 10)
        self.index('index', [('Dev::Make', 'call', 'F', 'src/w.cpp:2:6', 'g->Make(1);'),
                             ('Base::Make', 'call', 'F', 'src/w.cpp:3:5', 'q.Make();'),
                             ('Base::Make', 'decl', '', 'src/w.cpp:1:1', 'void Make();')],
                   [], tus=('src/w.cpp',))
        out = self.ltq('refs', 'Base::Make')
        # 同じ文面の行は query に解決されている -> 呼び出し
        self.assertIn('src/mac/m.mm:3  (the same line text elsewhere is a call to the query -> probably a call)', out)
        # 文面は違うが、受け手の式 g-> が別の先に解決されている -> 弱い手がかり
        self.assertIn('src/mac/m.mm:2  (`g->Make` elsewhere resolves to Dev::Make -> probably not the query;'
                      ' check the receiver type)', out)
        # 受け手の無い呼び出しには付けない
        self.assertRegex(out, r'src/mac/m\.mm:4  \| Make\(\);')

    def test_compact_summarizes_low_value_sections(self):
        # 短い形: 呼び出しは全部出し、コメント内の一致はフォルダごとの件数にする
        self.index('index', self.COMMON, [self.COMMENT])
        full = self.ltq('refs', 'Run')
        out = self.ltq('refs', 'Run', '--compact')
        self.assertIn('(compact output:', out)
        self.assertIn('src/a.cpp:3  [in Main]  | Run();', out)
        self.assertIn('src/a.cpp:7', full)
        self.assertIn('inside comments -> not code; do not report these: 1\n    src/  1 matches in 1 files', out)
        self.assertNotIn('src/a.cpp:7', out)

    def test_compact_keeps_lines_that_spell_the_qualified_name(self):
        # 修飾名で問われたら、無効な #if の中でも `Scope::Name` と書かれた行は、いちばん短い形(--compact を 2 つ)でも残す
        # (O3DE の DynamicModuleHandle::Create では正解の 2 つがここにあった)。名前だけの行はフォルダの件数にする
        self.write('src/q.cpp', 'void F() {\n#if 0\n  ns::Run();\n  x.Run();\n#endif\n}\n', self.t0 - 10)
        self.index('index', [('ns::Run', 'decl', '', 'src/a.cpp:1:6', 'void Run();'),
                             ('ns::Run', 'call', 'Main', 'src/a.cpp:3:3', 'Run();')],
                   [('src/q.cpp', 'inactive', '2:1', '5:7')], tus=('src/a.cpp', 'src/q.cpp'))
        out = self.ltq('refs', 'ns::Run', '--compact', '--compact')
        self.assertIn('lines that spell ns::Run are listed in full', out)
        self.assertRegex(out, r'inside inactive #if/#ifdef branches -> not compiled in the indexed configuration[^\n]*: 2\n'
                              r'    \(index\.tsv\.alt\.tsv is missing[^\n]*\n'
                              r'    src/q\.cpp:3  \[#if 0\]  \| ns::Run\(\);\n    src/  1 matches in 1 files\n')
        self.assertNotIn('src/q.cpp:4', out)
        self.assertIn('ask again with --path <folder>', out)
        out = self.ltq('refs', 'Run', '--compact', '--compact')  # 修飾されていない問いでは、どちらも件数
        self.assertNotIn('src/q.cpp:3', out)

    def test_compact_lists_inactive_lines_one_by_one(self):
        # 短い形の 1 段目(--compact 1 つ)は、無効な #if の中の一致を 1 行ずつ出す。名前や引数を変えるときに直す行なので、
        # rg で見つかる行が件数に埋もれないようにする。まとめるのはコメントなど、コードでない分類だけ
        self.write('src/q.cpp', 'void F() {\n#if 0\n  ns::Run();\n  x.Run();\n#endif\n}\n', self.t0 - 10)
        self.index('index', self.COMMON, [self.COMMENT, ('src/q.cpp', 'inactive', '2:1', '5:7')],
                   tus=('src/a.cpp', 'src/q.cpp'))
        out = self.ltq('refs', 'Run', '--compact')
        self.assertIn('other text matches up to 30 per group', out)
        self.assertIn('    src/q.cpp:3  [#if 0]  | ns::Run();\n    src/q.cpp:4  [#if 0]  | x.Run();\n', out)
        self.assertIn('inside comments -> not code; do not report these: 1\n    src/  1 matches in 1 files', out)

    def test_compact_lists_up_to_30_lines_per_group(self):
        # 1 段目でも、1 つの分類で 30 行を超えた分はフォルダごとの件数にする(--path で出し直せると案内する)
        self.index('index', self.COMMON, [self.COMMENT])
        os.makedirs(os.path.join(self.root, 'other'))
        self.write('other/x.cpp', 'void G() {\n' + '  x.Run();\n' * 40 + '}\n', self.t0 - 10)
        out = self.ltq('refs', 'Run', '--compact')
        self.assertIn('in files no indexed TU includes -> not analyzed; read them if they matter: 40\n'
                      '    other/x.cpp:2  | x.Run();\n', out)
        self.assertIn('    other/x.cpp:31  | x.Run();\n    other/  10 matches in 1 files\n', out)
        self.assertNotIn('other/x.cpp:32', out)
        self.assertIn('ask again with --path <folder>', out)
        out = self.ltq('refs', 'Run', '--compact', '--path', 'other')  # フォルダを絞れば、残りも並ぶ分だけ減る
        self.assertIn('(--path: only lines in other are listed and counted)', out)
        self.assertNotIn('src/a.cpp', out.split('== calls', 1)[1])

    def test_compact_lists_only_the_folders_with_most_matches(self):
        # 名前だけの一致は大きなコードでは数百のフォルダに散る(O3DE の Create で 180)。多い順に 10 まで、残りは 1 行
        self.index('index', self.COMMON, [self.COMMENT])
        for i in range(12):
            os.makedirs(os.path.join(self.root, 'other', f'd{i:02}'))
            self.write(f'other/d{i:02}/x.cpp', 'void G() {\n' + '  x.Run();\n' * (20 - i) + '}\n', self.t0 - 10)
        out = self.ltq('refs', 'Run', '--compact', '--compact')
        self.assertIn('in files no indexed TU includes -> not analyzed; read them if they matter: 174\n'
                      '    other/d00/  20 matches in 1 files\n', out)
        # 残りのフォルダも、上の階層ごとの件数は出す(どこにも名前の出ない行を作らない)
        self.assertIn('    other/d09/  11 matches in 1 files\n'
                      '    ... 2 more folders (19 matches) under: other/d10/ 10, other/d11/ 9\n', out)
        self.assertNotIn('other/d10/  ', out)

    def test_folders_beyond_the_listed_ones_are_counted_by_parent_folder(self):
        # 並べきれないフォルダは、上の階層でまとめる。--path で絞った先でも、分かれるところまで階層を下りる
        self.assertEqual(ltq.top_dirs(['a/x/f.cpp', 'a/x/g.cpp', 'a/y/f.cpp', 'b/z/f.cpp', 'c/f.cpp'], 1),
                         ['a/x/  2 matches in 2 files', '... 3 more folders (3 matches) under: a/ 1, b/ 1, c/ 1'])
        self.assertEqual(ltq.top_dirs(['a/x/f.cpp', 'a/x/g.cpp', 'a/y/p/f.cpp', 'a/z/f.cpp', 'a/z/q/f.cpp'], 1),
                         ['a/x/  2 matches in 2 files', '... 3 more folders (3 matches) under: a/z/ 2, a/y/ 1'])
        many = [f'd{i:02}/f.cpp' for i in range(12)]
        self.assertTrue(ltq.top_dirs(many, 1)[1].endswith('d10/ 1, d11/ 1'))  # 上の階層は数を切らない

    def test_non_source_files_are_searched_by_name(self):
        # C/C++ でないファイル(生成器・ビルドの定義・文書)の一致も出す。中身は解析しないが、rg なら出る行を無いことにしない
        self.index('index', self.COMMON, [self.COMMENT])
        os.makedirs(os.path.join(self.root, 'tools', 'gen'))
        self.write('tools/gen/Gen.cs', 'class Gen {\n  void Emit() { w.Write("Run();"); }\n}\n', self.t0 - 10)
        self.write('tools/build.cmake', '# Runner is another word\n' + 'add_step(Run)\n' * 13, self.t0 - 10)
        # テキストかどうかは、rg と同じく中身で見分ける(拡張子や大きさでは除かない)
        with open(os.path.join(self.root, 'tools', 'blob.dat'), 'wb') as f:
            f.write(b'\0\1\2 Run \0')  # 先頭に NUL のあるファイル(バイナリ)には、探す部分が無い
        with open(os.path.join(self.root, 'tools', 'mixed.bin'), 'wb') as f:
            f.write(b'Run in the text part\n\0\1 Run \n')  # 最初の NUL より前だけを探す(rg は NUL に当たるまでの一致を出す)
        self.write('tools/icon.png', 'Run\n')  # 拡張子はバイナリでも、中身がテキスト(Git LFS の実体を取っていないファイルなど)
        head = ('== matches of "Run" in files that are not C/C++ sources (scripts, build files, docs, other languages'
                ' -> not analyzed; update them too when renaming): ')
        out = self.ltq('refs', 'Run')
        self.assertIn(head + '16\n', out)
        self.assertIn('  tools/gen/Gen.cs:2  | void Emit() { w.Write("Run();"); }\n', out)
        self.assertIn('  tools/build.cmake:14  | add_step(Run)\n', out)
        self.assertIn('  tools/icon.png:1  | Run\n', out)
        self.assertIn('  tools/mixed.bin:1  | Run in the text part\n', out)
        self.assertNotIn('tools/mixed.bin:2', out)
        self.assertNotIn('tools/build.cmake:1 ', out)
        self.assertNotIn('blob.dat', out)
        self.assertNotIn('matches in', out)
        self.assertIn('== coverage: all 20 lines that contain the word "Run" are listed above, one per line' + SCOPE + '\n', out)
        # 短い形の 1 段目は 10 行まで並べ、残りはフォルダごとの件数。2 段目は全部フォルダごとの件数
        sec = self.ltq('refs', 'Run', '--compact').split(head, 1)[1]
        self.assertEqual(len(re.findall(r'^  tools/\S+:\d+  \| ', sec, re.M)), 10)
        self.assertEqual(sum(int(n) for n in re.findall(r'/  (\d+) matches in', sec)), 6)
        self.assertIn('ask again with --path <folder>', sec)
        sec = self.ltq('refs', 'Run', '--compact', '--compact').split(head, 1)[1]
        self.assertIn('16\n  tools/  15 matches in 3 files\n  tools/gen/  1 matches in 1 files\n', sec)
        self.assertNotIn('  | ', sec)
        # --path で絞れば、短い形でもその先の行が並ぶ
        sec = self.ltq('refs', 'Run', '--compact', '--compact', '--path', 'tools/gen').split(head, 1)[1]
        self.assertTrue(sec.startswith('1\n'))
        out = self.ltq('refs', 'Run', '--compact', '--path', 'tools/gen')
        self.assertIn('  tools/gen/Gen.cs:2  | ', out)
        self.assertNotIn('build.cmake', out)
        # --exclude はこの節にも効く
        self.assertNotIn(head, self.ltq('refs', 'Run', '--exclude', 'tools'))

    def test_word_starts_matches_the_regex(self):
        # 単語の位置を find で拾う部品は、正規表現の finditer と同じ位置を返す(前後が英数字・_ の一致は拾わない)
        import random
        rnd = random.Random(7)
        for sb in (b'ab', b'a', b'aa', b'_a1', b'operator==', b'~ab'):
            pat = re.compile(rb'(?<![A-Za-z0-9_])' + re.escape(sb) + rb'(?![A-Za-z0-9_])')
            for _ in range(300):
                data = bytes(rnd.choice(b'ab_1 =~.\n\x80') for _ in range(rnd.randrange(0, 60)))
                data = rnd.choice((b'', sb)) + data + rnd.choice((b'', sb))
                self.assertEqual(list(ltq.word_starts(data, sb)), [m.start() for m in pat.finditer(data)], (sb, data))

    def test_name_without_a_word_is_refused(self):
        # 名前の最後の部分が空("Foo::"・"::"・"")だと、探す単語が無い。空の名前はどの位置にも一致するので、
        # 断らないと、単語の位置を拾う部品が終わらない
        self.assertEqual(list(ltq.word_starts(b' a b', b'')), [])
        self.assertEqual(list(ltq.word_starts(b'', b'')), [])
        self.index('index', self.COMMON, [self.COMMENT])
        for name in ('Foo::', '::', ''):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = ltq.main(['--root', self.root, 'refs', name])
            self.assertEqual(code, 2, name)
            self.assertEqual(out.getvalue(), f'refs needs a name after the last "::" (got "{name}")\n')

    def test_names_with_symbols_are_found_across_a_cut_in_a_long_line(self):
        # 長い 1 行を途中で分けるとき、分け目は単語の途中でないだけなので、記号や空白を含む名前は分け目をまたぐ。
        # どこで分かれても、名前を含む行は正規表現で全体を探したときと同じになる
        import random
        from unittest import mock
        rnd = random.Random(11)
        p = os.path.join(self.root, 'long.txt')
        with mock.patch.multiple(ltq, OTHER_HEAD=32, READ_CHUNK=16, LONG_LINE=32):
            for sb in (b'operator==', b'~Foo', b'operator new', b'a.b', b'Foo'):
                pat = re.compile(rb'(?<![A-Za-z0-9_])' + re.escape(sb) + rb'(?![A-Za-z0-9_])')
                for _ in range(150):
                    data = b''.join(rnd.choice((sb, sb[:-1], b'x', b'_', b' ', b'=', b'~', b'.', b'ab ', b'= ', b'\n'))
                                    if rnd.random() < 0.3 else bytes([rnd.choice(b'ab1 =~.')])
                                    for _ in range(rnd.randrange(1, 120)))
                    with open(p, 'wb') as f:
                        f.write(data)
                    out, skipped = ltq.other_scan(([('long.txt', p)], sb, None))
                    want = [i for i, line in enumerate(data.split(b'\n'), 1) if pat.search(line)]
                    self.assertEqual([ln for _, ln, _ in out], want, (sb, data))
                    self.assertEqual(skipped, [])

    def test_file_that_grew_after_its_size_was_read_is_read_in_chunks(self):
        # 大きさを見た後に伸びたファイル(書いている途中のログ)も最後まで読む。伸びた分を 1 バイトずつは読まない
        from unittest import mock
        p = os.path.join(self.root, 'growing.log')
        data = b'line\n' * 4000
        with open(p, 'wb') as f:
            f.write(data)
        reads = []

        class Counting:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.f.close()

            def fileno(self):
                return self.f.fileno()

            def read(self, n=-1):
                reads.append(n)
                return self.f.read(n)

        class Stat:
            st_size = ltq.OTHER_HEAD + 100  # 実際より小さい(見た後に伸びた)

        with mock.patch.object(ltq, 'open', lambda fp, mode: Counting(open(fp, mode)), create=True), \
                mock.patch.object(ltq.os, 'fstat', lambda fd: Stat):
            got = list(ltq.other_chunks(p))
        self.assertEqual(b''.join(d for _, d in got), data)
        self.assertLess(len(reads), 8, reads)

    def test_files_that_cannot_be_read_are_reported(self):
        # 読めなかったファイル(ほかのプログラムが開いている・権限が無い)の中の行は、出せない。黙って「全部」と言わず、
        # どのファイルが読めなかったかを書く。一覧を作った後に消えたファイルは、rg でも出ないので書かない
        from unittest import mock
        self.index('index', self.COMMON, [self.COMMENT])
        self.write('src/b.cpp', 'void F() { Run(); }\n', self.t0 - 10)
        self.write('src/gone.cpp', 'void G() { Run(); }\n', self.t0 - 10)
        self.write('locked.txt', 'Run\n', self.t0 - 10)
        self.write('notes.txt', 'Run\n', self.t0 - 10)
        base = self.ltq('refs', 'Run')
        self.assertNotIn('not searched', base)
        self.assertTrue(base.endswith(SCOPE + '\n'))
        n_base = int(re.search(r'== coverage: all (\d+) lines', base).group(1))

        def fake(fp, *a, **k):
            name = os.path.basename(fp)
            if name in ('b.cpp', 'locked.txt'):
                raise PermissionError(13, 'used by another process', fp)
            if name == 'gone.cpp':
                raise FileNotFoundError(2, 'No such file', fp)
            return open(fp, *a, **k)

        with mock.patch.object(ltq, 'open', fake, create=True):
            out = self.ltq('refs', 'Run')
            src = self.ltq('refs', 'Run', '--path', 'src')
        self.assertIn('\n== not searched: 2 files could not be read (in use by another program, or no permission); lines in'
                      ' them may be missing from the list and from the count below: locked.txt, src/b.cpp\n', out)
        self.assertTrue(out.endswith(f'== coverage: all {n_base - 3} lines that contain the word "Run" are listed above, one per'
                                     ' line (searched: the root, except folders that start with ".", 2 files that could not'
                                     ' be read)\n'), out[-300:])
        self.assertIn('notes.txt:1  | Run\n', out)
        # --path で絞れば、その先の読めなかったファイルだけを書く
        self.assertIn('\n== not searched: 1 file could not be read (', src)
        self.assertIn('count below: src/b.cpp\n', src)
        self.assertTrue(src.endswith(', 1 file that could not be read)\n'), src[-200:])

    def test_utf16_and_long_lines_are_read_in_parts(self):
        # UTF-16(BOM 付き)も分けて読み、UTF-8 に直して返す(全部を一度にメモリに載せない)
        p = os.path.join(self.root, 'wide.txt')
        text = ''.join(f'行 {i} filler\n' for i in range(900)) + 'Run ここ\n' + 'tail\n' * 20
        with open(p, 'wb') as f:
            f.write(text.encode('utf-16'))
        got = list(ltq.other_chunks(p, 63))  # 奇数: 2 バイトの単位の途中で分かれる
        self.assertGreater(len(got), 10)
        self.assertEqual(b''.join(d for _, d in got).decode('utf-8'), text)
        seen = 1
        for first, data in got:
            self.assertEqual(first, seen)
            seen += data.count(b'\n')
        # 行の区切りが無いまま大きくなったら、単語の途中でない所で分ける(1 行だけの大きなファイルを貯め続けない)
        q = os.path.join(self.root, 'oneline.json')
        one = b' '.join(b'token_%d' % i for i in range(3000)) + b' Run and Run end'
        with open(q, 'wb') as f:
            f.write(one)
        self.index('index', self.COMMON, [self.COMMENT])
        saved = ltq.READ_CHUNK, ltq.LONG_LINE
        ltq.READ_CHUNK, ltq.LONG_LINE = 64, 256
        try:
            got = list(ltq.other_chunks(q))
            out = self.ltq('refs', 'Run')
        finally:
            ltq.READ_CHUNK, ltq.LONG_LINE = saved
        self.assertGreater(len(got), 10)
        self.assertEqual(b''.join(d for _, d in got), one)
        self.assertEqual({first for first, _ in got}, {1})
        self.assertTrue(all(d.endswith(b' ') for _, d in got[:-1]))
        self.assertEqual(out.count('  oneline.json:1  | '), 1)  # 同じ行の一致は、分かれても 1 行
        self.assertIn('  wide.txt:901  | Run ここ\n', out)
        self.assertEqual(len(re.findall(r'^  (?:oneline\.json|wide\.txt):', out, re.M)), 2)

    def test_index_files_under_the_root_are_not_searched(self):
        # 索引がテキスト検索の読む所(ルートの下の、. で始まらないフォルダ)にあっても、索引のファイル自体は探さない
        # (索引の行はソースの行の写し。探すと、名前を含む行の数が増える)。隣の、索引でないファイルは探す
        self.index('index', self.COMMON, [self.COMMENT])
        base = self.ltq('refs', 'Run')
        self.assertIn(SCOPE + '\n', base)
        out_dir = os.path.join(self.root, 'out')
        shutil.copytree(os.path.join(self.root, '.index'), out_dir)
        self.write('out/notes.txt', 'Run\n', self.t0 - 10)
        self.write('out/index.tsv.words.cache', 'Run\n', self.t0 - 10)
        self.write('out/index.tsv.words.cache.1234.tmp', 'Run\n', self.t0 - 10)  # 写しを書いている途中のファイル
        # 索引のパスで始まるだけの、索引でないファイルは探す
        self.write('out/index.tsv.md', 'Run\n', self.t0 - 10)
        self.write('out/index.tsvx.txt', 'Run\n', self.t0 - 10)
        out = self.ltq('--index', os.path.join(out_dir, 'index.tsv'), 'refs', 'Run')
        for p in ('index.tsv', 'index.tsv.skip.tsv', 'index.tsv.tu.tsv', 'index.tsv.words.cache',
                  'index.tsv.words.cache.1234.tmp'):
            self.assertNotIn(f'out/{p}:', out)
        for p in ('notes.txt', 'index.tsv.md', 'index.tsvx.txt'):
            self.assertIn(f'  out/{p}:1  | Run\n', out)

        def total(text):
            return int(re.search(r'== coverage: all (\d+) lines', text).group(1))

        self.assertEqual(total(out), total(base) + 3)
        self.assertTrue(out.endswith(' (searched: the root, except folders that start with ".", the index files)\n'))
        # --exclude で除いた所も、最後の行に書く
        out = self.ltq('refs', 'Run', '--exclude', 'out', '--exclude', 'gen')
        self.assertTrue(out.endswith(' (searched: the root, except folders that start with ".", --exclude gen, out)\n'))

    def test_large_non_source_files_are_read_in_parts(self):
        # 大きなファイルも、行の区切りで分けて全部読む(大きさで除かない)。行番号は通しで数える
        p = os.path.join(self.root, 'big.txt')
        lines = [b'filler line %d' % i for i in range(2000)]
        lines[70] = b'Run here'
        lines[1500] = b'x' * 300 + b' Run'  # 1 回に読む大きさより長い行
        with open(p, 'wb') as f:
            f.write(b'\n'.join(lines))  # 最後の行は改行で終わらない
        got = list(ltq.other_chunks(p, 64))
        self.assertGreater(len(got), 10)
        self.assertEqual(b''.join(d for _, d in got), b'\n'.join(lines))
        seen = 1
        for first, data in got:
            self.assertEqual(first, seen)
            self.assertTrue(data.endswith(b'\n') or data is got[-1][1])
            seen += data.count(b'\n')
        self.index('index', self.COMMON, [self.COMMENT])
        chunk, ltq.READ_CHUNK = ltq.READ_CHUNK, 64
        try:
            out = self.ltq('refs', 'Run')
        finally:
            ltq.READ_CHUNK = chunk
        self.assertIn('  big.txt:71  | Run here\n', out)
        self.assertIn('  big.txt:1501  | xxx', out)
        self.assertEqual(out, self.ltq('refs', 'Run'))  # 1 つで読んだときと同じ

    def test_all_lists_every_line_that_contains_the_name(self):
        # --all は、名前を含む行(rg -w なら出る行)を 1 行も件数にまとめずに並べる(名前やシグネチャを変えるときの、直す先の一覧)。
        # ふつうの出力は、最後の行で「名前を含む行のうち何行を 1 行ずつ並べたか」を言う
        os.makedirs(os.path.join(self.root, 'lib'))
        # lib/b.cpp: 2〜6 行目は別の記号 Other::Run の呼び出し、7〜18 行目は文字列の中(12 行。全文でも件数にまとめる)
        self.write('lib/b.cpp', 'void Use(Other* o) {\n' + '  o->Run();\n' * 5 + '  Log("Run");\n' * 12 + '}\n', self.t0 - 10)
        self.write('lib/notes.md', 'Run\n' * 3, self.t0 - 10)
        rows = [('Mine::Run', 'decl', '', 'src/a.cpp:1:6', 'void Run();'),
                ('Mine::Run', 'call', 'Main', 'src/a.cpp:3:3', 'Run();')]
        rows += [('Other::Run', 'call', 'Use', f'lib/b.cpp:{n}:6', 'o->Run();') for n in range(2, 7)]
        # 無効な #if の中(5 行目)を仮に有効にした解析が、別の記号に解決した行
        self.index('index', rows, [self.COMMENT, self.INACTIVE], tus=('src/a.cpp', 'lib/b.cpp'),
                   alt=[('Other::Run', 'call', 'Main', 'src/a.cpp:5:3', 'Run();')])
        out = self.ltq('refs', 'Mine::Run')
        self.assertIn('== other symbols named "Run" (not the query): Other::Run (6)\n', out)
        self.assertNotIn('lib/b.cpp:2', out)
        self.assertNotIn('src/a.cpp:5', out)
        self.assertTrue(out.endswith('\n== coverage: 6 of 24 lines that contain the word "Run" are listed above, one per line' + SCOPE +
                                     '; the rest are only counted (refs --all lists every line)\n'), out[-300:])
        out = self.ltq('refs', 'Mine::Run', '--compact', '--compact', '--limit', '1')
        self.assertIn('\n== coverage: 2 of 24 lines', out)
        out = self.ltq('refs', 'Mine::Run', '--all', '--limit', '1')  # --all は --limit より強い
        self.assertIn('(--all: every line that contains the name is listed', out)
        self.assertIn('== lines the index resolves to other symbols named "Run" (not the query; listed so that no line'
                      ' that contains the name is left out): 6\n', out)
        self.assertIn('  lib/b.cpp:2  Other::Run  | o->Run();\n', out)
        self.assertIn('  src/a.cpp:5  Other::Run  [inactive: #if defined(DEBUG_ONLY)]  | Run();\n', out)
        for n in range(7, 19):
            self.assertIn(f'    lib/b.cpp:{n}  | Log("Run");\n', out)
        self.assertIn('  lib/notes.md:3  | Run\n', out)
        self.assertNotIn('matches in', out)
        self.assertNotIn(' more ', out)
        self.assertTrue(out.endswith('\n== coverage: all 24 lines that contain the word "Run" are listed above, one per line' + SCOPE + '\n'))
        # --path と合わせると、そのフォルダの行だけを数えて、全部並べる
        out = self.ltq('refs', 'Mine::Run', '--all', '--path', 'lib')
        self.assertTrue(out.endswith('\n== coverage: all 20 lines that contain the word "Run" are listed above, one per line'
                                     ' (searched: --path lib, except folders that start with ".")\n'))

    def test_matched_symbols_are_capped_by_row_count(self):
        # 短い名前が多くの記号に当たっても、記号の一覧は行の多い順に上限まで。残りは件数(行は下の節に記号つきで出る)
        rows = [(f'n{i:02}::Run', 'decl', '', f'src/a.cpp:{i + 1}:6', 'void Run();') for i in range(30)]
        rows += [('n29::Run', 'call', 'Main', 'src/a.cpp:3:3', 'Run();')]
        self.index('index', rows, [self.COMMENT])
        line = next(s for s in self.ltq('refs', 'Run').splitlines() if s.startswith('  matched symbols: '))
        self.assertTrue(line.startswith('  matched symbols: n29::Run (2), n00::Run (1), '))
        self.assertEqual(line.count('::Run ('), ltq.SYM_LIST)
        self.assertIn(f', ... {30 - ltq.SYM_LIST} more symbols ({30 - ltq.SYM_LIST} rows;', line)

    def test_unparsed_files_are_listed_line_by_line_in_the_full_output(self):
        # どの TU も読まないファイルの一致はコードでありうる。全文では 10 行を超えても 1 行ずつ出す(rg と同じ見え方)
        self.index('index', self.COMMON, [self.COMMENT])
        os.makedirs(os.path.join(self.root, 'other'))
        self.write('other/x.cpp', 'void G() {\n' + '  x.Run();\n' * 15 + '}\n', self.t0 - 10)
        out = self.ltq('refs', 'Run')
        self.assertIn('read them if they matter: 15\n    other/x.cpp:2  | x.Run();\n', out)
        self.assertIn('    other/x.cpp:16  | x.Run();\n', out)
        self.assertNotIn('matches in', out)

    def test_lines_over_the_limit_are_counted_by_folder(self):
        # --limit で並べきれない行は、フォルダごとの件数で残す(「あと N 行」だけにして、どこにあるか分からない行を作らない)
        os.makedirs(os.path.join(self.root, 'lib'))
        self.write('lib/b.cpp', 'void Use() {\n' + '  Run();\n' * 5 + '}\n', self.t0 - 10)
        os.makedirs(os.path.join(self.root, 'other'))
        self.write('other/x.cpp', 'void G() {\n' + '  x.Run();\n' * 6 + '}\n', self.t0 - 10)
        self.write('other/notes.md', 'Run\n' * 4, self.t0 - 10)
        self.index('index', self.COMMON + [('Run', 'call', 'Use', f'lib/b.cpp:{n}:3', 'Run();') for n in range(2, 7)],
                   [self.COMMENT], tus=('src/a.cpp', 'lib/b.cpp'))
        out = self.ltq('refs', 'Run', '--limit', '2')
        calls = out.split('== calls (resolved by clang): 6\n', 1)[1].split('\n== ', 1)[0]
        self.assertEqual(len(re.findall(r'^  call   ', calls, re.M)), 2)
        self.assertIn('  ... 4 more (raise --limit, or ask with --path <folder>), by folder:\n', calls)
        self.assertEqual(sum(int(n) for n in re.findall(r'/  (\d+) matches in', calls)), 4)
        self.assertIn('read them if they matter: 6\n    other/x.cpp:2  | x.Run();\n    other/x.cpp:3  | x.Run();\n'
                      '    other/  4 matches in 1 files\n', out)
        self.assertIn('  other/notes.md:2  | Run\n  other/  2 matches in 1 files\n', out)
        self.assertIn('ask again with --path <folder>', out)
        # いちばん短い形でも同じ: 上限で切れるのは 1 行ずつの行だけで、フォルダごとの件数は必ず残る
        out = self.ltq('refs', 'Run', '--compact', '--compact', '--limit', '1')
        self.assertIn('read them if they matter: 6\n    other/x.cpp:2  | x.Run();\n    other/  5 matches in 1 files\n', out)
        self.assertNotIn('more (raise --limit)\n', out)

    def test_other_limit_keeps_resolved_calls(self):
        # --other-limit は、索引が問い合わせに解決しなかった行の節だけを減らす(解決済みの呼び出しは --limit のまま)。
        # MCP サーバーは、呼び出しを減らす前にこちらを減らす。一覧(--all)では効かない
        os.makedirs(os.path.join(self.root, 'lib'))
        self.write('lib/b.cpp', 'void Use() {\n' + '  Run();\n' * 5 + '}\n', self.t0 - 10)
        os.makedirs(os.path.join(self.root, 'other'))
        self.write('other/x.cpp', 'void G() {\n' + '  x.Run();\n' * 6 + '}\n', self.t0 - 10)
        self.write('other/notes.md', 'Run\n' * 4, self.t0 - 10)
        self.index('index', self.COMMON + [('Run', 'call', 'Use', f'lib/b.cpp:{n}:3', 'Run();') for n in range(2, 7)],
                   [self.COMMENT], tus=('src/a.cpp', 'lib/b.cpp'))
        out = self.ltq('refs', 'Run', '--other-limit', '2')
        calls = out.split('== calls (resolved by clang): 6\n', 1)[1].split('\n== ', 1)[0]
        self.assertEqual(len(re.findall(r'^  call   ', calls, re.M)), 6)
        self.assertNotIn('more (raise --limit', calls)
        self.assertIn('read them if they matter: 6\n    other/x.cpp:2  | x.Run();\n    other/x.cpp:3  | x.Run();\n'
                      '    other/  4 matches in 1 files\n', out)
        self.assertIn('  other/notes.md:2  | Run\n  other/  2 matches in 1 files\n', out)
        # --limit の方が小さければ、そちらが効く
        out = self.ltq('refs', 'Run', '--other-limit', '4', '--limit', '1')
        self.assertIn('read them if they matter: 6\n    other/x.cpp:2  | x.Run();\n    other/  5 matches in 1 files\n', out)
        # 一覧では効かない
        out = self.ltq('refs', 'Run', '--all', '--other-limit', '1')
        for n in range(2, 8):
            self.assertIn(f'other/x.cpp:{n}  | x.Run();', out)

    def test_path_limits_the_lines_but_not_the_symbol(self):
        # --path は並べる行だけを絞る。記号は索引の全体から決める(絞った先に宣言が無くても、同じ記号として引く)
        os.makedirs(os.path.join(self.root, 'lib'))
        self.write('lib/b.cpp', 'void Run();\nvoid Use() {\n  Run();\n}\n', self.t0 - 10)
        self.index('index', self.COMMON + [('Run', 'call', 'Use', 'lib/b.cpp:3:3', 'Run();')], [self.COMMENT],
                   tus=('src/a.cpp', 'lib/b.cpp'))
        out = self.ltq('refs', 'Run', '--path', 'lib')
        self.assertIn('symbol: Run\n', out)
        self.assertIn('== calls (resolved by clang): 1\n  call   lib/b.cpp:3  [in Use]  | Run();', out)
        self.assertNotIn('src/a.cpp:', out)
        out = self.ltq('refs', 'Run', '--path', '.\\LIB\\b.cpp', '--path', 'src/a.cpp')  # 区切りと大文字小文字は問わない。複数可
        self.assertIn('lib/b.cpp:3', out)
        self.assertIn('src/a.cpp:3', out)
        out = self.ltq('refs', 'Run', '--path', 'li')  # フォルダ名の途中までは一致にしない
        self.assertNotIn('lib/b.cpp:3', out)

    INACTIVE = ('src/a.cpp', 'inactive', '4:1', '6:7')
    ALT_ROW = ('Run', 'call', 'Main', 'src/a.cpp:5:3', 'Run();')

    def test_alt_rows_are_listed_with_the_condition(self):
        # 差分パスが無効な #if の中を解析した行(.alt.tsv)は、解決済みの行として並べ、その条件を付ける
        self.index('index', self.COMMON, [self.COMMENT, self.INACTIVE], alt=[self.ALT_ROW])
        out = self.ltq('refs', 'Run')
        self.assertIn('(a line tagged [inactive: #if ...] is code the indexed configuration does not compile', out)
        self.assertNotIn('lines below', out)  # 数を書くと、モデルがその行を探しにいく
        self.assertIn('== calls (resolved by clang): 2', out)
        self.assertIn('  call   src/a.cpp:5  [in Main]  [inactive: #if defined(DEBUG_ONLY)]  | Run();', out)
        self.assertNotIn('inside inactive #if', out)  # 索引の行になったので、テキストの一致には残らない
        self.assertEqual(self.ltq('refs', 'Run', '--compact').count('src/a.cpp:5'), 1)  # 短い形でも省かない
        out = self.ltq('check', 'src/a.cpp:5')
        self.assertIn('indexed: call Run at col 3  [in Main]  [inside an inactive #if, parsed with the condition'
                      ' forced on: #if defined(DEBUG_ONLY)]', out)

    def test_without_alt_file_the_inactive_group_says_so(self):
        # .alt.tsv が無い索引(古い索引、-NoAlt)では、無効な #if の中は解析されていないと知らせる
        self.index('index', self.COMMON, [self.COMMENT, self.INACTIVE])
        out = self.ltq('refs', 'Run')
        self.assertIn('(index.tsv.alt.tsv is missing or older than the index, so these branches were not parsed;', out)
        self.assertIn('    src/a.cpp:5  [#if defined(DEBUG_ONLY)]', out)
        self.index('index', self.COMMON, [self.COMMENT, self.INACTIVE], alt=[])  # 差分パスは走ったが、この行は解析できなかった
        out = self.ltq('refs', 'Run')
        self.assertIn('inside inactive #if/#ifdef branches', out)
        self.assertNotIn('alt.tsv', out)

    def test_alt_file_older_than_the_index_is_not_used(self):
        # 本体を作り直した後(自動更新・-Update・差分パスの失敗)に残った古い差分は、行番号が合わないので使わない。
        # 無効な行は文字列の一致として出る
        self.index('index', self.COMMON, [self.COMMENT, self.INACTIVE], alt=[self.ALT_ROW])
        alt = os.path.join(self.root, '.index', 'index.tsv.alt.tsv')
        os.utime(alt, (self.t0 - 100, self.t0 - 100))
        out = self.ltq('refs', 'Run')
        self.assertNotIn('[inactive: #if', out)
        self.assertIn('== calls (resolved by clang): 1\n', out)
        self.assertIn('(index.tsv.alt.tsv is missing or older than the index', out)
        self.assertIn('    src/a.cpp:5  [#if defined(DEBUG_ONLY)]  | Run();', out)

    B_CPP = ('struct Base { virtual void Run(); };\n'      # 1
             'struct Mine : Base { void Run() override; };\n'  # 2
             '#if PLATFORM_X\n'                            # 3
             '  // Run the thing\n'                        # 4 コメント
             '  /* Run */\n'                               # 5 コメント
             '   * Run more\n'                             # 6 ブロックコメントの続きの行
             '  LOG("Run");\n'                             # 7 文字列
             'void Other::Run() {}\n'                      # 8 別のクラスの修飾名
             '  ns::Other::Run();\n'                       # 9 名前空間付きでも、名前の直前のクラスで見る
             '  Super::Run();\n'                           # 10 基底を指す別名 -> 族
             '  Base::Run();\n'                            # 11 族のクラス
             '  x->Run();\n'                               # 12 手がかり無し
             '  Other::Run(); Run();\n'                    # 13 修飾の無い名前もある -> 決めない
             '  *p = Run();\n'                             # 14 `*` で始まってもコード
             '#endif\n')                                   # 15

    def inactive_b(self, alt):
        self.write('src/b.cpp', self.B_CPP, self.t0 - 10)
        self.index('index', [('Base::Run', 'decl', '', 'src/b.cpp:1:28', 'struct Base { virtual void Run(); };'),
                             ('Mine::Run', 'decl', '', 'src/b.cpp:2:27', 'struct Mine : Base { void Run() override; };'),
                             ('Mine::Run', 'override', 'Base::Run', 'src/b.cpp:2:27',
                              'struct Mine : Base { void Run() override; };')],
                   [('src/b.cpp', 'inactive', '3:1', '15:7')], tus=('src/b.cpp',), alt=alt)

    def test_inactive_lines_in_comments_strings_or_other_classes(self):
        # 無効な #if の中は clang が字句を読まないので、索引にコメントや文字列の範囲が無い。文面の見当で分けて、
        # 「直す先かもしれない無効な行」の件数を、本当に確かめが要る行だけにする(UE5 の Initialize で 186 行のうち
        # 半分近くがコメント、3 割が別のクラスの修飾名だった)
        for alt in (None, []):  # 差分の無い索引でも、差分パスが解析できなかった行でも同じ
            self.inactive_b(alt)
            out = self.ltq('refs', 'Mine::Run', '--all')
            comment = out.split('inside comments')[1].split('\n  inside')[0]
            for ln in (4, 5, 6):
                self.assertIn(f'src/b.cpp:{ln}', comment)
            self.assertIn('src/b.cpp:7', out.split('inside string literals')[1])
            inactive = out.split('inside inactive #if')[1].split('\n  inside')[0]
            for ln in (4, 5, 6, 7):
                self.assertNotIn(f'src/b.cpp:{ln} ', inactive)
            self.assertIn('src/b.cpp:8  [#if PLATFORM_X]  (written as Other::Run, a class outside the overrides'
                          ' -> probably not the query)  | void Other::Run() {}', inactive)
            self.assertIn('src/b.cpp:9  [#if PLATFORM_X]  (written as Other::Run', inactive)
            for ln in (10, 11, 12, 13, 14):
                self.assertIn(f'src/b.cpp:{ln}  [#if PLATFORM_X]  | ', inactive)
            # 短い形では、別のクラスの行は「別の記号らしい」の件数に入り、残りだけが確かめの要る行になる
            out = self.ltq('refs', 'Mine::Run', '--compact', '--compact')
            self.assertIn('    2 look like other symbols\n', out)
            self.assertIn('inside inactive #if/#ifdef branches -> not compiled in the indexed configuration, not resolved;'
                          ' still code to update when renaming or changing a signature: 8', out)

    def test_utf16_sources_are_searched(self):
        # UTF-16(BOM 付き)のソースも、UTF-8 に直してから名前を探す(rg は BOM 付き UTF-16 を直して探す)
        self.index('index', self.COMMON, [self.COMMENT])
        with open(os.path.join(self.root, 'src', 'u16.cpp'), 'wb') as f:
            f.write('void X() {\n  Run();\n}\n'.encode('utf-16'))
        out = self.ltq('refs', 'Run')
        self.assertIn('src/u16.cpp:2', out)

    def test_absolute_path_is_taken_relative_to_the_root(self):
        # MCP のクライアントは root を絶対パスで渡すので、path も絶対で来る。ルートの下なら相対にして使う
        self.index('index', self.COMMON, [self.COMMENT])
        out = self.ltq('refs', 'Run', '--path', os.path.join(self.root, 'src'))
        self.assertIn('call   src/a.cpp:3', out)

    def test_one_folder_is_counted_by_file(self):
        # 1 つのフォルダに収まるときは、ファイルごとの件数にする(--path でそのフォルダを渡しても、同じ件数に戻らない)
        self.assertEqual(ltq.top_dirs(['t/a.cpp', 't/a.cpp', 't/b.cpp'], 5), ['t/a.cpp  2 matches', 't/b.cpp  1 matches'])
        self.assertEqual(ltq.top_dirs(['t/a.cpp', 't/b.cpp', 't/c.cpp'], 1),
                         ['t/a.cpp  1 matches', '... 2 more files (2 matches): b.cpp 1, c.cpp 1'])
        self.assertEqual(ltq.top_dirs(['t/a.cpp', 't/a.cpp'], 5), ['t/  2 matches in 1 files'])  # 1 ファイルならフォルダのまま

    def test_compact_keeps_the_folders_of_lines_that_resolve_elsewhere(self):
        # 短い形で「別の記号らしい」行を件数にまとめても、どのフォルダにあるかは残す(rg なら出る行)
        os.makedirs(os.path.join(self.root, 'other'))
        self.write('other/x.cpp', 'void G() {\n' + '  g->Run();\n' * 12 + '}\n', self.t0 - 10)
        self.index('index', [('Mine::Run', 'decl', '', 'src/a.cpp:1:6', 'void Run();'),
                             ('Other::Run', 'call', 'H', 'src/a.cpp:9:6', 'g->Run();')], [self.COMMENT])
        out = self.ltq('refs', 'Mine::Run', '--compact', '--compact')
        self.assertIn('look like other symbols', out)
        self.assertIn('other/  12 matches in 1 files', out)

    def test_alt_row_does_not_duplicate_an_indexed_row(self):
        # 同じ位置が本体の索引にもあれば、本体の行だけにする(索引の後で差分だけが古くなっても、二重にしない)
        self.index('index', self.COMMON + [self.ALT_ROW], [self.COMMENT], alt=[self.ALT_ROW])
        out = self.ltq('refs', 'Run')
        self.assertIn('== calls (resolved by clang): 2', out)
        self.assertIn('  call   src/a.cpp:5  [in Main]  | Run();', out)
        self.assertNotIn('[inactive', out)

    def test_alt_row_is_dropped_when_another_config_compiles_the_line(self):
        # release では無効で差分パスが解析した行も、debug がコンパイルしていれば debug の行を使う
        self.index('index', self.COMMON + [self.ALT_ROW], [self.COMMENT])
        self.index('release', self.COMMON, [self.COMMENT, self.INACTIVE], alt=[self.ALT_ROW])
        out = self.ltq('refs', 'Run')
        self.assertIn('  call   src/a.cpp:5  [in Main]  [only in index]  | Run();', out)
        self.assertNotIn('[inactive', out)
        # debug がその行で別の記号に解決しているなら、差分の行(条件を無理に有効にした推定)は出さない
        self.index('index', self.COMMON + [('Other::Run', 'call', 'Main', 'src/a.cpp:5:3', 'Run();')], [self.COMMENT])
        out = self.ltq('refs', 'Run')
        self.assertNotIn('[inactive', out)
        self.assertNotIn('src/a.cpp:5  [in Main]', out.split('== text matches')[0].replace('Other::Run', ''))

    def test_inactive_condition_names_the_branch(self):
        # 条件は、その行を無効にしている #if の枝を外側から並べる。インクルードガードのように有効な枝は書かない
        self.write('src/c.h', '#ifndef C_H\n#define C_H\n#if defined(_WIN32)\nvoid W();\n#elif defined(__linux__)\n'
                              '#  ifdef USE_X\ninline void L() { Run(); }\n#  else\ninline void M() { Run(); }\n#  endif\n'
                              '#else\ninline void E() { Run(); }\n#endif\n#endif\n', self.t0 - 10)
        self.index('index', self.COMMON, [self.COMMENT, ('src/c.h', 'inactive', '5:1', '13:7')],
                   deps={'src/a.cpp': ['src/c.h']})
        out = self.ltq('refs', 'Run')
        self.assertIn('    src/c.h:7  [#if defined(__linux__) [and not: defined(_WIN32)] > #if defined(USE_X)]  | inline void L()', out)
        self.assertIn('    src/c.h:9  [#if defined(__linux__) [and not: defined(_WIN32)] > #if !(defined(USE_X))]  | inline void M()', out)
        self.assertIn('    src/c.h:12  [#if !(defined(_WIN32)) && !(defined(__linux__))]  | inline void E()', out)
        self.assertNotIn('C_H', out)

    def test_many_files_read_the_ranges_in_one_pass(self):
        # 一致のあったファイルが SKIP_SCAN_FILES を超えると .skip.tsv を 1 回通して読む。1 つずつ引くときと結果が同じこと
        names = [f'src/f{i}.cpp' for i in range(70)]  # f1 と f10 のように、別のファイルの名前を頭に含むものもある
        rows, skips = list(self.COMMON), [self.COMMENT]
        for i, n in enumerate(names):
            self.write(n, 'void F() {\n  // Run\n#if 0\n  Run();\n#endif\n  Run();\n}\n', self.t0 - 10)
            rows.append(('Run', 'call', 'F', f'{n}:6:3', 'Run();'))
            skips += [(n, 'comment', '2:3', '2:9'), (n, 'inactive', '3:1', '5:7')]
        skips.append(('src/unused.cpp', 'comment', '1:1', '1:9'))  # 一致の無いファイルの範囲は使わない
        self.index('index', rows, skips, tus=['src/a.cpp'] + names)
        skip = os.path.join(self.root, '.index', 'index.tsv.skip.tsv')
        with open(skip, 'rb') as f:
            data = f.read().replace(b'src/f7.cpp\tcomment\t2:3\t2:9\n', b'src/f7.cpp\tcomment\t2:3\t2:9\r\n')  # CRLF の行
        with open(skip, 'wb') as f:
            f.write(data)
        os.utime(skip, (self.t0, self.t0))
        saved = ltq.SKIP_SCAN_FILES
        try:
            ltq.SKIP_SCAN_FILES = 10 ** 9
            one_by_one = self.ltq('refs', 'Run')
            ltq.SKIP_SCAN_FILES = 64
            one_pass = self.ltq('refs', 'Run')
        finally:
            ltq.SKIP_SCAN_FILES = saved
        self.assertEqual(one_pass, one_by_one)
        self.assertIn('inside comments -> not code; do not report these: 71', one_pass)
        self.assertRegex(one_pass, r'inside inactive #if[^\n]*: 70')

    def test_many_failed_tus_are_summarized(self):
        # 失敗した TU が多いと、並べるだけでクライアントの出力の上限を超える(O3DE で 180 TU・100 KB)。10 件まで並べ、残りは理由ごとの件数
        self.index('index', self.COMMON, [self.COMMENT])
        tu = os.path.join(self.root, '.index', 'index.tsv.tu.tsv')
        with open(tu, 'a', encoding='utf-8', newline='\n') as f:
            for i in range(12):
                why = 'x.h file not found' if i % 3 else "type '__m128' is not a structure"
                f.write(f'{os.path.join(self.root, "src", f"bad{i}.cpp")}\t0\t1\t0.1\t0\t{why}\n')
        os.utime(tu, (self.t0, self.t0))
        out = self.ltq('refs', 'Run')
        # 名前を含むファイルの TU が無ければ、1 行の件数だけ
        self.assertIn("12 TU had parse errors, none in a file that contains the name (all in .index/index.tsv.tu.tsv):"
                      " 8 x x.h file not found; 4 x type '__m128' is not a structure", out)
        self.assertNotIn('    src/bad', out)
        for i in range(12):  # 名前を含むファイルの TU は 10 件まで並べる
            self.write(f'src/bad{i}.cpp', 'void G() { Run(); }\n', self.t0 - 10)
        out = self.ltq('refs', 'Run')
        self.assertIn('12 TU had parse errors; references only in those TUs may be missing.'
                      ' In files that contain the name (12):', out)
        self.assertEqual(len([l for l in out.splitlines() if re.match(r'    src/bad\d+\.cpp  \(', l)]), 10)
        self.assertIn('... 2 more (all in .index/index.tsv.tu.tsv)', out)
        # 構成が複数なら、最初の構成の .tu.tsv だけを挙げない
        self.index('release', self.COMMON, [self.COMMENT])
        out = self.ltq('refs', 'Run')
        self.assertIn('(all in .index/index.tsv.tu.tsv, .index/release.tsv.tu.tsv)', out)

    def test_root_is_found_from_a_subfolder(self):
        # --root を省くと、カレントから親へさかのぼって .index のあるフォルダを使う
        self.index('index', self.COMMON, [self.COMMENT])
        cwd = os.getcwd()
        out = io.StringIO()
        try:
            os.chdir(os.path.join(self.root, 'src'))
            with contextlib.redirect_stdout(out):
                code = ltq.main(['refs', 'Run'])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('the index is in this parent folder', out.getvalue().splitlines()[0])
        self.assertIn('src/a.cpp:3  [in Main]  | Run();', out.getvalue())


if __name__ == '__main__':
    unittest.main()
