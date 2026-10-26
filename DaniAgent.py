"""Pinned-interpreter entry point for the user-started GUI and contained worker."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker")
    parser.add_argument("--store")
    parser.add_argument("--job-dir")
    args = parser.parse_args()
    if args.worker:
        from dani_agent.coordinator import worker
        return worker(args.store, args.worker, args.job_dir)
    import os
    for name in ("TCLLIBPATH", "TCL_LIBRARY", "TK_LIBRARY"):
        os.environ.pop(name, None)
    from dani_agent.ui import main as ui_main
    ui_main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        import json
        folder = ROOT / "data"
        folder.mkdir(exist_ok=True)
        (folder / "startup_error.json").write_text(json.dumps({"error": type(exc).__name__,
            "reason": str(exc) if isinstance(exc, (RuntimeError, ValueError)) else "STARTUP_FAILED"}), encoding="utf-8")
        if sys.stderr is not None:
            print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "Dani Agent could not start. See data/startup_error.json.", "Dani Agent", 0x10)
        raise SystemExit(1)
