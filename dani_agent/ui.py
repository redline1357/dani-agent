"""Native desktop controls; no local HTTP server, startup registration or API keys."""
import json
import tkinter as tk
from tkinter import messagebox, ttk

from .coordinator import DATA, APP, Coordinator, InstanceLock, check_correction_plan, check_diagnostic_plan, correction_preflight_ready, fingerprint, human_decision, import_evidence, integration_ready, prepare_correction_validation, recover_saved_results
from .collaboration import submission
from .presentation import acceptance_confirmation, acceptance_text
from .state import POLICIES, Store, approval_scope


class SafeTk(tk.Tk):
    def readprofile(self, baseName, className):
        # Tk otherwise executes ~/.Tk.py and basename Tcl/Python profiles.
        # The coordinator does not load executable user or project UI settings.
        pass


def main():
    lock = InstanceLock(DATA / "coordinator.lock")
    lock.acquire()
    store = Store(DATA / "dani_agent.sqlite3")
    recover_saved_results(store)
    import_evidence(store)
    engine = Coordinator(store)
    root = SafeTk()
    root.title("Dani Agent")
    root.geometry("1200x900")
    navigation = ttk.Notebook(root)
    navigation.pack(fill='both', expand=True)
    surface = ttk.Frame(navigation)
    navigation.add(surface, text='Tasks')
    ttk.Label(surface, text="Dani Agent — local coordinator", font=("Segoe UI", 17)).pack(anchor="w", padx=16, pady=10)
    ttk.Label(surface, text="One task at a time • existing subscriptions • explicit model approvals • Windows startup not enabled").pack(anchor="w", padx=16)
    ttk.Label(surface, text="Toy correction LIVE VERIFIED • real project worktrees require separate native validation").pack(anchor="w", padx=16, pady=4)
    form = ttk.LabelFrame(surface, text="Disposable toy development task")
    form.pack(fill="x", padx=16, pady=6)
    project = tk.StringVar(value="Disposable Python clamp")
    ttk.Label(form, text="Project name (a new isolated toy repository is created)").pack(anchor="w", padx=8)
    ttk.Entry(form, textvariable=project).pack(fill="x", padx=8)
    objective = tk.Text(form, height=3, wrap="word")
    objective.insert("1.0", APP["OBJECTIVE"])
    objective.pack(fill="x", padx=8, pady=4)
    buttons = ttk.Frame(surface)
    buttons.pack(fill="x", padx=16, pady=10)
    table = ttk.Treeview(surface, columns=("kind", "state", "objective"), show="headings", height=8)
    for name, width in (("kind", 155), ("state", 160), ("objective", 700)):
        table.heading(name, text=name.capitalize())
        table.column(name, width=width)
    table.pack(fill="x", padx=16)
    status_text = tk.StringVar(value="Loading history")
    ttk.Label(surface, textvariable=status_text).pack(anchor="w", padx=16, pady=8)
    notebook = ttk.Notebook(surface)
    notebook.pack(fill="both", expand=True, padx=16, pady=8)
    details, history = (tk.Text(notebook, wrap="word") for _ in range(2))
    acceptance = ttk.Frame(notebook)
    independently_tested = tk.BooleanVar(value=False)
    ttk.Checkbutton(acceptance, text="I independently tested this exact revision (optional; unchecked means no)",
        variable=independently_tested).pack(anchor='w', padx=8, pady=4)
    report_view = tk.Text(acceptance, wrap='word')
    report_view.pack(fill='both', expand=True)
    notebook.add(details, text="Task and progress")
    notebook.add(history, text="Saved real integration results")
    notebook.add(acceptance, text="Final report / human decision")
    closing = False
    last_selected = None

    def display(widget, value):
        text = json.dumps(value, indent=2, ensure_ascii=False)
        if widget.get("1.0", "end-1c") != text:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            widget.configure(state="disabled")

    def selected():
        items = table.selection()
        if not items:
            raise ValueError("Select a task first.")
        return store.get(items[0])

    def action(function):
        try:
            function()
        except (ValueError, RuntimeError, OSError) as exc:
            messagebox.showerror("Dani Agent", str(exc), parent=root)

    def add(kind):
        payload = {}
        if kind == "codex_diagnostic":
            task = selected()
            if task["kind"] != "codex_preflight" or task["state"] != "completed":
                raise ValueError("Select a completed Codex host check. It creates the fresh read-only diagnostic repository.")
            payload["run"] = task["result"]["run"]
        if kind == "integration" and not integration_ready(store):
            raise ValueError("A real Codex diagnostic must pass before a collaboration attempt can be approved.")
        names = {"offline_tests": "Run offline regression tests", "codex_preflight": "Check native Codex host (no models)",
            "codex_diagnostic": "Read-only Codex startup diagnostic", "integration": "Implement and review toy clamp"}
        task_id = store.add(kind, names[kind], payload)
        refresh()
        table.selection_set(task_id)

    def approve():
        task = selected()
        if task["state"] != "awaiting_approval":
            raise ValueError("This task is not awaiting approval. Consumed approvals cannot be reused.")
        policy = approval_scope(task)['limits']
        extra_scope = ''
        project_label = task['payload'].get('project_name', 'Disposable diagnostic')
        if task['kind'] == 'project_development':
            from .project_workflow import check_ready
            plan = check_ready(APP['approved_run'](task['payload']['run']))
            project_label = plan['project']['name']
            if plan['settings'] != task['payload']['settings'] or plan['objective'] != task['objective']:
                raise ValueError('Project execution scope changed. Prepare a new task.')
            extra_scope = '\nOriginal: ' + plan['project']['path'] + '\nBaseline: ' + plan['project']['original_sha'] + '\nEditable paths: ' + ', '.join(plan['editable_paths']) + '\nTest command: ' + json.dumps(plan['test']) + '\nOriginal repository is not modified during agent execution.\n'
        if task['kind'] == 'project_apply':
            from .project_apply import proposal
            if proposal(store, task['payload']['task_id']) != task['payload']:
                raise ValueError('Apply scope changed. Prepare a new request.')
            extra_scope = '\nTHIS WRITES THE ORIGINAL WORKING TREE:\n' + json.dumps(task['payload'], indent=2) + '\n'
            project_label = task['payload']['project_path']
        if task['kind'] == 'milestone_review':
            project_label = 'Phase 2 source milestone'
            extra_scope = '\nRead-only Phase 2 source snapshot; one Claude review, no Codex, no modifications.\nSnapshot: ' + task['payload']['candidate_sha'] + '\n'
        if task['kind'] == 'project_recover':
            from .project_apply import recovery_proposal
            if recovery_proposal(task['payload']['journal']) != task['payload']:
                raise ValueError('Recovery contents changed. Prepare a new recovery request.')
            project_label = task['payload']['project_path']
            extra_scope = '\nRESTORE ORIGINAL FILES AFTER AN INTERRUPTED APPLY. This may replace partial or human edits. Current files are backed up first.\n' + json.dumps(task['payload'], indent=2) + '\n'
        if policy['codex'] or policy['claude']:
            extra_scope += ('\nPRIVACY: project source, diffs, task text, test output and findings are transmitted '
                'to the selected AI providers using your subscriptions. Secret screening is incomplete; '
                'only approve source you are authorized to send. No credential files are intentionally included.\n')
        if task["kind"] == "codex_diagnostic":
            check_diagnostic_plan(task)
        if task["kind"] in ("integration", "collaboration", "correction_validation") and not integration_ready(store):
            raise ValueError("The pending read-only Codex startup diagnostic needs its own approval and passing result first.")
        if task['kind'] == 'correction_validation' and not correction_preflight_ready(store, task):
            raise ValueError('The queued model-free seeded-fixture check must pass before this approval.')
        if task['kind'] == 'correction_validation':
            check_correction_plan(task)
        if messagebox.askyesno("Approve this one task?",
                f"Project: {project_label}\nTask: {task['objective']}\n\n"
                f"Maximum Codex sessions: {policy['codex']}\nMaximum Claude sessions: {policy['claude']}\n"
                f"Maximum seconds per agent: {policy['seconds']}\nRetries: 0\n"
                f"Maximum correction cycles: {policy.get('max_correction_cycles', 0)}\n"
                + ('Seeded test: docstring-only development → independent review → one fix → re-review.\n'
                    'Ordinary tests intentionally miss the seed; a separate sandboxed oracle verifies the fix.\n'
                    if task['kind'] == 'correction_validation' else '') +
                extra_scope + "Pinned models; existing subscriptions only; no API fallback.\n\n"
                "Approve this exact workflow once?", parent=root):
            store.approve(task["id"], fingerprint())

    def submit():
        text = objective.get("1.0", "end-1c")
        payload = {"project_name": project.get(), "template": "clamp-v1"}
        submission(text, payload)
        task_id = store.add("collaboration", text, payload)
        refresh()
        table.selection_set(task_id)

    ttk.Button(form, text="Submit task (model approval required)", command=lambda: action(submit)).pack(anchor="w", padx=8, pady=4)

    def validation():
        task_id = prepare_correction_validation(store)
        refresh()
        table.selection_set(task_id)

    def decide(accepted):
        task = selected()
        if task["state"] != "awaiting_acceptance":
            raise ValueError("Select a task with a passing reviewed candidate awaiting acceptance.")
        if messagebox.askyesno("Accept revision?" if accepted else "Reject revision?",
                acceptance_confirmation(task, independently_tested.get()) + "\nRecord this exact-revision decision?", parent=root):
            human_decision(store, task["id"], accepted, independently_tested=independently_tested.get())

    def cancel():
        task = selected()
        if engine.current and engine.current["id"] == task["id"]:
            engine.cancel()
        else:
            store.cancel_pending(task["id"])

    def refresh_evidence():
        import_evidence(store)
        display(history, store.evidence())

    def refresh():
        nonlocal last_selected
        rows = store.tasks()
        existing = set(table.get_children())
        for task in rows:
            values = (task["kind"], task["state"], task["objective"])
            if task["id"] in existing:
                table.item(task["id"], values=values)
            else:
                table.insert("", 0, iid=task["id"], values=values)
        progress = engine.progress()
        waiting = sum(task["state"] == "awaiting_approval" for task in rows)
        status_text.set("Progress: " + progress.get("phase", "idle") + f" | {waiting} awaiting model approval | no automatic model retries")
        if not table.selection() and rows:
            table.selection_set(next((task["id"] for task in rows if task["state"] == "awaiting_approval"), rows[0]["id"]))
        if table.selection():
            task = selected()
            if last_selected != task['id']:
                independently_tested.set(False)
                last_selected = task['id']
            display(details, {"task": task, "policy": POLICIES[task["kind"]],
                "progress": progress if engine.current and engine.current["id"] == task["id"] else None,
                "events": store.history(task["id"])})
            body = acceptance_text(task)
            result = task.get("result") or {}
            if result.get("run"):
                try:
                    path = APP["approved_run"](result["run"]) / "REPORT.md"
                    if path.is_file():
                        body += '\n\nSaved Markdown report:\n\n' + path.read_text(encoding="utf-8")
                except (ValueError, OSError):
                    body = "Saved report is unavailable; see task evidence."
            if result.get("human_decision"):
                body += "\n\nHuman decision: " + json.dumps(result["human_decision"], indent=2)
            if result.get("interrupted"):
                body += "\n\nInterrupted: no replay. Reason: " + result.get("reason", "unknown")
            if report_view.get("1.0", "end-1c") != body:
                report_view.configure(state="normal")
                report_view.delete("1.0", "end")
                report_view.insert("1.0", body)
                report_view.configure(state="disabled")
        display(history, store.evidence())

    for button_index, (label, callback) in enumerate((("Offline checks", lambda: add("offline_tests")),
            ("Validate correction loop", validation),
            ("Check Codex host", lambda: add("codex_preflight")),
            ("New Codex diagnostic", lambda: add("codex_diagnostic")),
            ("Approve selected", approve), ("Cancel selected", cancel),
            ("Accept revision", lambda: decide(True)), ("Reject revision", lambda: decide(False)),
            ("Refresh evidence", refresh_evidence))):
        ttk.Button(buttons, text=label, command=lambda f=callback: action(f)).grid(row=button_index // 5, column=button_index % 5, padx=3, pady=3, sticky='ew')

    def select_task(task_id):
        refresh()
        table.selection_set(task_id)
        refresh()  # Reset revision-specific human attestation before any action.
        navigation.select(surface)

    from .phase2_ui import attach
    refresh_phase2 = attach(root, navigation, store, engine, select_task, approve, decide, data_dir=DATA)

    def tick():
        if closing:
            if engine.thread and engine.thread.is_alive():
                root.after(200, tick)
            else:
                root.destroy()
            return
        try:
            engine.tick()
            refresh()
            refresh_phase2()
            if engine.host_context == {'appcontainer': False, 'elevated': False}:
                pending = next((task for task in store.tasks() if task['kind'] == 'correction_validation'
                    and task['state'] == 'awaiting_approval' and correction_preflight_ready(store, task)
                    and not any(event['kind'] == 'approval_dialog_shown' for event in store.history(task['id']))), None)
                if pending:
                    table.selection_set(pending['id'])
                    store.record(pending['id'], 'approval_dialog_shown', {'limits': POLICIES['correction_validation']})
                    approve()
            if engine.host_context == {'appcontainer': False, 'elevated': False}:
                from .project_workflow import check_ready
                for candidate in store.tasks():
                    if candidate['kind'] != 'project_development' or candidate['state'] != 'awaiting_approval':
                        continue
                    if any(e['kind'] == 'approval_dialog_shown' for e in store.history(candidate['id'])):
                        continue
                    try:
                        check_ready(APP['approved_run'](candidate['payload']['run']))
                    except (OSError, RuntimeError, ValueError):
                        continue
                    table.selection_set(candidate['id'])
                    store.record(candidate['id'], 'approval_dialog_shown', {'scope': approval_scope(candidate)})
                    approve()
                    break
        except Exception as exc:
            status_text.set("Coordinator stopped scheduling: " + type(exc).__name__)
        root.after(600, tick)

    def close():
        nonlocal closing
        if engine.current and not messagebox.askyesno("Close Dani Agent?", "Cancel the active task and close? Model attempts will not be replayed.", parent=root):
            return
        closing = True
        engine.cancel()

    root.protocol("WM_DELETE_WINDOW", close)
    try:
        refresh_evidence()
        tick()
        root.mainloop()
    finally:
        engine.cancel()
        lock.close()
