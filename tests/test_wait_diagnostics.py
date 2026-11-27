import os
from pathlib import Path
import subprocess
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "browser-cli"


class WaitDiagnosticsTest(unittest.TestCase):
    def run_cli(self, command, *probes, reject_eval=False, args=None, file_source=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "osascript"
            mock.write_text("""#!/bin/bash
if [ "$1" = -e ]; then
  printf '%s\\n' "${@: -2:1}" >> "$MOCK_DIR/notifications"
  exit 1
fi
if [ "$MOCK_REJECT_EVAL" = 1 ] && grep -q 'eval(' "$3"; then
  echo 'err:string eval blocked'
  exit 0
fi
count=0
if [ -f "$MOCK_DIR/count" ]; then read -r count < "$MOCK_DIR/count"; fi
probe="$MOCK_DIR/probe-$count"
if [ -f "$probe" ]; then
  result="$(cat "$probe")"
  echo $((count + 1)) > "$MOCK_DIR/count"
  printf '%s\\n' "$result" > "$MOCK_DIR/last"
else
  result="$(cat "$MOCK_DIR/last")"
fi
printf '%s\\n' "$result"
""")
            mock.chmod(0o755)
            for index, probe in enumerate(probes):
                (root / f"probe-{index}").write_text(probe)
            cli_args = [command, "--timeout", "3", *(args or [])]
            if command == "wait" and args is None:
                cli_args.append("false")
            if file_source is not None:
                source = root / "user.js"
                source.write_text(file_source)
                cli_args.append(str(source))
            result = subprocess.run([CLI, "--browser", "chrome", *cli_args], text=True,
                                    capture_output=True, timeout=8,
                                    env=dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", MOCK_DIR=directory,
                                             MOCK_REJECT_EVAL=str(int(reject_eval))))
            notice = root / "notifications"
            return result, notice.read_text() if notice.exists() else ""

    def test_info_does_not_use_string_eval(self):
        result, _ = self.run_cli("info", "str:challenge: none", reject_eval=True)
        self.assertEqual((result.returncode, result.stdout), (0, "challenge: none"))

    def test_builtin_reads_and_snapshot_do_not_use_string_eval(self):
        for command in ("text", "links", "html", "snapshot"):
            with self.subTest(command=command):
                result, _ = self.run_cli(command, "str:browser-cli-result:content", reject_eval=True)
                self.assertEqual((result.returncode, result.stdout), (0, "content\n"))

    def test_default_actions_do_not_use_string_eval(self):
        for command, args in (("click", ["#missing"]), ("fill", ["#missing", "value"]),
                              ("select", ["#missing", "value"]), ("submit", ["#missing"]),
                              ("press", ["Escape", "--selector", "#missing"]),
                              ("scroll", ["--to", "top", "--selector", "#missing"])):
            with self.subTest(command=command):
                result, _ = self.run_cli(command, "str:browser-cli-no-match:",
                                         reject_eval=True, args=args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("no element matches", result.stderr)
                self.assertNotIn("string eval blocked", result.stderr)

    def test_screenshot_geometry_does_not_use_string_eval(self):
        result, _ = self.run_cli("screenshot", "str:no-match", reject_eval=True,
                                 args=["--selector", "#missing"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("no element matches", result.stderr)
        self.assertNotIn("string eval blocked", result.stderr)

    def test_wait_challenge_does_not_use_string_eval(self):
        result, _ = self.run_cli("wait-challenge", "str:none\nexample.com", reject_eval=True)
        self.assertEqual((result.returncode, result.stdout), (0, "challenge: none\n"))

    def test_user_expressions_still_use_string_eval(self):
        for command, kwargs in (("eval", {"args": ["1 + 1"]}),
                                ("file", {"file_source": "1 + 1"}),
                                ("wait", {"args": ["true"]})):
            with self.subTest(command=command):
                result, _ = self.run_cli(command, "ok:true", reject_eval=True, **kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("string eval blocked", result.stderr)

    def test_initial_none_does_not_claim_clearance(self):
        result, notice = self.run_cli("wait-challenge", "str:none\nexample.com")
        self.assertEqual((result.returncode, result.stdout, notice), (0, "challenge: none\n", ""))
        self.assertFalse(any(line.startswith("browser-cli: ") for line in result.stderr.splitlines()))

    def test_passive_to_human_stops_even_if_notification_fails(self):
        result, notice = self.run_cli("wait-challenge", "str:cloudflare-passive\nexample.com",
                                      "str:needs-human\nexample.com", "str:none\nexample.com")
        self.assertEqual((result.returncode, result.stdout, notice), (1, "", "example.com\n"))
        self.assertIn("human verification needed", result.stderr)

    def test_failed_probe_after_passive_does_not_claim_challenge_remains(self):
        result, _ = self.run_cli("wait-challenge", "str:cloudflare-passive\nexample.com", "err:tab closed")
        self.assertEqual(result.returncode, 1)
        self.assertIn("tab closed", result.stderr)
        self.assertIn("could not determine challenge state", result.stderr)
        self.assertNotIn("still detected", result.stderr)

    def test_successful_passive_probe_replaces_previous_error(self):
        result, _ = self.run_cli("wait-challenge", "err:probe failed", "str:cloudflare-passive\nexample.com")
        self.assertEqual(result.returncode, 1)
        self.assertIn("passive challenge still detected", result.stderr)
        self.assertNotIn("probe failed", result.stderr)

    def test_successful_false_probe_replaces_previous_error(self):
        result, _ = self.run_cli("wait", "err:probe failed", "ok:false")
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out", result.stderr)
        self.assertNotIn("probe failed", result.stderr)
