"""Trusted unittest entry point; invoked ONLY inside MXC for project tests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


def use_runtime_temp(root):
    """MXC swaps TEMP for an unwritable per-launch AppContainer folder; use the run's runtime folder."""
    os.environ['TEMP'] = os.environ['TMP'] = str(Path(root).parent / 'runtime')
    tempfile.tempdir = None


def main():
    root = Path(sys.argv[1]).resolve()
    nonce=sys.argv[2]
    use_runtime_temp(root)
    sys.argv=[sys.argv[0]]
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(root), pattern='test*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('DANI_TEST_COMPLETE=' + json.dumps({'nonce':nonce,'counts':{'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': len(result.skipped)}}),flush=True)
    raise SystemExit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)


if __name__ == '__main__':
    main()
