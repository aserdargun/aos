import threading
import unittest

from aos.contracts import AOSFault
from aos.remote_form_operator import SingleUseRequestPermit


REQUEST_SHA256 = 'a' * 64
OTHER_REQUEST_SHA256 = 'b' * 64


class SingleUseRequestPermitTests(unittest.TestCase):
    def setUp(self):
        self.wall_time = [100.0]
        self.monotonic_time = [500.0]
        self.permit = SingleUseRequestPermit(
            wall_clock=lambda: self.wall_time[0],
            monotonic_clock=lambda: self.monotonic_time[0])

    def test_exact_request_is_authorized_only_once(self):
        self.permit.arm(REQUEST_SHA256, 110.0)
        self.assertTrue(self.permit.consume(REQUEST_SHA256))
        self.assertFalse(self.permit.consume(REQUEST_SHA256))
        self.permit.arm(OTHER_REQUEST_SHA256, 110.0)
        self.assertFalse(self.permit.consume(REQUEST_SHA256))
        self.assertFalse(self.permit.consume(OTHER_REQUEST_SHA256))

    def test_monotonic_expiry_rejects_request_even_after_wall_clock_rollback(self):
        self.permit.arm(REQUEST_SHA256, 105.0)
        self.wall_time[0] = 10.0
        self.monotonic_time[0] = 505.0
        self.assertFalse(self.permit.consume(REQUEST_SHA256))
        self.assertFalse(self.permit.consume(REQUEST_SHA256))

    def test_expired_or_unbounded_deadline_never_arms(self):
        for deadline in (99.0, 100.0, 190.1, True, float('nan'), float('inf')):
            with self.subTest(deadline=deadline), self.assertRaises(AOSFault):
                self.permit.arm(REQUEST_SHA256, deadline)
            self.assertFalse(self.permit.consume(REQUEST_SHA256))

    def test_malformed_request_hash_and_duplicate_arm_are_rejected(self):
        for request_sha256 in ('A' * 64, 'a' * 63, 'g' * 64, None):
            with self.subTest(request_sha256=request_sha256), self.assertRaises(AOSFault):
                self.permit.arm(request_sha256, 110.0)
        self.permit.arm(REQUEST_SHA256, 110.0)
        with self.assertRaises(AOSFault):
            self.permit.arm(OTHER_REQUEST_SHA256, 110.0)
        self.assertTrue(self.permit.consume(REQUEST_SHA256))

    def test_concurrent_consumers_cannot_reuse_one_approval(self):
        self.permit.arm(REQUEST_SHA256, 110.0)
        barrier = threading.Barrier(3)
        results = []

        def consume():
            barrier.wait()
            results.append(self.permit.consume(REQUEST_SHA256))

        first = threading.Thread(target=consume)
        second = threading.Thread(target=consume)
        first.start()
        second.start()
        barrier.wait()
        first.join()
        second.join()
        self.assertEqual(sorted(results), [False, True])


if __name__ == '__main__':
    unittest.main()
