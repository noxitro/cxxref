"""ソース 1 つの条件付きコンパイルの構造を読む(#if / #ifdef / #ifndef / #elif / #else / #endif)。

clang を使わず、行単位で読む(行番号を崩さない)。使うのは 2 か所:
- ltq: 無効な #if の中の行に、それを囲む条件を添える(索引には条件を持たない。問い合わせのときにソースから読む)
- scripts/altpass.py: 無効な分岐を仮に有効にした写しを作る(#if 1 / #if 0 への書き換え)

コメントの中の `#if` は、索引の .skip.tsv の comment の範囲(comment_lines)を渡されたときだけ除く。
"""
import re

DIR = re.compile(rb'^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$')
OPEN = ('if', 'ifdef', 'ifndef')


def logical_lines(data):
    """(最初の行番号, 最後の行番号, 続き行をつないだ本文) を返す。行番号は 1 始まり。"""
    lines = data.split(b'\n')
    i = 0
    while i < len(lines):
        s = lines[i].rstrip(b'\r')
        b = i
        while s.endswith(b'\\') and i + 1 < len(lines):
            i += 1
            s = s[:-1] + b' ' + lines[i].rstrip(b'\r')
        yield b + 1, i + 1, s
        i += 1


def strip_comment(s):
    s = re.sub(r'/\*.*?\*/', ' ', s)
    s = re.sub(r'//.*$', '', s)
    s = re.sub(r'/\*.*$', '', s)
    return ' '.join(s.split())


class Branch:
    """分岐 1 つ(#if・#elif・#else の行と、その本体)。本体は line_end の次の行から body_end の前の行まで。"""
    __slots__ = ('kind', 'cond', 'line', 'line_end', 'body_end', 'group')

    def __init__(self, kind, cond, line, line_end, group):
        self.kind, self.cond, self.line, self.line_end, self.group = kind, cond, line, line_end, group
        self.body_end = None  # 次の分岐か #endif の行

    def body_first(self):
        """本体の最初の行。本体が空なら None。"""
        return self.line_end + 1 if self.line_end + 1 < self.body_end else None


class Group:
    """#if から #endif まで。parent は、このグループを含む分岐(Branch)か None。"""
    __slots__ = ('branches', 'endif', 'parent', 'depth')

    def __init__(self, parent, depth):
        self.branches, self.endif, self.parent, self.depth = [], None, parent, depth


def parse(data, comment_lines=()):
    """条件のグループの一覧(文書の順。外側のグループが先)。"""
    groups, stack = [], []  # stack: 開いている Group
    for b, e, s in logical_lines(data):
        m = DIR.match(s)
        if not m or b in comment_lines:
            continue
        kind = m.group(1).decode()
        rest = strip_comment(m.group(2).decode('utf-8', 'replace'))
        if kind in OPEN:
            g = Group(stack[-1].branches[-1] if stack else None, len(stack))
            groups.append(g)
            stack.append(g)
            g.branches.append(Branch(kind, rest, b, e, g))
        elif not stack:
            continue  # 対応の崩れたファイル
        elif kind == 'endif':
            g = stack.pop()
            g.branches[-1].body_end = b
            g.endif = (b, e)
        else:
            g = stack[-1]
            g.branches[-1].body_end = b
            g.branches.append(Branch(kind, rest, b, e, g))
    for g in stack:  # 閉じていないグループ
        g.branches[-1].body_end = 10 ** 9
        g.endif = None
    return groups


def own_cond(br):
    """その分岐の行に書かれた条件(#ifdef X は defined(X) に直す)。#else は ''。"""
    if br.kind in ('ifdef', 'elifdef'):
        return f'defined({br.cond})'
    if br.kind in ('ifndef', 'elifndef'):
        return f'!defined({br.cond})'
    return br.cond


def cond_text(br):
    """その分岐が有効になる条件を、読める形で。#elif・#else は、前の分岐が成り立たないことを添える。"""
    prev = [own_cond(x) for x in br.group.branches[:br.group.branches.index(br)]]
    if br.kind == 'else':
        return ' && '.join(f'!({p})' for p in prev)
    if not prev:
        return own_cond(br)
    return own_cond(br) + ' [and not: ' + ', '.join(prev) + ']'


def is_const_false(br):
    """#if 0 の類(どの構成でも有効にならない)。"""
    return br.kind == 'if' and re.fullmatch(r'\(?\s*(0+[uUlL]*|false)\s*\)?', br.cond) is not None


def directive_lines(groups):
    """#if・#elif・#else・#endif の行(続き行を含む)。"""
    s = set()
    for g in groups:
        for br in g.branches:
            s.update(range(br.line, br.line_end + 1))
        if g.endif:
            s.update(range(g.endif[0], g.endif[1] + 1))
    return s


def enclosing(groups, line):
    """その行を囲む分岐を、外側から。"""
    out = [br for g in groups for br in g.branches if br.line_end < line < (br.body_end or 0)]
    return sorted(out, key=lambda br: br.group.depth)


def conds(groups, line):
    """その行を囲む分岐の条件を、外側から。"""
    return [cond_text(br) for br in enclosing(groups, line)]
