"""Offline unit checks only. Candidate execution is mocked, never run unrestricted."""
import hashlib
import json
import msvcrt
from pathlib import Path
import tempfile
import unittest
import uuid

base = Path(__file__).resolve().parent

# One self-test at a time on this PC. A second one (in parallel, or started by a test inside this one) stops at
# once instead of running the whole suite again; the lock is released by Windows when this process ends.
lock = open(Path(tempfile.gettempdir()) / "dani_agent_run_checks.lock", "a+b")
try:
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
except OSError:
    print("Another Dani Agent self-test is already running on this PC; not starting a second one.")
    raise SystemExit(3)
def source_hashes():
    paths = list(base.glob("*.py")) + [base / "second_failure_f3fb4430/session_probe.py"]
    values = {path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    for path in list((base.parent / "dani_agent").glob("*.py")) + [base.parent / "DaniAgent.py", base.parent / "DaniDashboard.py"]:
        values["../" + path.relative_to(base.parent).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values

before = source_hashes()
suite = unittest.defaultTestLoader.discover(str(base), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sources_unchanged = before == source_hashes()
record = {"scope": "OFFLINE_ORCHESTRATOR_TESTS", "tests_run": result.testsRun,
          "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped),
          "verdict": "PASS" if result.wasSuccessful() and sources_unchanged else "FAIL", "model_calls": 0,
          "candidate_execution": "MOCKED_NOT_RUN", "sources_sha256": before,
          "sources_unchanged_during_tests": sources_unchanged}
destination = base / "offline_test_results.json"
if destination.exists():
    destination = base / ("offline_test_results_" + uuid.uuid4().hex + ".json")
with destination.open("x", encoding="utf-8") as output:
    json.dump(record, output, indent=2)
print("Evidence: " + str(destination))
print(json.dumps(record, indent=2))
raise SystemExit(0 if record["verdict"] == "PASS" else 1)
