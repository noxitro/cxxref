---
name: cxxref
description: C++ repos with a .index folder only. Lists every place a C++ function, type, variable or macro is called or used, resolved by clang (not text). Use it for "where is X used / called", "what breaks if X changes", or finding all callers before changing a signature. Not for explaining code, adding logs or tests, or editing a spot you already know.
---

# cxxref

Run it in PowerShell from the repository, as a command of its own (nothing chained with `;`, `|` or `&&`; then it runs without asking the user):

```
python -I "<absolute folder of this SKILL.md>\..\..\mcp\cxxref_cli.py" refs <name>
```

- `<name>`: qualified name or its suffix (`ns::File::Read`, `File::Read`, `Read`)
- Options: `--path <folder>` (list only under it, repeatable), `--exclude <folder>`, `--limit <n>`
- `check <path:line> ...` instead of `refs`: what the index has on lines found by grep (comment, inactive `#if`)
- It may take a while (it rebuilds a stale index first). Run it once and wait; do not run grep for the same name meanwhile

Reading the result
- `calls`: every call, with the enclosing function `[in ...]`. This is the answer; do not grep the source again to confirm it
- `other references`, `definitions and declarations`, `virtual dispatch` (overrides, `[dynamic]`/`[static]`/`[super]` calls), `unresolved template calls` (decide from the line)
- `text matches ... not in the index`: text matches of the name that are not this symbol, with the reason (comment, inactive `#if`, string, other symbol). Leave the comment ones out
- If the last line names list files, read those files (each fits in one read) instead of grepping
- "No cxxref index": search with rg as usual. Building one takes minutes; ask the user first

When fixing callers: open only around the `calls` lines. After editing, do not run `refs` again to verify (the index is being rebuilt); rg is enough.
