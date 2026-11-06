import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.agent_interface.catalog import ROOT

BENCH = ROOT / "tools/research_benchmark.py"
BASELINE = ROOT / "tests/benchmarks/research-quality-baseline.json"


class Phase7Tests(unittest.TestCase):
    def run_bench(self, *args):
        return subprocess.run([sys.executable, str(BENCH), *args], text=True, capture_output=True, timeout=300)

    def test_pinned_baseline_exists_and_matches(self):
        self.assertTrue(BASELINE.is_file())
        baseline = json.loads(BASELINE.read_text())
        self.assertEqual("research-quality-benchmark", baseline["kind"])
        self.assertIn("supported_findings", baseline["metrics"])

    def test_benchmark_runs_and_stays_within_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "summary.json"
            result = self.run_bench("--runs", "2", "--baseline", str(BASELINE), "--output", str(out))
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            summary = json.loads(out.read_text())
            self.assertEqual(2, summary["runs"])
            self.assertTrue(all(r["audit_ok"] for r in summary["per_run"]))
            self.assertTrue(all(r["dispatch_succeeded"] for r in summary["per_run"]))

    def test_regression_against_inflated_baseline_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            inflated = Path(temp) / "baseline.json"
            baseline = json.loads(BASELINE.read_text())
            baseline["metrics"]["supported_findings"]["min"] = 99
            inflated.write_text(json.dumps(baseline))
            result = self.run_bench("--runs", "1", "--baseline", str(inflated))
            self.assertEqual(1, result.returncode)
            self.assertIn("supported_findings", result.stdout)

    def test_missing_baseline_requires_explicit_update(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_bench("--runs", "1", "--baseline", str(Path(temp) / "none.json"))
            self.assertEqual(2, result.returncode)


if __name__ == "__main__":
    unittest.main()
