---
name: cxxref
description: Use before grep when the question is where a C++ function, type, variable or macro is used or called, or what a change to it would affect ("where is X used", "what breaks if X changes", list the callers). Resolved by clang over every translation unit, with the reason for each text match it leaves out. Needs a .index folder in the repository. Not when you are asked to make the change yourself (editing callers), explain code, or add logs or tests - use grep and view as usual.
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

If the task turns into editing: open only around the `calls` lines. After editing, do not run `refs` again to verify (the index is being rebuilt); rg is enough.
