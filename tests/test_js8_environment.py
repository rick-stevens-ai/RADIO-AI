import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hamradio import js8


def test_is_running_requires_exact_process_name():
    with patch("hamradio.js8.subprocess.run") as run:
        run.return_value.returncode = 0
        assert js8.is_running() is True
        assert run.call_args.args[0] == ["pgrep", "-x", "js8call"]


def test_ensure_running_starts_user_service_without_tmux():
    with patch("hamradio.js8.is_running", side_effect=[False, True]), \
         patch("hamradio.js8.api_up", return_value=True), \
         patch("hamradio.js8.subprocess.run") as run, \
         patch("hamradio.js8.time.sleep"):
        result = js8.ensure_running(wait_s=1)
        assert result["api"] is True
        run.assert_called_once_with(
            ["systemctl", "--user", "start", "js8call.service"],
            check=True, capture_output=True, text=True,
        )
