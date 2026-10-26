"""Publish one fresh run without letting report errors replace the root outcome."""
from .coordinator import APP


def finalize(run, report, text):
    try:
        with (run / 'REPORT.md').open('x', encoding='utf-8') as stream:
            stream.write(text)
    except OSError as exc:
        report['outcome_before_report_failure'] = {k: report.get(k) for k in ('verdict', 'reason')}
        report.update(verdict='BLOCKED', reason='FINAL_REPORT_PUBLICATION_FAILED', report_error=type(exc).__name__)
    APP['save'](run / 'result.json', report)
    return report
