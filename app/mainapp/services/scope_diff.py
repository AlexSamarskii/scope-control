"""Bounded line diff; plain data is autoescaped by templates."""

from difflib import SequenceMatcher


def scope_diff(source, proposed):
    result = []
    for name, label in [
        ("body", "Объём работ"),
        ("exclusions", "Исключения"),
        ("revision_rules", "Правила правок"),
    ]:
        old, new = getattr(source, name) or "", proposed.get(name) or ""
        changes = []
        before, after = old.splitlines(), new.splitlines()
        if before == after:
            changes = [("same", new)]
        elif len(before) + len(after) > 1000:
            changes = [("removed", old), ("added", new)]
        else:
            for kind, i, j, k, l in SequenceMatcher(None, before, after).get_opcodes():
                if kind == "equal":
                    changes.append(("same", "\n".join(before[i:j])))
                else:
                    if i != j:
                        changes.append(("removed", "\n".join(before[i:j])))
                    if k != l:
                        changes.append(("added", "\n".join(after[k:l])))
        result.append({"label": label, "changed": before != after, "changes": changes})
    return result
