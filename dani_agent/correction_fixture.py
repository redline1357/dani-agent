"""Trusted, disposable review-gap fixture. No provider calls or executable settings."""
import ast

TEMPLATE = "clamp-review-gap-v1"
OBJECTIVE = ("Validate one automatic correction cycle on the disposable clamp fixture. "
    "The initial development step adds a docstring without changing behavior. "
    "An independent reviewer must check the complete contract: integer clamp returns the bounded value "
    "and raises ValueError when lower > upper. Forward actual findings, fix, retest and re-review.")
SOURCE = "def clamp(value, lower, upper):\n    return min(max(value, lower), upper)\n"
INITIAL_PROMPT = ("This is the initial maintenance step of a controlled review-gap fixture. "
    "Add a concise function docstring to clamp in toy_math.py. Preserve the existing function body and behavior exactly. "
    "Do not fix functional defects in this initial step; an independent reviewer will assess the full contract next.")
RULES = (" Change only toy_math.py. Do not edit tests, control files, configuration or Git metadata. "
    "Do not install anything, commit, use MCP/network or request escalation. "
    "The coordinator runs tests and commits. Return JSON matching the developer schema.")
WEAK_TESTS = '''import unittest
from toy_math import clamp
class ClampTests(unittest.TestCase):
    def test_below(self): self.assertEqual(clamp(-1, 0, 10), 0)
    def test_lower(self): self.assertEqual(clamp(0, 0, 10), 0)
    def test_inside(self): self.assertEqual(clamp(4, 0, 10), 4)
    def test_upper(self): self.assertEqual(clamp(10, 0, 10), 10)
    def test_above(self): self.assertEqual(clamp(11, 0, 10), 10)
    def test_equal(self): self.assertEqual(clamp(1, 3, 3), 3)
    def test_negative(self): self.assertEqual(clamp(-5, -10, -2), -5)
    def test_large(self): self.assertEqual(clamp(10**20, 0, 10), 10)
'''
# Coordinator-owned oracle; executed only through the existing read-only MXC
# test runner for real candidates. Never supplied to the initial reviewer.
ACCEPTANCE_TEST = '''import importlib.util
from pathlib import Path
import sys
import unittest
path = Path(sys.argv.pop(1)).resolve()
spec = importlib.util.spec_from_file_location("candidate", path)
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)
class AcceptanceTests(unittest.TestCase):
    def test_reversed_bounds(self):
        for value, lower, upper in ((5, 10, 0), (0, 1, -1), (100, 7, 6)):
            with self.assertRaises(ValueError): candidate.clamp(value, lower, upper)
    def test_below(self): self.assertEqual(candidate.clamp(-1, 0, 10), 0)
    def test_above(self): self.assertEqual(candidate.clamp(11, 0, 10), 10)
    def test_equal(self): self.assertEqual(candidate.clamp(0, 3, 3), 3)
    def test_negative(self): self.assertEqual(candidate.clamp(-5, -10, -2), -5)
unittest.main()
'''


def initial_body_preserved(source):
    """Parse only; never execute a provider-generated candidate on the host."""
    tree = ast.parse(source)
    if (len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef)
            or ast.get_docstring(tree.body[0]) is None):
        return False
    tree.body[0].body.pop(0)
    return ast.dump(tree, include_attributes=False) == ast.dump(ast.parse(SOURCE), include_attributes=False)
