"""Supervisor: child restarts with backoff, low-disk kill switch, start/stop guards."""
import pathlib
import tempfile
import time
import unittest
from unittest import mock

import supervisor


class ChildTests(unittest.TestCase):
    def test_restart_backoff_doubles_on_quick_crashes(self):
        c = supervisor.Child("bot", [])
        proc = mock.Mock(pid=1); proc.poll.return_value = 1; proc.returncode = 1
        c.proc, c.started_at = proc, time.time()
        with mock.patch.object(supervisor, "notify") as n, mock.patch.object(supervisor.subprocess, "Popen") as popen:
            c.ensure()
            self.assertEqual(c.backoff, 10.0)
            popen.assert_not_called()                     # waits out the backoff
            n.assert_called_once()

    def test_long_run_resets_backoff(self):
        c = supervisor.Child("bot", [])
        proc = mock.Mock(pid=1); proc.poll.return_value = 0; proc.returncode = 0
        c.proc, c.started_at, c.backoff = proc, time.time() - 3600, 160
        with mock.patch.object(supervisor, "notify"), mock.patch.object(supervisor.subprocess, "Popen"):
            c.ensure()
        self.assertEqual(c.backoff, 5.0)


class GuardTests(unittest.TestCase):
    def test_start_refuses_when_a_bot_already_runs(self):
        with mock.patch.object(supervisor, "read_pid", return_value=None), \
                mock.patch.object(supervisor, "other_bots", return_value=[4242]), \
                mock.patch.object(supervisor.subprocess, "Popen") as popen:
            self.assertEqual(supervisor.start(["--mode", "auto"]), 1)
        popen.assert_not_called()

    def test_low_disk_engages_kill_switch(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            ks = d / "KILL_SWITCH"
            calls = {"n": 0}

            def fake_sleep(_):
                calls["n"] += 1
                raise KeyboardInterrupt
            with mock.patch.multiple(supervisor, LOGS=d, PID_FILE=d / "pid", STATE_FILE=d / "state",
                                     KILL_SWITCH=ks, BOT_STATUS=d / "st.json"), \
                    mock.patch.object(supervisor, "free_gb", return_value=0.5), \
                    mock.patch.object(supervisor, "notify"), \
                    mock.patch.object(supervisor.Child, "ensure"), \
                    mock.patch.object(supervisor.time, "sleep", fake_sleep):
                with self.assertRaises(KeyboardInterrupt):
                    supervisor.run([])
            self.assertTrue(ks.exists())
            self.assertIn("low disk", ks.read_text())
            self.assertFalse((d / "pid").exists())        # cleaned up on exit


if __name__ == "__main__":
    unittest.main()
