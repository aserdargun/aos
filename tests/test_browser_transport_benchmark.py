from pathlib import Path
import runpy
import unittest


BENCHMARK = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/compare_browser_transports.py"))


class BrowserTransportBenchmarkTests(unittest.TestCase):
    def test_model_phase_excludes_unbounded_worker_fields(self):
        self.assertEqual(BENCHMARK["model_phase"]({"load_ms": 220.0, "inference_ms": 61.0,
                                                   "reused": False, "raw_prediction": "private"}),
                         {"load_ms": 220.0, "inference_ms": 61.0, "reused": False})

    def test_summary_reports_sample_count_range_and_median_without_claiming_p95(self):
        self.assertEqual(BENCHMARK["summary"]([20.0, 10.0]),
                         {"count": 2, "min_ms": 10.0, "median_ms": 15.0, "max_ms": 20.0})

    def test_verified_form_requires_real_model_calls_only_for_decider(self):
        verified = [{"method": "independent_dom_equals", "result": "passed"}] * 2
        calls = [{"role": "system1", "status": "ok", "latency_ms": 12.0}] * 2
        fixture = {"status": "succeeded", "verified": True, "real_model": False}
        real = {**fixture, "real_model": True}
        check = BENCHMARK["verified_form"]
        self.assertTrue(check(fixture, verified, [], "fixture"))
        self.assertTrue(check(real, verified, calls, "decider"))
        self.assertFalse(check(real, verified, [], "decider"))
        self.assertFalse(check(fixture, verified, calls, "decider"))
        self.assertFalse(check(real, verified, calls[:1], "decider"))
        self.assertFalse(check(real, verified[:1], calls, "decider"))
        self.assertFalse(check(real, verified, [{**calls[0], "status": "error"}, calls[1]], "decider"))


if __name__ == "__main__":
    unittest.main()
