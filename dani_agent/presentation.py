"""Readable acceptance evidence; no provider calls or candidate execution."""
import json
from .coordinator import APP


def acceptance_text(task):
    result = task.get('result') or {}
    lines = ['Final status: ' + task['state'].upper(), 'Automated verdict: ' + result.get('verdict', 'NOT RUN'),
        'Reason: ' + result.get('reason', 'Waiting for execution'), '',
        ('Snapshot digest: ' if result.get('scope') in ('LIVE_MILESTONE_REVIEW', 'SIMULATED_MILESTONE_REVIEW') else 'Git revision: ')
        + result.get('candidate_sha', 'not available')]
    changed = result.get('changed_files')
    if changed is None and result.get('candidate_inventory') and result.get('run'):
        try:
            run = APP['approved_run'](result['run'])
            before = json.loads((run / 'control/baseline.json').read_text())['inventory']
            after = result['candidate_inventory']
            changed = sorted(name for name in before.keys() | after.keys() if not name.startswith('.git/') and before.get(name) != after.get(name))
        except (OSError, ValueError, KeyError):
            changed = None
    lines += ['Changed files: ' + (', '.join(changed) if changed else 'none' if changed == [] else 'not available')]
    if result.get('tests_written_by_developer'):
        lines.append('Tests written by the developer agent (project had none for this driver): '
            + ', '.join(result['tests_written_by_developer']) + ' — passing them is weaker evidence; check them yourself.')
    lines += ['', 'Tests and independent review:']
    for cycle in result.get('cycles', []):
        tests = cycle.get('tests')
        prefix = '  Cycle ' + str(cycle['cycle'])
        lines.append(prefix + ': ' + cycle['candidate_sha'])
        if tests:
            lines += ['    Deterministic tests: ' + ('REPORTED PASS (not security-attested)' if tests['exit_code'] == 0 else 'FAIL')
                + (' [SIMULATED]' if tests.get('simulated') else ''), tests.get('output', '').strip()]
        review = cycle.get('review')
        if cycle.get('review_context_files'):
            lines.append('    Review scope: supplied files only; cross-file impact outside this context is not verified.')
            lines.append('    Supplied files: ' + ', '.join(cycle['review_context_files']))
        if review:
            lines.append('    Claude review: ' + review['verdict'] + ' (' + str(len(review['findings'])) + ' findings)')
            for finding in review['findings']:
                lines.append('      ' + finding['severity'].upper() + ' ' + finding['file'] + ':' + str(finding['line']) + ' — ' + finding['explanation'])
        for key in ('oracle_before', 'oracle_after'):
            audit = cycle.get(key)
            if audit:
                lines += ['    Acceptance oracle ' + ('before fix: ' if key == 'oracle_before' else 'after fix: ')
                    + ('PASS' if audit['exit_code'] == 0 else 'FAIL (seed reproduced)' if audit.get('seed_reproduced') else 'FAIL')
                    + (' [SIMULATED]' if audit.get('simulated') else ''), audit.get('output', '').strip()]
    if not result.get('cycles'):
        lines.append('  No completed candidate evidence yet.')
    sessions = result.get('agent_sessions')
    if sessions:
        lines += ['', 'Agent session attempts: Codex ' + str(sessions['codex']) + ', Claude ' + str(sessions['claude'])]
    decision = result.get('human_decision')
    lines += ['', 'Human decision: ' + (decision['state'] if decision else 'pending'),
        'Independently tested by human: ' + ('yes' if decision.get('independently_tested_by_human') is True else
            'no' if decision and decision.get('independently_tested_by_human') is False else 'not recorded') if decision else
            'Independently tested by human: not recorded',
        'Acceptance records your decision on this revision. It does not prove independent human testing, merge or deploy.']
    return '\n'.join(lines)


def acceptance_confirmation(task, independently_tested):
    result = task.get('result') or {}
    cycles = result.get('cycles') or []
    final = cycles[-1] if cycles else {}
    tests = final.get('tests') or {}
    review = final.get('review') or {}
    return ('Revision: ' + result.get('candidate_sha', 'unknown')
        + '\nChanged files: ' + ', '.join(result.get('changed_files') or ['see acceptance screen'])
        + '\nFinal deterministic tests: ' + ('REPORTED PASS (not security-attested)' if tests.get('exit_code') == 0 else 'not verified')
        + (' — written by the developer agent' if result.get('tests_written_by_developer') else '')
        + '\nFinal Claude review: ' + review.get('verdict', 'not verified')
        + ' (' + str(len(review.get('findings', []))) + ' findings)'
        + '\nCorrection cycles: ' + str(result.get('correction_cycles', 'unknown'))
        + '\nIndependent acceptance oracle: ' + ('PASS' if final.get('oracle_after', {}).get('exit_code') == 0 else 'not applicable')
        + '\nIndependently tested by you: ' + ('yes' if independently_tested else 'no')
        + '\n\nFull tests and findings are shown in the acceptance tab.\nThis records a decision; it does not merge or deploy.')
