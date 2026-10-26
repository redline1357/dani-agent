"""Variable-sized native job enumeration; no MXC or provider/model invocation."""
import ctypes as c
import json
from pathlib import Path
import runpy
import sys
import unittest
import uuid

BASE = Path(__file__).resolve().parent
PROCESS = runpy.run_path(str(BASE / "process.py"), run_name="job_membership_regression")


def fake_query(sequence, capacities):
    def query(job, information_class, buffer, size, returned):
        capacity = (size - 8) // c.sizeof(c.c_size_t)
        capacities.append(capacity)
        ok, error, assigned, listed = sequence.pop(0)
        header = c.cast(buffer, c.POINTER(c.c_uint32 * 2)).contents
        header[0], header[1] = assigned, listed
        entries = (c.c_size_t * capacity).from_address(c.cast(buffer, c.c_void_p).value + 8)
        for index in range(min(capacity, listed)):
            entries[index] = index + 1
        c.set_last_error(error)
        return ok
    return query


class JobMembershipTests(unittest.TestCase):
    def test_more_data_and_growth_race_resize_until_complete(self):
        capacities, statistics = [], {}
        query = fake_query([(False, 234, 20, 16), (False, 234, 33, 32), (True, 0, 33, 33)], capacities)
        pids = PROCESS["job_pids"]({"queryjob": query}, None, statistics)
        self.assertEqual(pids, list(range(1, 34)))
        self.assertEqual(capacities, [16, 32, 64])
        self.assertEqual(statistics["pid_buffer_resizes"], 2)

    def test_successful_but_truncated_list_is_not_accepted(self):
        capacities = []
        query = fake_query([(True, 0, 19, 16), (True, 0, 19, 19)], capacities)
        self.assertEqual(len(PROCESS["job_pids"]({"queryjob": query}, None, {})), 19)
        self.assertEqual(capacities, [16, 32])

    def test_real_api_errors_still_fail_closed_with_winerror(self):
        query = fake_query([(False, 5, 0, 0)], [])
        statistics = {}
        with self.assertRaisesRegex(RuntimeError, "JOB_QUERY_FAILED_WINERROR_5"):
            PROCESS["job_pids"]({"queryjob": query}, None, statistics)
        self.assertEqual(statistics["fatal_job_query_winerror"], 5)

    def test_query_memory_remains_bounded(self):
        query = fake_query([(False, 234, 5000, 16)], [])
        with self.assertRaisesRegex(RuntimeError, "JOB_PID_QUERY_MEMORY_BOUND_EXCEEDED"):
            PROCESS["job_pids"]({"queryjob": query}, None, {})

    def test_real_more_than_sixteen_children_remain_contained_and_are_killed(self):
        folder = BASE / ("job_membership_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        code = ('import subprocess,sys,time; '
            'children=[subprocess.Popen([sys.executable,"-I","-B","-c","import time; time.sleep(15)"],'
            'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(20)]; '
            'print("TWENTY_CHILDREN_READY",flush=True); time.sleep(0.8)')
        result = PROCESS["run"]({"argv": [sys.executable, "-I", "-B", "-c", code], "cwd": str(folder),
            "env": {"SystemRoot": r"C:\Windows"}, "kind": "plain"}, folder, timeout=10)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("TWENTY_CHILDREN_READY", result["stdout"])
        containment = result["containment"]
        self.assertTrue(containment["assigned_before_resume"])
        self.assertTrue(containment["job_queries_passed"])
        self.assertGreater(containment["pid_query_statistics"]["max_job_processes"], 16)
        self.assertGreater(containment["pid_query_statistics"]["pid_buffer_resizes"], 0)
        self.assertTrue(containment["job_termination_succeeded"])
        self.assertTrue(containment["job_empty_after_termination"])
        self.assertTrue(containment["all_observed_processes_stopped"])
        with (folder / "verification.json").open("x", encoding="utf-8") as output:
            json.dump({"model_calls": 0, "result": result}, output, indent=2)

    def test_timeout_kills_entire_large_job_and_preserves_evidence(self):
        folder = BASE / ("job_timeout_" + uuid.uuid4().hex[:8])
        folder.mkdir()
        code = ('import subprocess,sys,time; '
            'children=[subprocess.Popen([sys.executable,"-I","-B","-c","import time; time.sleep(15)"],'
            'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(20)]; '
            'time.sleep(15)')
        with self.assertRaisesRegex(RuntimeError, "TIMEOUT"):
            PROCESS["run"]({"argv": [sys.executable, "-I", "-B", "-c", code], "cwd": str(folder),
                "env": {"SystemRoot": r"C:\Windows"}, "kind": "plain"}, folder, timeout=3)
        evidence = list(folder.glob("*.containment.json"))
        self.assertEqual(len(evidence), 1)
        containment = json.loads(evidence[0].read_text())
        self.assertEqual(containment["termination_reason"], "TIMEOUT")
        self.assertGreater(containment["pid_query_statistics"]["max_job_processes"], 16)
        self.assertTrue(containment["job_queries_passed"])
        self.assertTrue(containment["job_empty_after_termination"])
        self.assertTrue(containment["all_observed_processes_stopped"])


if __name__ == "__main__":
    unittest.main()
