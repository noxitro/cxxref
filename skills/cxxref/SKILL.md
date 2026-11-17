---
name: cxxref
description: Use before grep when the question is where a C++ function, type, variable or macro is used or called, or what a change to it would affect ("where is X used", "what breaks if X changes", list the callers). Resolved by clang over every translation unit. Needs a .index folder. When asked to change a signature and fix the callers yourself, grep first and load this only if the callers are in 5 or more files. Not for explaining code or adding logs or tests.
---

# cxxref

Run it in PowerShell from the repository, as a command of its own (nothing chained with `;`, `|` or `&&`; then it runs without asking the user):

```
python -I "<base directory for this skill>\..\..\mcp\cxxref_cli.py" refs <name>
```

- Put the base directory in as it is and keep `\..\..\` as written. Do not shorten the path yourself (a wrong path is refused). If the session notes already give this command with a full path, use that one

- `<name>`: qualified name or its suffix (`ns::File::Read`, `File::Read`, `Read`). A template goes without its arguments (`Vec::push`, not `Vec<int>::push`). Put a name with spaces or `( ) [ ]` in double quotes (`"Foo::operator()"`, `"operator bool"`). A name with `< > & | ^ %` (`"operator<"`, `"operator->"`) asks the user first
- Options: `--path <folder>` (list only under it, repeatable), `--exclude <folder>`, `--limit <n>`, `--context <n>` (source lines around each line found, for editing callers; see below)
- `check <path:line> ...` instead of `refs`: what the index has on lines found by grep (comment, inactive `#if`)
- It may take a while (it rebuilds a stale index first). Run it once and wait; do not run grep for the same name meanwhile

Reading the result
- `calls`: every call clang resolved, with the enclosing function `[in ...]`. Do not grep the source again to confirm them
- `other references`, `definitions and declarations`, `virtual dispatch` (overrides, `[dynamic]`/`[static]`/`[super]` calls), `unresolved template calls` (decide from the line)
- Marks on a call: `[exact]` the receiver is a value or a final class, so no other override can run; `[dynamic via C]` only overrides in C and the classes derived from C can run; `[instantiated in X]` the template instantiation in which the call resolves to this function; `[under #if COND]` compiled only where COND holds; `[not built]` in a file outside the build (a test, another platform), parsed on a best-effort basis - still code, include it
- For a class name, `derived classes` lists every class that inherits it, directly or not
- `text matches ... not in the index`: text matches of the name the index does not resolve to this symbol, with the reason (comment, inactive `#if`, string, other symbol). Leave the comment ones out
- Matches under "files no indexed TU includes" (tests, other platforms) are not analyzed and may be uses too: judge them from the line, or view the file, and include the real ones
- If the last line names list files, read those files (each fits in one read) instead of grepping
- "No cxxref index": search with rg as usual. Building one takes minutes; ask the user first

Changing a signature and fixing every caller (when grep showed callers in 5 or more files)
- Run `refs <name> --context 3` once. It adds a section with the file text around every call, reference, declaration, definition and override (`>` marks the line; the text after `| ` is the exact file text), as far as the result stays under the size limit. With callers in fewer than 5 files, or when the result is too large, the section is not printed: view each listed line with a small line range
- Write each edit from those lines as the old text. Do not open the whole files; if an edit does not apply or you need more of a file (an include at the top, say), view only a line range
- After editing, do not run `refs` again to verify (the index is being rebuilt); one grep for the name is enough
