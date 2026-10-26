"""Project/settings/report pages for the existing Tk coordinator."""
import json
import time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import model_settings as M, workspaces as W
from .coordinator import APP
from .presentation import acceptance_text
from .state import POLICIES


def attach(root, navigation, store, engine, select_task, approve_task, decide_task, *, data_dir):
    pages = {}
    for title in ('Dashboard', 'Projects', 'Agents', 'Model Settings', 'Execution History', 'Final Reports', 'Settings'):
        frame = ttk.Frame(navigation)
        navigation.add(frame, text=title)
        pages[title] = frame
    navigation.insert(0, pages['Dashboard'])

    def text(parent):
        widget = tk.Text(parent, wrap='word', height=12)
        scroll = ttk.Scrollbar(parent, command=widget.yview)
        widget.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        widget.pack(fill='both', expand=True, padx=8, pady=8)
        return widget

    def show(widget, value):
        body = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
        if widget.get('1.0', 'end-1c') != body:
            widget.configure(state='normal')
            widget.delete('1.0', 'end')
            widget.insert('1.0', body)
            widget.configure(state='disabled')

    def safe(function):
        try:
            return function()
        except (RuntimeError, ValueError, OSError, StopIteration) as exc:
            messagebox.showerror('Dani Agent', str(exc) or 'Selection is no longer available.', parent=root)

    dashboard = text(pages['Dashboard'])
    project_actions = ttk.Frame(pages['Projects'])
    project_actions.pack(fill='x', padx=8, pady=8)
    project_choice = ttk.Combobox(project_actions, state='readonly', width=65)
    project_choice.pack(side='left', padx=5)
    project_ids = {}
    test_choice = ttk.Combobox(pages['Projects'], state='readonly', width=90)
    edit_paths = tk.StringVar()
    objective = tk.Text(pages['Projects'], height=3, wrap='word')
    ttk.Label(pages['Projects'], text='Feature or bug request').pack(anchor='w', padx=8)
    objective.pack(fill='x', padx=8)
    ttk.Label(pages['Projects'], text='Exact editable relative paths, comma separated. Existing configuration is protected.').pack(anchor='w', padx=8)
    ttk.Entry(pages['Projects'], textvariable=edit_paths).pack(fill='x', padx=8)
    ttk.Label(pages['Projects'], text='Detected test command (runs only in MXC; dependencies are never installed automatically)').pack(anchor='w', padx=8)
    test_choice.pack(fill='x', padx=8)
    task_buttons = ttk.Frame(pages['Projects'])
    task_buttons.pack(fill='x', padx=8, pady=6)
    project_summary = text(pages['Projects'])

    def selected_project():
        ident = project_ids.get(project_choice.get())
        return next(p for p in store.projects() if p['id'] == ident)

    def project_changed(event=None):
        project = selected_project()['summary']
        options = project['test_options']
        test_choice['values'] = [o['id'] for o in options]
        preferred = next((o['id'] for o in options if o['id'] in ('python-unittest', 'node-test')), options[0]['id'] if options else '')
        test_choice.set(preferred)

    project_choice.bind('<<ComboboxSelected>>', lambda event: safe(project_changed))

    def add_project():
        path = filedialog.askdirectory(title='Select the Git project root (read-only discovery)', parent=root)
        if path:
            ident = store.add('project_scan', 'Discover selected project without executing hooks or scripts', {'path': path})
            select_task(ident)

    def rescan():
        project = selected_project()['summary']
        select_task(store.add('project_scan', 'Refresh project state (read only)', {'path': project['path']}))

    def scope():
        project = selected_project()
        if not project['summary'].get('eligible'):
            raise ValueError('Project is not executable. Review its warnings; no Git initialization or cleanup is automatic.')
        settings = M.validate(store.setting('model_settings', M.DEFAULTS))
        paths = W.select_scope(project['summary'], [p.strip() for p in edit_paths.get().split(',')])
        request = objective.get('1.0', 'end-1c').strip()
        if not request or len(request) > 4000:
            raise ValueError('Describe the feature or bug in 1–4000 characters.')
        if test_choice.get() not in [o['id'] for o in project['summary']['test_options']]:
            raise ValueError('Select a detected test command.')
        return {'project_id': project['id'], 'editable_paths': paths, 'test_id': test_choice.get(), 'settings': settings}, request

    def preview():
        payload, request = scope()
        summary = selected_project()['summary']
        return {'task': request, 'project': summary['path'], 'original_revision': summary['original_sha'], **payload,
            'isolated_workspace': 'Private sanitized Git snapshot and task worktree inside Dani Agent',
            'test': next(o for o in summary['test_options'] if o['id'] == payload['test_id']),
            'excluded_configurations': summary['excluded'], 'network': 'disabled for generated commands and tests',
            'apply_to_original': 'Separate human acceptance AND exact-revision apply approval required'}

    def prepare_task():
        proposed = preview()
        if messagebox.askyesno('Prepare isolated execution scope?', json.dumps(proposed, indent=2)
                + '\n\nPrepare worktree and model-free security checks? Model sessions require a later explicit approval.', parent=root):
            payload, request = scope()
            select_task(store.add('project_prepare', request, payload))

    ttk.Button(project_actions, text='Add existing project', command=lambda: safe(add_project)).pack(side='left', padx=5)
    ttk.Button(project_actions, text='Rescan', command=lambda: safe(rescan)).pack(side='left', padx=5)
    ttk.Button(task_buttons, text='Preview execution scope', command=lambda: safe(lambda: show(project_summary, preview()))).pack(side='left', padx=5)
    ttk.Button(task_buttons, text='Prepare project task', command=lambda: safe(prepare_task)).pack(side='left', padx=5)

    def fixtures():
        select_task(store.add('project_validation_prepare', 'Prepare owned Python and Next-metadata validation repositories (no models)'))

    ttk.Button(task_buttons, text='Prepare disposable Python + Next.js validation', command=lambda: safe(fixtures)).pack(side='left', padx=5)

    def recovery():
        from .apply_journal import pending
        from .project_apply import recovery_proposal
        entries = pending(data_dir)
        if not entries:
            messagebox.showinfo('Apply recovery', 'No interrupted file applications require recovery.', parent=root)
            return
        # One exact recovery request at a time; the approval dialog lists current
        # hashes and explicitly warns that restoring may replace human edits.
        payload = recovery_proposal(entries[0]['journal'])
        ident = store.add('project_recover', 'Restore originals from an interrupted apply (separate approval)', payload)
        select_task(ident)
        approve_task()

    ttk.Button(task_buttons, text='Recover interrupted apply', command=lambda: safe(recovery)).pack(side='left', padx=5)

    settings_frame = pages['Model Settings']
    variables = {}
    current = store.setting('model_settings', M.DEFAULTS)
    for index, (key, default) in enumerate(M.DEFAULTS.items()):
        ttk.Label(settings_frame, text=key.replace('_', ' ').capitalize()).grid(row=index, column=0, sticky='w', padx=12, pady=5)
        var = tk.StringVar(value=str(current[key]))
        variables[key] = var
        if key.endswith('_effort'):
            widget = ttk.Combobox(settings_frame, textvariable=var, state='readonly', values=('low', 'medium', 'high'))
        elif key.endswith('_model'):
            widget = ttk.Combobox(settings_frame, textvariable=var, state='readonly', values=(default,))
        else:
            high = M.MAX_SECONDS_PER_SESSION if key == 'seconds_per_session' else 2 if key == 'max_correction_cycles' else 3
            widget = ttk.Spinbox(settings_frame, textvariable=var, from_=0 if key == 'max_correction_cycles' else 15 if key == 'seconds_per_session' else 1, to=high)
        widget.grid(row=index, column=1, sticky='ew', padx=12, pady=5)
    settings_frame.columnconfigure(1, weight=1)

    def save_settings():
        value = {key: int(var.get()) if type(M.DEFAULTS[key]) is int else var.get() for key, var in variables.items()}
        M.availability(value)
        store.save_setting('model_settings', value)
        messagebox.showinfo('Model settings', 'Saved. Existing prepared tasks retain their approval-bound settings. Unknown models are blocked, never substituted.', parent=root)

    ttk.Button(settings_frame, text='Save bounded settings', command=lambda: safe(save_settings)).grid(row=9, column=0, padx=12, pady=10)
    ttk.Label(settings_frame, text='Only subscription-proven models are enabled. Availability/quota can change; every execution still checks CLI versions and authentication.').grid(row=10, column=0, columnspan=2, sticky='w', padx=12)
    agents_buttons = ttk.Frame(pages['Agents'])
    agents_buttons.pack(fill='x', padx=8, pady=8)

    def milestone():
        from .milestone_review import prepare
        payload = prepare()
        ident = store.add('milestone_review', 'Independent Phase 2 milestone review: architecture, correctness, security and tests', payload)
        select_task(ident)
        approve_task()

    ttk.Button(agents_buttons, text='Request independent milestone review', command=lambda: safe(milestone)).pack(side='left')
    agents_text = text(pages['Agents'])
    history = text(pages['Execution History'])
    report_frame = pages['Final Reports']
    report_actions = ttk.Frame(report_frame)
    report_actions.pack(fill='x', padx=8, pady=8)
    report_choice = ttk.Combobox(report_actions, state='readonly', width=85)
    report_choice.pack(side='left', padx=5)
    report_ids = {}

    def report_task():
        return store.get(report_ids[report_choice.get()])

    def disposition(accepted):
        select_task(report_task()['id'])
        decide_task(accepted)

    def apply_changes():
        from .project_apply import proposal
        payload = proposal(store, report_task()['id'])
        ident = store.add('project_apply', 'Apply exact accepted revision to the original project (separate approval)', payload)
        select_task(ident)
        approve_task()

    ttk.Button(report_actions, text='Accept', command=lambda: safe(lambda: disposition(True))).pack(side='left', padx=4)
    ttk.Button(report_actions, text='Reject / preserve original', command=lambda: safe(lambda: disposition(False))).pack(side='left', padx=4)
    ttk.Button(report_actions, text='Request apply to original', command=lambda: safe(apply_changes)).pack(side='left', padx=4)
    report_view = text(report_frame)
    settings_text = text(pages['Settings'])
    show(settings_text, 'Local-first Windows coordinator. No Windows startup, service, deployment or paid API fallback.\n\n'
        'Selecting a project performs read-only Git discovery. Dirty/untracked changes block task preparation. '
        'Task worktrees and Git metadata are private copies; original repositories stay untouched during agents/tests. '
        'Rejecting preserves the task branch and evidence and leaves the original unchanged.\n\n'
        'Applying an accepted revision requires a separate approval, clean unchanged original and file/hash checks. '
        'It updates reviewed working-tree files without committing, pushing or deploying. Originals are backed up; '
        'a failed transfer attempts rollback without overwriting a detected concurrent edit. Durable journals block '
        'new applications after an interruption; Projects → Recover interrupted apply requires separate approval '
        'and backs up current contents before restoring originals. Git locks are removed by Windows on process exit.\n\n'
        'No dependencies are installed. Ignored node_modules/virtualenvs are not imported. Missing dependencies, '
        'symlinks, submodules, oversized repositories and unsupported model choices stop rather than relax controls.')
    last_project = None
    report_cache = {}
    last_refresh = 0.0

    def startup_review_request():
        # A developer-prepared request is inert data, not permission to call a model.
        # Only this human-started GUI can present and record the actual approval.
        path = Path(data_dir) / 'phase2_startup_request.json'
        if not path.is_file():
            return
        from .coordinator import fingerprint
        request = json.loads(path.read_text(encoding='utf-8'))
        if request.get('fingerprint') != fingerprint() or request.get('kind') != 'milestone_review':
            return
        existing = next((t for t in store.tasks() if t['kind'] == 'milestone_review'
            and t['payload'] == request['payload']), None)
        ident = existing['id'] if existing else store.add('milestone_review', request['objective'], request['payload'])
        if request.get('prepare_validation_fixtures') and not any(e['kind'] == 'model_free_validation_preparation_queued' for e in store.history(ident)):
            validation_id = store.add('project_validation_prepare', 'Prepare representative project host checks; no model approval granted')
            store.record(ident, 'model_free_validation_preparation_queued', {'task_id': validation_id, 'model_calls': 0})
        if store.get(ident)['state'] != 'awaiting_approval':
            return
        if any(e['kind'] == 'approval_dialog_shown' for e in store.history(ident)):
            return
        store.record(ident, 'approval_dialog_shown', {'scope': 'One Claude session, ' + str(POLICIES['milestone_review']['seconds']) + ' seconds, zero tools/MCP; no Codex or modifications'})
        select_task(ident)
        approve_task()

    root.after(1200, lambda: safe(startup_review_request))

    def refresh():
        nonlocal last_project, last_refresh
        now = time.monotonic()
        if now-last_refresh < 2:
            return
        last_refresh = now
        projects = store.projects()
        project_ids.clear()
        project_ids.update({p['summary']['name'] + ' — ' + p['id'][:8]: p['id'] for p in projects})
        project_choice['values'] = list(project_ids)
        if project_ids and project_choice.get() not in project_ids:
            project_choice.set(next(iter(project_ids)))
        if project_choice.get() in project_ids:
            project = selected_project()
            if last_project != (project['id'], project['summary'].get('digest')):
                project_changed()
                last_project = (project['id'], project['summary'].get('digest'))
                show(project_summary, {k: v for k, v in project['summary'].items() if k not in ('files', 'working_hashes')})
        tasks = store.tasks()
        from .apply_journal import pending
        recovery_entries = pending(data_dir)
        show(dashboard, {'progress': engine.progress(), 'projects': len(projects),
            'interrupted_apply_recovery_required': recovery_entries,
            'tasks': [{'id': t['id'], 'kind': t['kind'], 'state': t['state']} for t in tasks[:15]],
            'model_usage': [t['result'].get('usage') for t in tasks[:10] if t.get('result') and t['result'].get('usage')],
            'toy_correction': 'LIVE VERIFIED', 'phase2_real_projects': 'Require native preflight and explicit bounded validation'})
        show(agents_text, {'developer': 'Codex CLI, ChatGPT subscription', 'reviewer': 'Claude Code CLI, subscription; zero tools/MCP, disabled hooks',
            'settings': store.setting('model_settings', M.DEFAULTS), 'active': engine.progress(),
            'availability': 'Pinned models have successful subscription evidence; no API model catalog or paid fallback is used.'})
        show(history, [{'id': t['id'], 'kind': t['kind'], 'state': t['state'], 'result': t['result']} for t in tasks[:30]])
        completed = [t for t in tasks if t.get('result')]
        report_ids.clear()
        report_ids.update({t['kind'] + ' ' + t['id'][:8] + ' — ' + t['state']: t['id'] for t in completed})
        report_choice['values'] = list(report_ids)
        if report_ids and report_choice.get() not in report_ids:
            report_choice.set(next(iter(report_ids)))
        if report_choice.get() in report_ids:
            task = report_task()
            body = acceptance_text(task)
            result = task['result']
            if result.get('run'):
                run = APP['approved_run'](result['run'])
                for p in sorted((run / 'artifacts').glob('cycle_*/handoff.json')):
                    signature = (p.stat().st_mtime_ns, p.stat().st_size)
                    if report_cache.get(p, (None,))[0] != signature:
                        report_cache[p] = (signature, json.loads(p.read_text(encoding='utf-8')))
                    value = report_cache[p][1]
                    body += '\n\nGit diff ' + p.parent.name + ':\n' + value.get('diff', '')
            if result.get('review'):
                body += '\n\nIndependent milestone findings:\n' + json.dumps(result['review'], indent=2)
            show(report_view, body)
    return refresh
