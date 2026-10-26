"""Pinned-interpreter entry point for the browser dashboard (http://127.0.0.1 only)."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def message(text):
    if sys.stderr is not None:
        print(text, file=sys.stderr)
    else:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, 'Dani Agent', 0x40)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline-only', action='store_true', help='Open the dashboard without permitting model sessions.')
    args = parser.parse_args()
    from dani_agent.dashboard import serve
    try:
        serve(offline_only=args.offline_only)
    except RuntimeError as exc:
        if str(exc) == 'COORDINATOR_ALREADY_RUNNING':
            message('Dani Agent is already running. Close the other Dani Agent window first.')
            return 1
        raise
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        import json
        folder = ROOT / 'data'
        folder.mkdir(exist_ok=True)
        (folder / 'startup_error.json').write_text(json.dumps({'error': type(exc).__name__,
            'reason': str(exc) if isinstance(exc, (RuntimeError, ValueError)) else 'STARTUP_FAILED'}), encoding='utf-8')
        message('Dani Agent could not start. See data/startup_error.json.')
        raise SystemExit(1)
