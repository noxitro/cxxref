"""長く動くプロセス向けの部品(名前の目次・単語のフィルタ・ソースの一覧の監視)の試験。"""
import os
import random
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ltq  # noqa: E402
import namedir  # noqa: E402
import srctree  # noqa: E402
import wordfilter  # noqa: E402


class CliTest(unittest.TestCase):
    def test_ltq_starts_alone_in_isolated_mode(self):
        # ltq.cmd は python -I で起動する(スクリプトのフォルダが sys.path に入らない)。隣のモジュールに頼らない
        import subprocess
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'ltq.py')
        r = subprocess.run([sys.executable, '-I', here, '--help'], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_short_name_agrees(self):
        for s in (b'f', b'A::f', b'A<x::y>::f', b'a:::b', b'?::f', b''):
            self.assertEqual(ltq.sym_short(s), namedir.short_of(s))
            self.assertEqual(ltq.sym_short(s), s.rsplit(b'::', 1)[-1])


class GrepTest(unittest.TestCase):
    def test_many_needles_in_one_pass_match_one_by_one(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, 'index.tsv')
            lines = [f'S{i % 17}\tcall\tC\tsrc/f{i % 11}.cpp:{i}:3\tx(); f{i % 5}' for i in range(2000)]
            with open(p, 'w', encoding='utf-8', newline='\n') as f:
                f.write('\n'.join(lines))
            needles = [b'\tsrc/f1.cpp:', b'\tsrc/f10.cpp:', b'\tsrc/f3.cpp:', b'S4\t', b'f4']
            one = {}
            for n in needles:
                one.update(ltq.grep_found(p, [n]))
            self.assertEqual(ltq.grep_found(p, needles), one)
        finally:
            shutil.rmtree(d)


class NameDirTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'index.tsv')
        rnd = random.Random(7)
        names = [f'n{i}' for i in range(40)] + ['Run', 'operator==', '~Foo']
        self.lines = []
        for i in range(3000):
            sym = '::'.join(['ns', rnd.choice(['A', 'B<x::y>', 'C'])][:rnd.randint(0, 2)] + [rnd.choice(names)])
            self.lines.append(f'{sym}\tcall\tns::Caller{i % 7}\tsrc/f{i % 13}.cpp:{i}:3\t{sym}();')
        with open(self.path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(self.lines))  # 最後の行に改行が無い

    def tearDown(self):
        shutil.rmtree(self.dir)

    def brute(self, short):
        out, pos = [], 0
        for line in self.lines:
            if namedir.short_of(line.split('\t', 1)[0].encode()) == short:
                out.append(pos)
            pos += len(line.encode()) + 1
        return out

    def test_offsets_match_a_full_scan_across_worker_boundaries(self):
        nd = namedir.NameDir(self.path)
        self.assertFalse(nd.open())
        self.assertTrue(nd.build(workers=5))  # 区間の境目が行の途中に来る
        try:
            for short in (b'Run', b'n0', b'n39', b'operator==', b'~Foo', b'missing'):
                got = [o for o in nd.offsets(short)
                       if namedir.short_of(self._line(o).split(b'\t', 1)[0]) == short]
                self.assertEqual(got, self.brute(short), short)
            self.assertTrue(nd.current())
        finally:
            nd.close()
        # 書いた目次を開き直せる。索引が変わると古いと分かる
        nd2 = namedir.NameDir(self.path)
        try:
            self.assertTrue(nd2.open())
            got = [o for o in nd2.offsets(b'Run') if namedir.short_of(self._line(o).split(b'\t', 1)[0]) == b'Run']
            self.assertEqual(got, self.brute(b'Run'))
            with open(self.path, 'a', encoding='utf-8', newline='\n') as f:
                f.write('\nRun\tcall\t\tsrc/z.cpp:1:1\tRun();')
            self.assertFalse(nd2.current())
        finally:
            nd2.close()
        nd3 = namedir.NameDir(self.path)
        self.assertFalse(nd3.open())

    def test_corrupt_directory_is_rebuilt(self):
        # 書きかけ・壊れた目次は開けない(例外にしない)。作り直せる
        nd = namedir.NameDir(self.path)
        self.assertTrue(nd.build(workers=2))
        nd.close()
        with open(nd.cache_path, 'r+b') as f:
            data = f.read()
            f.seek(0)
            f.truncate()
            f.write(data[:len(data) // 3])
        nd2 = namedir.NameDir(self.path)
        self.assertFalse(nd2.open())
        self.assertTrue(nd2.build(workers=2))
        nd2.close()

    def _line(self, off):
        with open(self.path, 'rb') as f:
            f.seek(off)
            return f.readline().rstrip(b'\n')


class WordFilterTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, data):
        p = os.path.join(self.dir, name)
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def test_no_false_negatives_and_unfiltered_files(self):
        rnd = random.Random(3)
        vocab = [f'W{i}_x' for i in range(2000)]
        files, words = {}, {}
        for i in range(30):
            ws = set(rnd.sample(vocab, rnd.randint(0, 400)))
            words[f'f{i}.cpp'] = ws
            p = self.write(f'f{i}.cpp', (' '.join(sorted(ws)) + '\n// ok\n').encode())
            files[f'f{i}.cpp'] = (p, os.stat(p).st_mtime)
        p = self.write('u16.h', '﻿W1_x W2_x'.encode('utf-16'))
        files['u16.h'] = (p, os.stat(p).st_mtime)
        wf = wordfilter.WordFilter(os.path.join(self.dir, 'w.cache'))
        wf.load(None)
        self.assertEqual(wf.update(files, None, pool_min=10, workers=3), 31)
        fp = 0
        for w in vocab[:300]:
            test = wf.may_contain(w)
            for rel, ws in words.items():
                if w in ws:
                    self.assertTrue(test(rel, files[rel][1]), (w, rel))
                elif test(rel, files[rel][1]):
                    fp += 1
            self.assertTrue(test('u16.h', files['u16.h'][1]))  # UTF-16 は絞らない
            self.assertTrue(test('nope.cpp', 0))  # 知らないファイルは絞らない
            self.assertTrue(test('f0.cpp', -1.0))  # 更新日時が違えば絞らない
        self.assertLess(fp, 300 * 30 * 0.1)
        self.assertIsNone(wf.may_contain('operator=='))
        wf.save()  # update() が書くのは多くを読み直したときだけ
        # 書いた一覧を読み直せる。変わったファイルだけ読み直す
        wf2 = wordfilter.WordFilter(os.path.join(self.dir, 'w.cache'))
        wf2.load(None)
        self.assertEqual(wf2.update(files, None), 0)
        time.sleep(0.01)
        p = self.write('f0.cpp', b'Fresh_word\n')
        os.utime(p, (5000, 5000))
        files['f0.cpp'] = (p, os.stat(p).st_mtime)
        self.assertEqual(wf2.update(files, None), 1)
        self.assertTrue(wf2.may_contain('Fresh_word')('f0.cpp', files['f0.cpp'][1]))
        # コードページが変わったら作り直す
        self.assertEqual(wf2.update(files, 'cp932'), 31)

    def test_same_mtime_with_a_different_size_is_read_again(self):
        # 更新日時を保ったまま中身を入れ替えるコピー・同期のツールがある。大きさも見る
        p = self.write('a.cpp', b'Old_word\n')
        os.utime(p, (5000, 5000))
        files = {'a.cpp': (p, os.stat(p).st_mtime)}
        sizes = {'a.cpp': os.stat(p).st_size}
        wf = wordfilter.WordFilter()
        wf.update(files, None, sizes)
        self.assertFalse(wf.may_contain('New_word', sizes)('a.cpp', files['a.cpp'][1]))
        self.write('a.cpp', b'New_word and more\n')
        os.utime(p, (5000, 5000))
        sizes = {'a.cpp': os.stat(p).st_size}
        self.assertTrue(wf.may_contain('New_word', sizes)('a.cpp', files['a.cpp'][1]))  # 読み直すまでは絞らない
        self.assertEqual(wf.update(files, None, sizes), 1)
        self.assertTrue(wf.may_contain('New_word', sizes)('a.cpp', files['a.cpp'][1]))

    def test_cache_is_written_the_first_time_even_for_few_files(self):
        p = self.write('a.cpp', b'x\n')
        wf = wordfilter.WordFilter(os.path.join(self.dir, 'w.cache'))
        wf.load(None)
        wf.update({'a.cpp': (p, os.stat(p).st_mtime)}, None)
        self.assertTrue(os.path.exists(os.path.join(self.dir, 'w.cache')))

    def test_other_files_are_read_like_the_text_search(self):
        # C/C++ 以外のファイルのフィルタは、テキスト検索(ltq.other_chunks)と同じ部分を読む:
        # バイナリは最初の NUL より前だけ、大きなファイルは分けて全部
        mixed = self.write('mixed.bin', b'Before_word text\n\0\1 After_word\n')
        blob = self.write('blob.dat', b'\0\1 Blob_word')
        big = self.write('big.txt', b'pad\n' * 5000 + b'Late_word\n')
        u16 = self.write('u16.txt', 'Wide_word'.encode('utf-16'))
        files = {os.path.basename(p): (p, os.stat(p).st_mtime) for p in (mixed, blob, big, u16)}
        wf = wordfilter.WordFilter(other=True)
        chunk, ltq.READ_CHUNK = ltq.READ_CHUNK, 4096
        try:
            wf.update(files, None)
        finally:
            ltq.READ_CHUNK = chunk

        def may(word, name):
            return wf.may_contain(word)(name, files[name][1])

        self.assertTrue(may('Before_word', 'mixed.bin'))
        self.assertFalse(may('After_word', 'mixed.bin'))
        self.assertFalse(may('Blob_word', 'blob.dat'))
        self.assertTrue(may('Late_word', 'big.txt'))
        self.assertFalse(may('Missing_word', 'big.txt'))
        self.assertTrue(may('Wide_word', 'u16.txt'))  # UTF-16 も、UTF-8 に直して単語を取る
        self.assertFalse(may('Missing_word', 'u16.txt'))
        # 単語の種類が多すぎるファイルは、集合を貯め続けずに「絞らない」にする
        many = self.write('many.txt', b' '.join(b'w%d' % i for i in range(50)))
        cap, wordfilter.MAX_WORDS = wordfilter.MAX_WORDS, 10
        try:
            self.assertEqual(wordfilter._work(([('many.txt', many)], None, True)), [('many.txt', wordfilter.ALWAYS, 0)])
        finally:
            wordfilter.MAX_WORDS = cap

    def test_words_are_taken_after_conversion(self):
        # Shift_JIS の「ソ」(0x83 0x5C)と「ア」(0x83 0x41)。2 バイト目の英字を単語に数えない
        p = self.write('sj.cpp', 'アB ok'.encode('cp932'))
        files = {'sj.cpp': (p, os.stat(p).st_mtime)}
        wf = wordfilter.WordFilter()
        wf.update(files, 'cp932')
        self.assertFalse(wf.may_contain('AB')('sj.cpp', files['sj.cpp'][1]))
        self.assertTrue(wf.may_contain('B')('sj.cpp', files['sj.cpp'][1]))


@unittest.skipUnless(os.name == 'nt', 'ReadDirectoryChangesW is Windows only')
class SourceTreeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, '.index'))
        os.makedirs(os.path.join(self.root, 'src', 'sub'))
        for p in ('src/a.cpp', 'src/b.h', 'src/sub/c.cpp', 'src/readme.txt'):
            self.write(p, 'x')
        self.tree = srctree.SourceTree(self.root, ltq.SRC_EXT, other=ltq.is_other)  # C/C++ 以外のファイルも追う

    def tearDown(self):
        self.tree.close()
        shutil.rmtree(self.root)

    def write(self, rel, text):
        p = os.path.join(self.root, *rel.split('/'))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)

    def scan(self):
        out, others = {}, {}
        srctree.scan(self.root, '', ltq.SRC_EXT, out, ltq.is_other, others)
        return {k: v[1] for k, v in out.items()}, {k: v[1] for k, v in others.items()}

    def check(self):
        got = {k: v[1] for k, v in self.tree.current().items()}
        got_others = {k: v[1] for k, v in self.tree.others().items()}
        src, others = self.scan()
        self.assertEqual(got, src)
        self.assertEqual(got_others, others)
        # ltq の走査と同じ一覧(ソースと、C/C++ 以外のファイル)
        idx = ltq.Index.__new__(ltq.Index)
        idx.root, idx._sources, idx.tree, idx._own = self.root, None, None, ()  # 索引のファイルは無い
        self.assertEqual({k: v[1] for k, v in idx.sources().items()}, src)
        self.assertEqual({k: v[1] for k, v in idx.others().items()}, others)

    def test_follows_changes_without_rescanning(self):
        self.assertTrue(self.tree.watching)
        self.check()
        self.assertEqual(self.tree.rescans, 1)
        self.write('src/a.cpp', 'changed')  # 書き換え
        os.utime(os.path.join(self.root, 'src', 'a.cpp'), (9000, 9000))
        self.check()
        self.write('src/new.cpp', 'n')  # 新しいファイル
        self.check()
        os.remove(os.path.join(self.root, 'src', 'b.h'))  # 消す
        self.check()
        os.rename(os.path.join(self.root, 'src', 'new.cpp'), os.path.join(self.root, 'src', 'renamed.hpp'))
        self.check()
        # フォルダを外で作ってから入れる(中のファイルの知らせは来ない)
        outside = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(outside, 'mod', 'deep'))
            with open(os.path.join(outside, 'mod', 'deep', 'm.cpp'), 'w') as f:
                f.write('m')
            shutil.move(os.path.join(outside, 'mod'), os.path.join(self.root, 'src', 'mod'))
        finally:
            shutil.rmtree(outside)
        self.check()
        os.rename(os.path.join(self.root, 'src', 'mod'), os.path.join(self.root, 'src', 'mod2'))  # フォルダの名前を変える
        self.check()
        shutil.rmtree(os.path.join(self.root, 'src', 'sub'))  # フォルダを消す
        self.check()
        self.write('.hidden/x.cpp', 'h')  # . で始まるフォルダは数えない
        self.check()
        # C/C++ 以外のファイル: 足す・書き換える・消す。空のファイルは数えない(拡張子では除かない。中身で見分ける)
        self.write('src/notes.md', 'Run')
        self.write('src/icon.png', 'x')
        self.write('src/empty.txt', '')
        self.check()
        self.assertIn('src/notes.md', self.tree.others())
        self.assertIn('src/icon.png', self.tree.others())
        self.assertNotIn('src/empty.txt', self.tree.others())
        self.write('src/empty.txt', 'now text')
        self.check()
        self.assertIn('src/empty.txt', self.tree.others())
        os.remove(os.path.join(self.root, 'src', 'readme.txt'))
        self.check()
        # 大文字小文字だけの改名(Windows の stat は大文字小文字を区別しないので、古い名前の stat が成功する)
        os.rename(os.path.join(self.root, 'src', 'a.cpp'), os.path.join(self.root, 'src', 'A.cpp'))
        self.check()
        self.assertNotIn('src/a.cpp', self.tree.current())
        os.rename(os.path.join(self.root, 'src', 'notes.md'), os.path.join(self.root, 'src', 'Notes.md'))
        self.check()
        self.assertEqual(self.tree.rescans, 1)  # ここまで全体を走査し直していない
        self.assertFalse([n for n in os.listdir(os.path.join(self.root, '.index')) if n.startswith('.cxxref-watch-')])

    def test_old_marks_are_removed_at_start(self):
        # 作ってから消すまでの間に殺されたサーバーの印は、次に起動したときに消す(作ったばかりの印は残す)
        old = os.path.join(self.root, '.index', '.cxxref-watch-1-1')
        new = os.path.join(self.root, '.index', '.cxxref-watch-2-1')
        for p in (old, new):
            with open(p, 'w') as f:
                f.write('')
        os.utime(old, (1000, 1000))
        t = srctree.SourceTree(self.root, ltq.SRC_EXT)
        try:
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))
        finally:
            t.close()
            os.remove(new)

    def test_without_index_folder_rescans_every_time(self):
        root = tempfile.mkdtemp()
        try:
            with open(os.path.join(root, 'a.cpp'), 'w') as f:
                f.write('a')
            t = srctree.SourceTree(root, ltq.SRC_EXT)
            try:
                self.assertFalse(t.watching)
                self.assertEqual(list(t.current()), ['a.cpp'])
                with open(os.path.join(root, 'b.cpp'), 'w') as f:
                    f.write('b')
                self.assertEqual(sorted(t.current()), ['a.cpp', 'b.cpp'])
                self.assertEqual(t.rescans, 2)
            finally:
                t.close()
        finally:
            shutil.rmtree(root)


if __name__ == '__main__':
    unittest.main()
