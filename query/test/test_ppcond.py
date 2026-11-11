"""ppcond(#if の構造を読む)のテスト。

usage: python -I -m unittest discover -s query/test   (リポジトリのルートで)
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
import ppcond  # noqa: E402

SRC = b'''#ifndef H_H
#define H_H
void A();
#if defined(WIN) && \\
    !defined(XBOX)  // desktop
void B();
#elif defined(LINUX)
void C();
#  ifdef GL /* nested */
void D();
#  endif
#else
void E();
#endif
#if 0
void F();
#endif
#endif
'''


class PpcondTest(unittest.TestCase):
    def setUp(self):
        self.groups = ppcond.parse(SRC)

    def test_groups_and_branches(self):
        self.assertEqual([(g.depth, [(b.kind, b.line, b.line_end, b.body_end) for b in g.branches]) for g in self.groups], [
            (0, [('ifndef', 1, 1, 18)]),
            (1, [('if', 4, 5, 7), ('elif', 7, 7, 12), ('else', 12, 12, 14)]),
            (2, [('ifdef', 9, 9, 11)]),
            (1, [('if', 15, 15, 17)]),
        ])
        self.assertEqual(self.groups[1].endif, (14, 14))
        self.assertIs(self.groups[2].parent, self.groups[1].branches[1])
        self.assertIsNone(self.groups[0].parent)

    def test_condition_text(self):
        g = self.groups[1]
        self.assertEqual(g.branches[0].cond, 'defined(WIN) && !defined(XBOX)')  # 続き行をつなぎ、コメントを除く
        self.assertEqual(ppcond.cond_text(g.branches[0]), 'defined(WIN) && !defined(XBOX)')
        self.assertEqual(ppcond.cond_text(g.branches[1]), 'defined(LINUX) [and not: defined(WIN) && !defined(XBOX)]')
        self.assertEqual(ppcond.cond_text(g.branches[2]), '!(defined(WIN) && !defined(XBOX)) && !(defined(LINUX))')
        self.assertEqual(ppcond.own_cond(self.groups[0].branches[0]), '!defined(H_H)')
        self.assertEqual(ppcond.own_cond(self.groups[2].branches[0]), 'defined(GL)')

    def test_enclosing_conditions(self):
        self.assertEqual(ppcond.conds(self.groups, 10),
                         ['!defined(H_H)', 'defined(LINUX) [and not: defined(WIN) && !defined(XBOX)]', 'defined(GL)'])
        self.assertEqual(ppcond.conds(self.groups, 3), ['!defined(H_H)'])
        self.assertEqual(ppcond.conds(self.groups, 4), ['!defined(H_H)'])  # 条件の行そのものは、その分岐の中ではない
        self.assertEqual(ppcond.conds(self.groups, 5), ['!defined(H_H)'])
        self.assertEqual(ppcond.conds(self.groups, 19), [])

    def test_body(self):
        g = self.groups[1]
        self.assertEqual(g.branches[0].body_first(), 6)
        self.assertIsNone(ppcond.parse(b'#ifdef A\n#else\nint x;\n#endif\n')[0].branches[0].body_first())  # 空の本体

    def test_const_false(self):
        self.assertTrue(ppcond.is_const_false(self.groups[3].branches[0]))
        for text, want in ((b'#if 0', True), (b'#if (0)', True), (b'#if false', True), (b'#if 0L', True),
                           (b'#if 01', False), (b'#if 0 || X', False), (b'#if X', False), (b'#ifdef X', False)):
            with self.subTest(text=text):
                self.assertEqual(ppcond.is_const_false(ppcond.parse(text + b'\n#endif\n')[0].branches[0]), want)

    def test_comment_lines_are_skipped(self):
        src = b'/*\n#if OLD\n*/\n#if NEW\nint x;\n#endif\n'
        self.assertEqual(len(ppcond.parse(src)), 2)  # コメントの行を知らなければ、#if として読む
        gs = ppcond.parse(src, {2, 3})
        self.assertEqual([[b.cond for b in g.branches] for g in gs], [['NEW']])

    def test_unbalanced_files(self):
        gs = ppcond.parse(b'#endif\n#if A\nint x;\n')  # 余った #endif は読み飛ばす。閉じていない #if は末尾まで
        self.assertEqual(len(gs), 1)
        self.assertIsNone(gs[0].endif)
        self.assertEqual(ppcond.conds(gs, 3), ['A'])

    def test_crlf_and_spacing(self):
        gs = ppcond.parse(b'  #  if  A  \r\nint x;\r\n\t#\tendif\r\n')
        self.assertEqual([(b.kind, b.cond, b.body_end) for b in gs[0].branches], [('if', 'A', 3)])


if __name__ == '__main__':
    unittest.main()
