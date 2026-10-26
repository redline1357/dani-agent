"""Real Tk event-loop smoke test on a disposable database; no CLI agents."""
import json
from pathlib import Path
import sys
import tkinter as tk
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dani_agent import ui, coordinator as C
from dani_agent.state import Store



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class DesktopTests(unittest.TestCase):
    def test_programmatic_project_selection_resets_human_testing_attestation(self):
        from dani_agent import phase2_ui
        folder = ROOT / 'data' / ('desktop_selection_' + uuid.uuid4().hex[:8]);folder.mkdir()
        original_tk, original_attach = ui.SafeTk, phase2_ui.attach
        captured, failures = {}, []
        def wrap(*args, **kwargs):
            captured['store'], captured['select'] = args[2], args[4]
            return original_attach(*args, **kwargs)
        def walk(widget):
            for child in widget.winfo_children():
                yield child
                yield from walk(child)
        def create():
            root = original_tk();root.withdraw()
            def exercise():
                try:
                    store = captured['store']
                    first = store.add('collaboration', 'First revision')
                    second = store.add('collaboration', 'Second revision')
                    captured['select'](first)
                    checkbox = next(w for w in walk(root) if w.winfo_class() == 'TCheckbutton'
                        and str(w.cget('text')).startswith('I independently tested'))
                    checkbox.invoke()
                    self.assertTrue(root.getvar(checkbox.cget('variable')))
                    captured['select'](second)
                    self.assertFalse(root.getvar(checkbox.cget('variable')))
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    root.destroy()
            root.after(100, exercise)
            return root
        with patch.object(ui, 'DATA', folder), patch.object(ui, 'SafeTk', create), \
                patch.object(phase2_ui, 'attach', wrap), patch.object(ui, 'import_evidence', return_value=0):
            ui.main()
        if failures:
            raise failures[0]

    def test_real_desktop_task_submission_approval_denial_and_persistence(self):
        folder = ROOT / "prototype" / ("desktop_regression_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        captured = []
        original = ui.SafeTk
        failures = []

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        def create():
            root = original()
            root.withdraw()
            def exercise():
                try:
                    buttons = {child.cget("text"): child for child in descendants(root) if child.winfo_class() == "TButton"}
                    self.assertIn("Accept revision", buttons)
                    self.assertIn("Reject revision", buttons)
                    tabs = {child.tab(tab, 'text') for child in descendants(root)
                        if child.winfo_class() == 'TNotebook' for tab in child.tabs()}
                    self.assertTrue({'Dashboard', 'Projects', 'Tasks', 'Agents', 'Model Settings',
                        'Execution History', 'Final Reports', 'Settings'} <= tabs)
                    self.assertIn('Add existing project', buttons)
                    self.assertIn('Preview execution scope', buttons)
                    self.assertIn('Request apply to original', buttons)
                    buttons["Submit task (model approval required)"].invoke()
                    buttons["Approve selected"].invoke()  # Mock human says NO.
                    store = Store(folder / "dani_agent.sqlite3")
                    task = store.tasks()[0]
                    self.assertEqual(task["kind"], "collaboration")
                    self.assertEqual(task["state"], "awaiting_approval")
                    self.assertEqual(task["payload"]["template"], "clamp-v1")
                    # A blocked security prerequisite must never launch an agent.
                    captured.append(task["id"])
                    with store.connection() as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM approvals").fetchone()[0], 0)
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    root.destroy()
            root.after(100, exercise)
            return root

        with patch.object(ui, "DATA", folder), patch.object(ui, "SafeTk", create), \
                patch.object(ui, "integration_ready", return_value=True), \
                patch.object(ui.messagebox, "askyesno", return_value=False), \
                patch.object(ui.messagebox, "showerror") as error, \
                patch.object(ui, "import_evidence", return_value=0):
            ui.main()
            error.assert_not_called()
        if failures:
            raise failures[0]
        self.assertEqual(len(captured), 1)
        self.assertEqual(Store(folder / "dani_agent.sqlite3").get(captured[0])["state"], "awaiting_approval")
