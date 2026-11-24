"""CLI copy describes thin-data results and the step that re-compiles them.

Run: python -m unittest discover -s tests
"""
import ast
import re
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FILES = [ROOT / "run.py", *sorted((ROOT / "grizz_enrichment").glob("*.py"))]

BANNED = (
    "doesn't know", "does not know", "doesn't have", "does not have", "no data",
    "when available", "incomplete", "not searched", "can't find", "cannot find",
    "could not be found", "couldn't be found", "could not be enriched", "not found",
    "no match found", "returns nothing", "no match", "found or not", "couldn't be resolved",
    "no one matched", "confirm before relying", "in grizz but",
)

# Phrases that are about something other than Grizz data, allowed verbatim.
ALLOWED = (
    "file not found",                    # the user's own local files
    "can leave it incomplete",           # the local rerun file, not Grizz data
    "no match data",                     # the CRM match step's own data
    "the org doesn't have yet",          # field mappings, not Grizz data
)


def _strings(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


class CustomerCopyTests(unittest.TestCase):
    def test_no_missing_data_phrasing(self):
        hits = []
        for path in FILES:
            for line, text in _strings(path):
                low = text.lower()
                for allowed in ALLOWED:
                    low = low.replace(allowed.lower(), "")
                hits += [f"{path.name}:{line}: {b!r}" for b in BANNED if b in low]
                if re.search(r"in grizz.{0,3}(but|not|outside)", low):
                    hits.append(f"{path.name}:{line}: 'in grizz, not/outside'")
        self.assertEqual(hits, [], "missing-data phrasing in customer copy:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
