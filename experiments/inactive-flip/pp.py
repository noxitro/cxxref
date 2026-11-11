"""ソース 1 つの条件付きコンパイルの構造を読む(#if / #ifdef / #ifndef / #elif / #else / #endif)。
行の対応を崩さないよう、行単位で扱う。コメントの中の # は、索引の .skip.tsv の comment の範囲で除く(渡されたとき)。
"""
import re

DIR = re.compile(rb'^[ \t]*#[ \t]*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$')
IDENT = re.compile(r'[A-Za-z_]\w*')


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
    return s.strip()


class Branch:
    __slots__ = ('kind', 'cond', 'line', 'line_end', 'body_end', 'group')

    def __init__(self, kind, cond, line, line_end, group):
        self.kind, self.cond, self.line, self.line_end, self.group = kind, cond, line, line_end, group
        self.body_end = None  # 次の分岐か #endif の行(その行は含まない本体の終わり + 1)


class Group:
    __slots__ = ('branches', 'endif', 'parent', 'depth')

    def __init__(self, parent, depth):
        self.branches, self.endif, self.parent, self.depth = [], None, parent, depth


def parse(data, comment_lines=()):
    """条件のグループの一覧。parent は、そのグループを含む分岐(Branch)か None。"""
    groups, stack = [], []  # stack: 開いている Group
    for b, e, s in logical_lines(data):
        m = DIR.match(s)
        if not m or b in comment_lines:
            continue
        kind = m.group(1).decode()
        rest = strip_comment(m.group(2).decode('latin-1'))
        if kind in ('if', 'ifdef', 'ifndef'):
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


def cond_text(br):
    """その分岐が有効になる条件を、読める形で(前の分岐の否定を含む)。"""
    def own(x):
        if x.kind == 'ifdef' or x.kind == 'elifdef':
            return f'defined({x.cond})'
        if x.kind == 'ifndef' or x.kind == 'elifndef':
            return f'!defined({x.cond})'
        return x.cond
    g = br.group
    i = g.branches.index(br)
    prev = [own(x) for x in g.branches[:i]]
    if br.kind == 'else':
        return '!(' + ') && !('.join(prev) + ')' if len(prev) > 1 else f'!({prev[0]})'
    if i == 0:
        return own(br)
    return own(br) + ' [after: ' + ', '.join(prev) + ']'


def macros(cond):
    return [m for m in IDENT.findall(re.sub(r"'.'|\"[^\"]*\"", ' ', cond))
            if m not in ('defined', 'true', 'false', '__has_include', '__has_cpp_attribute', '__has_feature',
                         '__has_builtin', '__has_attribute', '__has_include_next', 'after')
            and not re.fullmatch(r'[0-9]\w*|[uUlL]+', m)]


def is_const_false(br):
    """#if 0 の類(どの構成でも有効にならない)。"""
    return br.kind == 'if' and re.fullmatch(r'\(?\s*(0+[uUlL]*|false)\s*\)?', br.cond) is not None
