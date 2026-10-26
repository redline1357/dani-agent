"""Run-bound completion framing, NOT security attestation of arbitrary test code."""
import json
import re
from urllib.parse import quote

PREFIX = 'DANI_TEST_COMPLETE='
SUPPORTED = {'python-unittest', 'node-test'}
NODE_REPORTER = '''export default async function*(events){
let summary, files=new Set(), cases=0;
for await(const e of events){
 if(e.type==='test:summary'){
  if(e.data.file){if(!e.data.counts.tests)throw Error('EMPTY_TEST_FILE');files.add(e.data.file);cases+=e.data.counts.tests;}else summary=e.data;
 }
 if(e.type==='test:fail')yield 'DANI_TEST_LOG='+JSON.stringify({name:e.data.name,error:String(e.data.details?.error?.message||'test failed')}).slice(0,1500)+'\\n';
}
if(!summary||files.size!==EXPECTED_FILES||!cases)throw Error('TEST_SUITE_COMPLETION_MISSING');
yield 'DANI_TEST_COMPLETE='+JSON.stringify({nonce:NONCE,counts:{tests:summary.counts.tests,failures:summary.counts.failed,errors:summary.counts.cancelled,skipped:summary.counts.skipped+summary.counts.todo}})+'\\n';
}'''


def command(argv, test_id, nonce):
    if test_id not in SUPPORTED or not re.fullmatch('[0-9a-f]{32}', nonce):
        raise RuntimeError('PROJECT_TEST_COMPLETION_DRIVER_REQUIRED')
    argv=list(argv)
    if test_id=='python-unittest':
        argv.append(nonce)
    else:
        files=len(argv)-argv.index('--test')-1
        if not files:raise RuntimeError('PROJECT_NODE_TEST_FILES_REQUIRED')
        source=NODE_REPORTER.replace('NONCE',json.dumps(nonce)).replace('EXPECTED_FILES',str(files))
        reporter='data:text/javascript,'+quote(source,safe='')
        argv.insert(argv.index('--test')+1,'--test-reporter='+reporter)
    return argv


def summary(test_id, output, exit_code, nonce, *, allow_failed=False):
    if exit_code!=0 and not allow_failed:
        return None
    if test_id not in SUPPORTED or type(nonce) is not str or not re.fullmatch('[0-9a-f]{32}',nonce):
        raise RuntimeError('PROJECT_TEST_COMPLETION_REQUIRED')
    lines=[line[len(PREFIX):] for line in output.splitlines() if line.startswith(PREFIX)]
    if len(lines)!=1:
        raise RuntimeError('PROJECT_TEST_UNIQUE_COMPLETION_REQUIRED')
    try:
        frame=json.loads(lines[0]);value=frame['counts']
        if set(frame)!={'nonce','counts'} or frame['nonce']!=nonce:
            raise ValueError('wrong invocation')
        if (set(value)!={'tests','failures','errors','skipped'} or any(type(v) is not int or v<0 for v in value.values())
                or not value['tests'] or value['skipped']>value['tests']):
            raise ValueError('not successful')
        if allow_failed:
            if exit_code != (1 if value['failures'] or value['errors'] else 0):
                raise ValueError('inconsistent exit')
        elif value['failures'] or value['errors'] or value['skipped']==value['tests']:
            raise ValueError('not successful')
    except (ValueError,KeyError,TypeError):
        raise RuntimeError('PROJECT_TEST_COMPLETION_INVALID') from None
    return {**value,'source':'RUN_BOUND_RUNNER_REPORT','security_attestation':False}
