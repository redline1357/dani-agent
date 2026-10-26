"""Real Windows crash/rollback checks with owned fixtures, no model execution."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid
import hashlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'prototype'))
from dani_agent import coordinator as C, project_apply as A, apply_journal as J
from dani_agent import projects as P, phase2_fixtures as FIX, workspaces as W, model_settings as M
from dani_agent.file_transfer import temporary_lock
from dani_agent.state import Store



import fixture_runs as TF
PATCHES = TF.patched()   # synthetic reviewer profile + verified default models; no earlier live runs needed


def setUpModule():
    TF.start(PATCHES)


def tearDownModule():
    TF.stop(PATCHES)

class RecoveryTests(unittest.TestCase):
    def test_fenced_review_replay_preserves_findings_and_rejects_extra_text(self):
        from dani_agent.collaboration import review_json
        value={'candidate_sha':'a'*40,'verdict':'changes_requested','findings':[
            {'severity':'medium','file':'toy_math.py','line':None,'explanation':'Confirmed defect'}]}
        raw=json.dumps(value)
        for text in (raw,'  ```json\r\n'+raw+'\r\n```\n'):
            self.assertEqual(review_json(text,C.APP['REVIEW_SCHEMA']),value)
        for text in ('prefix '+raw,'```json\n'+raw+'\n```\nextra',raw+raw,'```json\n{broken}\n```'):
            with self.assertRaises(ValueError): review_json(text,C.APP['REVIEW_SCHEMA'])
        bad=dict(value,verdict='invented')
        with self.assertRaises((ValueError,RuntimeError)):
            review_json(json.dumps(bad),C.APP['REVIEW_SCHEMA'])

    def test_status_publication_retries_transient_windows_denials_and_stops(self):
        import ctypes
        from types import SimpleNamespace
        folder=C.DATA/('status_retry_'+uuid.uuid4().hex);folder.mkdir()
        path=folder/'status.json';C.atomic_json(path,{'phase':'before'})
        native=ctypes.WinDLL('kernel32',use_last_error=True).MoveFileExW
        from ctypes import wintypes
        native.argtypes=[wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD]
        native.restype=wintypes.BOOL
        calls=[]
        def move(*args):
            calls.append(args)
            if len(calls)<3: ctypes.set_last_error(32);return 0
            return native(*args)
        with patch.object(J.ctypes,'WinDLL',return_value=SimpleNamespace(MoveFileExW=move)),patch.object(J.time,'sleep'):
            C.atomic_json(path,{'phase':'after'})
        self.assertEqual(len(calls),3)
        self.assertEqual(C.read_json(path),{'phase':'after'})
        calls.clear()
        def denied(*args): calls.append(args);ctypes.set_last_error(5);return 0
        with patch.object(J.ctypes,'WinDLL',return_value=SimpleNamespace(MoveFileExW=denied)),patch.object(J.time,'sleep'):
            with self.assertRaises(OSError): C.atomic_json(path,{'phase':'never'})
        self.assertEqual(len(calls),3)
        self.assertEqual(C.read_json(path),{'phase':'after'})

    def test_objective_is_never_silently_truncated(self):
        folder=C.DATA/('objective_'+uuid.uuid4().hex);folder.mkdir()
        store=Store(folder/'state.sqlite3');objective='x'*3999+'Z'
        ident=store.add('project_development',objective)
        self.assertEqual(store.get(ident)['objective'],objective)
        for invalid in ('', 'x'*4001,None):
            with self.assertRaises(ValueError):store.add('project_development',invalid)

    def fixture(self, *, addition=False):
        f = FIX.create()[0]
        paths = f['editable_paths'] + (['nested/added.py'] if addition else [])
        run = W.prepare(f['summary'], f['objective'], paths, f['test_id'], dict(M.DEFAULTS))
        (run / 'repo/calculator.py').write_text('def add(a, b):\n    return a + b\n')
        if addition:
            (run / 'repo/nested/added.py').write_text('VALUE = 1\n')
        P.git(run / 'repo', 'add', '--all'); P.git(run / 'repo', 'commit', '-m', 'Synthetic reviewed result')
        store_folder = C.DATA / ('recovery_test_' + uuid.uuid4().hex[:8]); store_folder.mkdir()
        store = Store(store_folder / 'state.sqlite3')
        tid = store.add('project_development', 'Synthetic accepted candidate', {'settings': M.DEFAULTS})
        store.approve(tid, 'test'); task = store.claim('test')
        result = {'verdict': 'PASS', 'scope': 'SIMULATED_PROJECT_DEVELOPMENT', 'run': str(run),
            'candidate_sha': P.git(run / 'repo', 'rev-parse', 'HEAD'), 'candidate_inventory': C.APP['inventory'](run / 'repo'),
            'changed_files': paths}
        store.finish(tid, task['run_id'], 'awaiting_acceptance', result)
        store.decide(tid, result['candidate_sha'], True)
        evidence = C.DATA / 'jobs' / ('recovery_test_' + uuid.uuid4().hex)
        evidence.mkdir(parents=True)
        return f, run, store, tid, evidence

    def test_process_death_releases_lock_and_durable_recovery_requires_approval(self):
        f, run, store, tid, evidence = self.fixture()
        payload = A.proposal(store, tid)
        payload_path = evidence / 'approved_payload.json'; payload_path.write_text(json.dumps(payload))
        script = ('import sys,json,os\nfrom pathlib import Path\n'
            'sys.path.insert(0,sys.argv[1])\nfrom dani_agent import project_apply as A\n'
            'from dani_agent.state import Store\n'
            'def crash(path,expected,content):\n Path(path).write_bytes(b"synthetic partial write"); os._exit(73)\n'
            'A.update_exact=crash\nA.apply(Store(sys.argv[2]),json.loads(Path(sys.argv[3]).read_text()),Path(sys.argv[4]))\n')
        child = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(ROOT), str(store.path),
            str(payload_path), str(evidence)], capture_output=True, timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(child.returncode, 73, child.stderr.decode(errors='replace'))
        original = Path(f['summary']['path'])
        self.assertFalse((original / '.git/index.lock').exists())
        journal = evidence / 'apply_journal.json'
        self.assertEqual(json.loads(journal.read_text())['state'], 'APPLYING')
        with self.assertRaisesRegex(RuntimeError, 'REQUIRES_EXPLICIT_RECOVERY'):
            A.proposal(store, tid)
        recovery = A.recovery_proposal(journal)
        self.assertTrue(recovery['files']['calculator.py']['possibly_concurrent_or_partial'])
        recovery_id = store.add('project_recover', 'Restore interrupted synthetic transfer', recovery)
        self.assertEqual(store.get(recovery_id)['state'], 'awaiting_approval')
        self.assertIsNone(store.claim('test'))
        store.approve(recovery_id, 'test'); claimed = store.claim('test')
        recovery_evidence = C.DATA / 'jobs' / recovery_id; recovery_evidence.mkdir()
        r = A.recover(store, recovery, recovery_evidence)
        store.finish(recovery_id, claimed['run_id'], 'completed', r)
        self.assertEqual(r['verdict'], 'PASS')
        self.assertEqual((original / 'calculator.py').read_text(), 'def add(a, b):\n    return a - b\n')
        self.assertEqual(json.loads(journal.read_text())['state'], 'RECOVERED')
        self.assertEqual((recovery_evidence / 'before_recovery/0').read_bytes(), b'synthetic partial write')
        self.assertFalse((original / '.git/index.lock').exists())

    def test_multi_file_failure_restores_files_and_removes_only_owned_empty_directories(self):
        f, run, store, tid, evidence = self.fixture(addition=True)
        original = Path(f['summary']['path'])
        real = A.update_exact
        calls = []
        def update(path, before, after):
            calls.append(Path(path).name)
            if Path(path).name == 'added.py' and after is not None:
                raise OSError('Synthetic second-file failure')
            return real(path, before, after)
        with patch.object(A, 'update_exact', update):
            with self.assertRaisesRegex(RuntimeError, 'ORIGINALS_RESTORED'):
                A.apply(store, A.proposal(store, tid), evidence)
        self.assertIn('added.py', calls)
        self.assertEqual((original / 'calculator.py').read_text(), 'def add(a, b):\n    return a - b\n')
        self.assertFalse((original / 'nested').exists())
        self.assertFalse((original / '.git/index.lock').exists())
        self.assertEqual(json.loads((evidence / 'apply_journal.json').read_text())['state'], 'ROLLED_BACK')

    def test_journal_save_failure_after_rollback_reports_outcome_and_stays_blocked(self):
        # Review 2026-10-21 M1: a failed final journal save must not mask the rollback outcome.
        f, run, store, tid, evidence = self.fixture(addition=True)
        original = Path(f['summary']['path'])
        real_update, real_save = A.update_exact, J.save
        def update(path, before, after):
            if Path(path).name == 'added.py' and after is not None:
                raise OSError('Synthetic second-file failure')
            return real_update(path, before, after)
        def save(path, value):
            if value.get('state') == 'ROLLED_BACK':
                raise OSError('Synthetic journal write denial')
            return real_save(path, value)
        with patch.object(A, 'update_exact', update), patch.object(J, 'save', save):
            with self.assertRaisesRegex(RuntimeError, '^APPLY_FAILED_ORIGINALS_RESTORED_JOURNAL_NOT_UPDATED_RECOVERY_REQUIRED$'):
                A.apply(store, A.proposal(store, tid), evidence)
        self.assertEqual((original / 'calculator.py').read_text(), 'def add(a, b):\n    return a - b\n')
        self.assertNotIn(json.loads((evidence / 'apply_journal.json').read_text())['state'], J.TERMINAL)
        with self.assertRaisesRegex(RuntimeError, 'REQUIRES_EXPLICIT_RECOVERY'):
            A.proposal(store, tid)

    def test_valid_journal_disagreeing_with_registry_stays_blocked(self):
        # Review 2026-10-21 M2: an edited/restored terminal journal must not clear the registered block.
        f, run, store, tid, evidence = self.fixture()
        original = Path(f['summary']['path'])
        A.apply(store, A.proposal(store, tid), evidence)
        journal = evidence / 'apply_journal.json'
        self.assertFalse(any(p['project_path'] == str(original) for p in J.pending(C.DATA)))
        value = json.loads(journal.read_text())
        value['candidate_sha'] = 'f' * 40
        J.save(journal, value)
        entry = next(p for p in J.pending(C.DATA) if p['project_path'] == str(original))
        self.assertEqual(entry['state'], 'RECOVERY_REQUIRED')

    def test_concurrent_human_edit_is_kept_and_does_not_block_the_project(self):
        # Review 2026-10-21 (third): a file refused before any write must not be a failed rollback.
        f, run, store, tid, evidence = self.fixture(addition=True)
        original = Path(f['summary']['path'])
        real_save, edited = J.save, []
        def save(path, value):
            real_save(path, value)
            if value.get('state') == 'APPLYING' and not edited:   # the human saves just as we start
                edited.append(value['writing'])
                (original / value['writing']).write_text('# human edit in progress\n')
        with patch.object(J, 'save', save):
            with self.assertRaisesRegex(RuntimeError, '^APPLY_FAILED_ORIGINALS_RESTORED$'):
                A.apply(store, A.proposal(store, tid), evidence)
        self.assertEqual((original / edited[0]).read_text(), '# human edit in progress\n')
        self.assertFalse((original / 'nested').exists())
        self.assertEqual(json.loads((evidence / 'apply_journal.json').read_text())['state'], 'ROLLED_BACK')
        self.assertFalse(any(p['project_path'] == str(original) for p in J.pending(C.DATA)))

    def test_recovery_scope_change_never_overwrites_new_human_edits(self):
        f, run, store, tid, evidence = self.fixture()
        original = Path(f['summary']['path'])
        backup = evidence / 'original_files'; backup.mkdir()
        source = original / 'calculator.py'; before = source.read_bytes()
        J.write(backup / '0', before)
        journal = evidence / 'apply_journal.json'
        J.save(journal, {'state':'RECOVERY_REQUIRED','project_path':str(original),'candidate_sha':'synthetic',
            'task_id':tid,'created_directories':{},'files':{'calculator.py':{'before':J.digest(before),'after':'synthetic','backup':'0'}}})
        payload = A.recovery_proposal(journal)
        source.write_text('# New human edit\n')
        output = evidence / 'recovery'; output.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'RECOVERY_SCOPE_CHANGED'):
            A.recover(store, payload, output)
        self.assertEqual(source.read_text(), '# New human edit\n')
        # Close this synthetic journal without touching the simulated human edit.
        value = json.loads(journal.read_text()); value['state']='RECOVERED'; J.save(journal,value)

    def test_foreign_git_lock_and_dangerous_config_are_not_removed_or_executed(self):
        folder = C.DATA / ('lock_test_' + uuid.uuid4().hex); folder.mkdir()
        lock = folder / 'index.lock'; lock.write_text('someone else')
        with self.assertRaises(OSError):
            with temporary_lock(lock): self.fail('Must not acquire existing lock')
        self.assertEqual(lock.read_text(), 'someone else')
        P.validate_config('[core]\nrepositoryformatversion=0\n[remote "origin"]\nurl=https://example.invalid/repo\nfetch=+refs/heads/*:refs/remotes/origin/*\n')
        for config in ('[core]\nsshCommand=NEVER_EXECUTE', '[credential]\nhelper=NEVER_EXECUTE',
                '[diff "x"]\ntextconv=NEVER_EXECUTE', '[include]\npath=private', '[core]\nworktree=elsewhere'):
            with self.assertRaisesRegex(RuntimeError, 'GIT_CONFIG_REQUIRES_REVIEW'):
                P.validate_config(config)

    def test_future_database_version_is_not_silently_downgraded(self):
        folder = C.DATA / ('future_db_' + uuid.uuid4().hex); folder.mkdir()
        path = folder / 'state.sqlite3'
        with sqlite3.connect(path) as db: db.execute('PRAGMA user_version=3')
        with self.assertRaisesRegex(RuntimeError, 'NEWER_THAN_APPLICATION'): Store(path)
        with sqlite3.connect(path) as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_missing_model_evidence_and_oversized_working_file_fail_safely(self):
        folder = C.DATA / ('limits_test_' + uuid.uuid4().hex); folder.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'SUBSCRIPTION_MODEL_EVIDENCE_REQUIRED'):
            M.availability(M.DEFAULTS, evidence=folder)
        p = folder / 'large'; p.write_bytes(b'x' * 17)
        with self.assertRaisesRegex(RuntimeError, 'WORKING_FILE_SIZE_LIMIT'): P.bounded_read(p, 16)

    def test_report_failure_preserves_root_outcome_and_blocks_acceptance(self):
        from dani_agent.reports import finalize
        folder = C.DATA / ('report_failure_' + uuid.uuid4().hex); folder.mkdir()
        (folder / 'REPORT.md').write_text('Preserve previous report')
        result = {'verdict':'FAIL','reason':'VERIFIED_ORIGINAL_FAILURE'}
        finalize(folder, result, 'must not overwrite')
        self.assertEqual(result['reason'], 'FINAL_REPORT_PUBLICATION_FAILED')
        self.assertEqual(result['outcome_before_report_failure']['reason'], 'VERIFIED_ORIGINAL_FAILURE')
        self.assertEqual((folder / 'REPORT.md').read_text(), 'Preserve previous report')
        self.assertEqual(json.loads((folder / 'result.json').read_text()), result)

    def test_hard_link_file_transfer_never_changes_the_other_name(self):
        from dani_agent.file_transfer import update_exact
        folder = C.DATA / ('link_transfer_' + uuid.uuid4().hex); folder.mkdir()
        source = folder / 'source'; source.write_bytes(b'original')
        alias = folder / 'alias'
        try:
            os.link(source, alias)
        except PermissionError:
            if C.APP['PROCESS']['DIAGNOSTICS']['execution_context']().get('appcontainer') is not True:
                raise
            # This executor cannot create links. Exercise the handle-metadata
            # rejection here; normal-host offline checks execute the real link.
            from types import SimpleNamespace
            with patch('dani_agent.file_transfer.os.fstat', return_value=SimpleNamespace(st_nlink=2, st_file_attributes=0)):
                with self.assertRaisesRegex(RuntimeError, 'FILE_LINK_CHANGED'):
                    update_exact(source, b'original', b'changed')
            proof = 'SIMULATED_HANDLE_METADATA_LINK_CREATION_DENIED_IN_APPCONTAINER'
        else:
            with self.assertRaisesRegex(RuntimeError, 'FILE_LINK_CHANGED'):
                update_exact(source, b'original', b'changed')
            self.assertEqual(alias.read_bytes(), b'original')
            proof = 'ACTUAL_HARD_LINK_REJECTED'
        self.assertEqual(source.read_bytes(), b'original')
        (folder / 'proof.json').write_text(json.dumps({'scope':proof,'model_calls':0}))

    def test_cancel_before_worker_start_does_not_launch_process(self):
        folder = C.DATA / ('cancel_start_' + uuid.uuid4().hex); folder.mkdir()
        store = Store(folder / 'dani_agent.sqlite3')
        tid = store.add('project_development', 'Cancel before start', {'settings':M.DEFAULTS})
        store.approve(tid,C.fingerprint()); task = store.claim(C.fingerprint())
        with patch.object(C,'DATA',folder):
            engine=C.Coordinator(store); engine.cancel()
            with patch.dict(C.APP['PROCESS'], {'run':lambda *a,**k: self.fail('Must not launch a worker')}):
                engine._execute(task)
        self.assertEqual(store.get(tid)['state'], 'cancelled')

    def test_source_change_aborts_outer_containment_watch(self):
        folder = C.DATA / ('source_watch_' + uuid.uuid4().hex); folder.mkdir()
        store = Store(folder / 'dani_agent.sqlite3')
        tid=store.add('milestone_review','Synthetic approved review')
        store.approve(tid,'old');task=store.claim('old')
        with patch.object(C,'DATA',folder), patch.object(C,'fingerprint',return_value='changed'):
            engine=C.Coordinator(store)
            def process(spec,directory,**kwargs):
                self.assertEqual(spec['kind'],'plain')
                kwargs['watch']()
                self.fail('Watch must abort before provider execution')
            with patch.dict(C.APP['PROCESS'],{'run':process}): engine._execute(task)
        self.assertEqual(store.get(tid)['result']['reason'],'SOURCE_CHANGED_DURING_APPROVED_WORKFLOW')
        self.assertIsNone(store.claim('changed'))

    def test_project_cancellation_records_attempt_and_unknown_usage_without_claude(self):
        from dani_agent import project_workflow as F
        f=FIX.create()[0]
        run=W.prepare(f['summary'], f['objective'],f['editable_paths'],f['test_id'],dict(M.DEFAULTS))
        def process(spec,directory,**kwargs):
            raise C.APP['PROCESS']['ProcessFailure']('USER_CANCELLED',{'containment':{'all_observed_processes_stopped':True}})
        result=F.execute(run,runner=process,simulated=True)
        self.assertEqual(result['reason'],'USER_CANCELLED')
        self.assertEqual(result['agent_sessions'],{'codex':1,'claude':0})
        self.assertIsNone(result['usage'][0]['tokens'])
        self.assertEqual(P.discover(f['summary']['path'])['digest'],f['summary']['digest'])

    def test_restart_of_uncertain_project_task_never_replays_models(self):
        folder=C.DATA/('project_restart_'+uuid.uuid4().hex);folder.mkdir()
        store=Store(folder/'dani_agent.sqlite3')
        tid=store.add('project_development','Interrupted project task',{'settings':M.DEFAULTS})
        store.approve(tid,'source');store.claim('source')
        with patch.object(C,'DATA',folder), patch('subprocess.run') as process:
            C.recover_saved_results(store)
            process.assert_not_called()
        self.assertEqual(store.get(tid)['state'],'interrupted')
        self.assertIsNone(store.claim('source'))

    def test_finished_legacy_run_is_untouched_before_finalization_or_any_agent(self):
        from dani_agent import collaboration
        folder=C.DATA/('finished_run_'+uuid.uuid4().hex);folder.mkdir()
        path=folder/'result.json';path.write_text('{"reason":"preserved evidence"}')
        before=path.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'FINISHED_RUN_NO_REPLAY'):
            collaboration.execute(folder,{},runner=lambda *a,**k:self.fail('No process may start'))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual([p.name for p in folder.iterdir()],['result.json'])

    def test_current_milestone_gate_rejects_simulation_missing_approval_and_material_findings(self):
        from dani_agent import milestone_review as R
        folder=C.DATA/('milestone_gate_'+uuid.uuid4().hex);folder.mkdir()
        candidate=folder/'candidate';artifacts=candidate/'artifacts';artifacts.mkdir(parents=True)
        sources={'synthetic.py':'fixture only'}
        digest=hashlib.sha256(json.dumps(sources,sort_keys=True).encode()).hexdigest()
        (artifacts/'milestone_plan.json').write_text(json.dumps({'fingerprint':C.fingerprint(),'candidate_sha':digest,'sources':sources}))
        result={'scope':'SIMULATED_MILESTONE_REVIEW','verdict':'PASS','candidate_sha':digest,
            'review':{'verdict':'pass','findings':[]}}
        def save(): (candidate/'result.json').write_text(json.dumps(result))
        save()
        # All synthetic gate evidence stays OUTSIDE production prototype/runs.
        with patch.object(R,'RUNS',folder),patch.object(R,'review_sources',return_value=sources):
            self.assertFalse(R.verified_current())
            result['scope']='LIVE_MILESTONE_REVIEW';save()
            self.assertFalse(R.verified_current())
            (artifacts/'local_live_approval.json').write_text('{}')
            self.assertTrue(R.verified_current())
            result['review']['findings']=[{'severity':'medium'}];save()
            self.assertFalse(R.verified_current())
            result['review']['findings']=[{'severity':'low'}];save()
            self.assertTrue(R.verified_current())
            with patch.object(R,'fingerprint',return_value='new source'):
                self.assertFalse(R.verified_current())
            sources['reviewed_test.py']='changed after review'
            self.assertFalse(R.verified_current())

    def test_corrupt_unregistered_journal_is_visible_and_blocks_without_crashing(self):
        folder=C.DATA/('bad_journal_'+uuid.uuid4().hex);journal=folder/'jobs/test/apply_journal.json'
        journal.parent.mkdir(parents=True);journal.write_text('{broken')
        entries=J.pending(folder)
        self.assertEqual(entries[0]['state'],'RECOVERY_JOURNAL_CORRUPT')
        self.assertIsNone(entries[0]['project_path'])
        with self.assertRaisesRegex(RuntimeError,'REQUIRES_EXPLICIT_RECOVERY'): J.ensure_clear(folder,folder/'any_project')

    def test_registered_missing_or_corrupt_journal_uses_baseline_and_preserves_bad_evidence(self):
        for mode in ('missing','corrupt'):
            f,run,store,tid,evidence=self.fixture()
            original=Path(f['summary']['path'])
            A.apply(store,A.proposal(store,tid),evidence)
            journal=evidence/'apply_journal.json'
            if mode=='missing': journal.unlink()  # NEW disposable test evidence only.
            else: journal.write_bytes(b'{synthetic corrupt record')
            self.assertTrue(any(p['project_path']==str(original) for p in J.pending(C.DATA)))
            with self.assertRaisesRegex(RuntimeError,'REQUIRES_EXPLICIT_RECOVERY'): A.proposal(store,tid)
            payload=A.recovery_proposal(journal)
            recovery=evidence/'recovery';recovery.mkdir()
            result=A.recover(store,payload,recovery)
            self.assertEqual(result['verdict'],'PASS')
            self.assertIn('a - b',(original/'calculator.py').read_text())
            if mode=='corrupt': self.assertEqual((recovery/'journal_before_recovery.bin').read_bytes(),b'{synthetic corrupt record')
            self.assertEqual(json.loads(journal.read_text())['state'],'RECOVERED')

    def test_lost_job_evidence_does_not_remove_independent_recovery_block(self):
        folder=C.DATA/('registry_test_'+uuid.uuid4().hex);folder.mkdir()
        journal=folder/'jobs/missing_job/apply_journal.json'
        baseline={'state':'PREPARED','project_path':str(folder/'original'),'candidate_sha':'synthetic',
            'files':{},'created_directories':{},'directory_intents':[]}
        J.register(folder,journal,baseline)
        self.assertFalse(journal.parent.exists())
        entries=J.pending(folder)
        self.assertEqual(entries[0]['project_path'],str(folder/'original'))
        with self.assertRaisesRegex(RuntimeError,'REQUIRES_EXPLICIT_RECOVERY'): J.ensure_clear(folder,folder/'original')
        record=next((folder/'apply_registry').glob('*.json'));record.write_text('broken')
        self.assertEqual(J.pending(folder)[0]['state'],'RECOVERY_REGISTRY_CORRUPT')

    def test_crash_after_mkdir_has_durable_intent_and_explicit_directory_recovery(self):
        f,run,store,tid,evidence=self.fixture(addition=True)
        payload=A.proposal(store,tid);payload_path=evidence/'approved_payload.json';payload_path.write_text(json.dumps(payload))
        script=('import sys,json,os\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\n'
            'from dani_agent import project_apply as A\nfrom dani_agent.state import Store\noriginal=A.J.save\n'
            'def save(path,value):\n if value.get("created_directories"): os._exit(74)\n original(path,value)\n'
            'A.J.save=save\nA.apply(Store(sys.argv[2]),json.loads(Path(sys.argv[3]).read_text()),Path(sys.argv[4]))\n')
        child=subprocess.run([sys.executable,'-I','-B','-c',script,str(ROOT),str(store.path),str(payload_path),str(evidence)],capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(child.returncode,74,child.stderr.decode(errors='replace'))
        journal=evidence/'apply_journal.json';record=json.loads(journal.read_text())
        self.assertIn('nested',record['directory_intents']);self.assertEqual(record['created_directories'],{})
        recovery=A.recovery_proposal(journal)
        self.assertFalse(recovery['directories']['nested']['confirmed_created_by_apply'])
        ident=store.add('project_recover','Restore owned fixture and explicitly approve empty intent directory cleanup',recovery)
        self.assertEqual(store.get(ident)['state'],'awaiting_approval')
        store.approve(ident,'fixture');claimed=store.claim('fixture')
        output=evidence/'recovery';output.mkdir();result=A.recover(store,recovery,output)
        store.finish(ident,claimed['run_id'],'completed',result)
        original=Path(f['summary']['path']);self.assertFalse((original/'nested').exists())
        self.assertIn('a - b',(original/'calculator.py').read_text())

    def test_partial_transfer_and_failed_rollback_remain_blocked_until_exact_recovery(self):
        f,run,store,tid,evidence=self.fixture();source=Path(f['summary']['path'])/'calculator.py'
        def broken(path,expected,content):
            Path(path).write_bytes(b'partial')
            raise RuntimeError('FILE_TRANSFER_INCOMPLETE_BACKUP_REQUIRED')
        with patch.object(A,'update_exact',broken):
            with self.assertRaisesRegex(RuntimeError,'ROLLBACK_INCOMPLETE'): A.apply(store,A.proposal(store,tid),evidence)
        journal=evidence/'apply_journal.json'
        self.assertEqual(json.loads(journal.read_text())['state'],'RECOVERY_REQUIRED')
        self.assertEqual(json.loads((evidence/'rollback.json').read_text())['cause'],'FILE_TRANSFER_INCOMPLETE_BACKUP_REQUIRED')
        with self.assertRaisesRegex(RuntimeError,'REQUIRES_EXPLICIT_RECOVERY'): A.proposal(store,tid)
        payload=A.recovery_proposal(journal);output=evidence/'recovery';output.mkdir()
        A.recover(store,payload,output)
        self.assertIn('a - b',source.read_text())

    def test_model_free_diagnostic_never_publishes_a_native_live_gate(self):
        from dani_agent import project_workflow as F
        f=FIX.create()[0];run=W.prepare(f['summary'],f['objective'],f['editable_paths'],f['test_id'],dict(M.DEFAULTS))
        def process(spec,directory,**kwargs):
            self.assertIn('sandbox',spec['argv']);self.assertNotIn('exec',spec['argv'])
            self.assertEqual(spec['kind'],'plain')
            return {'exit_code':0,'stdout':'DANI_PROJECT_SECURITY_OK','containment':{k:True for k in
                ('assigned_before_resume','job_queries_passed','job_empty_after_termination','job_termination_succeeded','all_observed_processes_stopped')}}
        with patch.dict(C.APP['PROCESS'],{'run':process}),patch.dict(C.APP['PROCESS']['DIAGNOSTICS'],{'execution_context':lambda:{'appcontainer':False,'elevated':False}}):
            result=F.diagnose(run)
        self.assertEqual(result['verdict'],'PASS');self.assertEqual(result['model_calls'],0)
        self.assertEqual(result['scope'],'MODEL_FREE_PROJECT_PROBE_DIAGNOSTIC')
        self.assertFalse((run/'artifacts/project_preflight.json').exists())
        with self.assertRaises(FileNotFoundError): F.check_ready(run)

    def test_journal_sharing_failure_preserves_old_snapshot_and_allows_publication_after_close(self):
        from dani_agent.file_transfer import shared_reader
        folder=C.DATA/('journal_sharing_'+uuid.uuid4().hex);folder.mkdir()
        path=folder/'journal.json'
        J.save(path,{'state':'old'})
        with shared_reader(path) as stream:
            try:
                J.save(path,{'state':'new'})
            except OSError as exc:
                self.assertIn(exc.winerror,(5,32,33))
                self.assertEqual(json.loads(path.read_text())['state'],'old')
            self.assertEqual(json.loads(stream.read())['state'],'old')
        J.save(path,{'state':'new'})
        self.assertEqual(json.loads(path.read_text())['state'],'new')

    def test_generated_oversized_file_is_rejected_before_unbounded_inventory_read(self):
        folder=C.DATA/('bounded_inventory_'+uuid.uuid4().hex);folder.mkdir()
        (folder/'source.py').write_bytes(b'x'*(128*1024+1))
        with self.assertRaisesRegex(RuntimeError,'WORKING_FILE_SIZE_LIMIT'):
            W.project_inventory(folder,['source.py'])
        (folder/'source.py').write_bytes(b'value=1\n')
        self.assertEqual(W.project_inventory(folder,['source.py']),{'source.py':hashlib.sha256(b'value=1\n').hexdigest()})


if __name__ == '__main__':
    unittest.main()
