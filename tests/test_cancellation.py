import time
import unittest
from threading import Event, Thread
from unittest.mock import Mock, patch

from bosshunter.cancellation import OperationCancelled, run_cancellable
from bosshunter.throttle import PageThrottle


class CancellationTests(unittest.TestCase):
    def test_page_throttle_returns_immediately_when_stop_is_requested(self):
        stop_event = Event()
        stop_event.set()

        with patch("bosshunter.throttle.time.sleep") as sleep:
            stopped = PageThrottle(delay_min=30, delay_max=30).wait(stop_event)

        self.assertTrue(stopped)
        sleep.assert_not_called()

    def test_blocking_operation_returns_control_promptly_after_stop(self):
        stop_event = Event()
        operation_started = Event()
        release_operation = Event()

        def blocking_operation():
            operation_started.set()
            release_operation.wait(timeout=2)
            return "late result"

        def request_stop():
            operation_started.wait(timeout=1)
            stop_event.set()

        Thread(target=request_stop, daemon=True).start()
        started_at = time.monotonic()
        try:
            with self.assertRaises(OperationCancelled):
                run_cancellable(
                    blocking_operation,
                    {"_workbench_stop_event": stop_event},
                    poll_seconds=0.02,
                )
        finally:
            release_operation.set()

        self.assertLess(time.monotonic() - started_at, 0.5)

    def test_already_stopped_operation_is_never_started(self):
        stop_event = Event()
        stop_event.set()
        operation = Mock(return_value="unexpected")

        with self.assertRaises(OperationCancelled):
            run_cancellable(operation, {"_workbench_stop_event": stop_event})

        operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
