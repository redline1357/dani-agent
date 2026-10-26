"""Representative, coordinator-authored local repositories. No dependency installs."""
from pathlib import Path
import uuid

from .coordinator import DATA
from . import projects as P


def create():
    parent = DATA / ('phase2_fixtures_' + uuid.uuid4().hex[:8])
    parent.mkdir()
    fixtures = []
    definitions = [
        ('python', {'calculator.py': 'def add(a, b):\n    return a - b\n',
            'test_calculator.py': 'import unittest\nfrom calculator import add\nclass Tests(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n',
            'pyproject.toml': '[project]\nname="dani-python-fixture"\nversion="0.0.0"\n'}, 'calculator.py', 'python-unittest'),
        ('nextjs', {'package.json': '{"name":"dani-next-fixture","private":true,"scripts":{"test":"node --test arithmetic.test.mjs"},"dependencies":{"next":"16.0.0","react":"19.0.0"},"devDependencies":{"typescript":"5.9.0"}}\n',
            'app/page.tsx': 'export default function Page() { return <main>Dani fixture</main>; }\n',
            'arithmetic.mjs': 'export function add(a, b) { return a - b; }\n',
            'arithmetic.test.mjs': "import {test} from 'node:test';\nimport {strict as assert} from 'node:assert';\nimport {add} from './arithmetic.mjs';\ntest('addition',()=>assert.equal(add(2,3),5));\n",
            'tsconfig.json': '{"compilerOptions":{"jsx":"preserve","strict":true}}\n'}, 'arithmetic.mjs', 'node-test')]
    for name, files, editable, test_id in definitions:
        root = parent / name
        root.mkdir()
        for path, source in files.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(source, encoding='utf-8')
        P.git(root, 'init', '--template=')
        P.git(root, 'add', '--all')
        P.git(root, 'commit', '-m', 'Disposable representative project baseline')
        fixtures.append({'summary': P.discover(root), 'editable_paths': [editable], 'test_id': test_id,
            'objective': 'Fix add(a, b) to return the sum. Change only ' + editable + '. Tests must pass.'})
    return fixtures
