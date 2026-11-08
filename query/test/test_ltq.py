"""ltq の回帰テスト(合成した小さな索引で、構成の統合と鮮度の扱いを確かめる)。

usage: python -I -m unittest discover -s query/test   (リポジトリのルートで)
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ltq  # noqa: E402

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

    def index(self, name, rows, skips, tus=('src/a.cpp',), mtime=None, deps=None, cp=None):
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
        self.assertIn('1 source files modified after indexing are not read by any indexed configuration', out)
        self.assertIn('src/c.cpp', out)
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
        self.assertIn(': 2\n    call   src/v.cpp:4  Base::Draw  [in Call]', out)
        self.assertNotIn('overridden by', out)
        self.assertNotIn('other symbols named', out)  # 上書きの関係にある記号は virtual dispatch の節に出す
        out = self.ltq('refs', 'Base::Draw')
        self.assertIn('== calls (resolved by clang): 1\n', out)
        self.assertNotIn('the query overrides', out)
        self.assertIn('calls resolved to these, 2, call the override', out)
        self.assertIn('    Derived::Draw  src/v.cpp:2\n    Leaf::Draw  src/v.cpp:3\n', out)
        self.assertNotIn('override ', out.split('== virtual dispatch')[0])  # override 行は他の節に混ぜない
        out = self.ltq('check', 'src/v.cpp:2')
        self.assertIn('indexed: override Derived::Draw at col 30  [overrides Base::Draw]', out)

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
        self.assertIn('1 source files changed after indexing', out)
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
        self.assertIn('12 TU had parse errors', out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith('    src/bad')]), 10)
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
