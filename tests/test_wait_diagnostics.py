import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "browser-cli"
START_TIME = "Fri Nov 27 00:00:00 2026"
CHROME_INSTANCE = "1234\t" + START_TIME


class WaitDiagnosticsTest(unittest.TestCase):
    def run_cli(self, command, *probes, reject_eval=False, args=None, file_source=None, page_fixture=None,
                browser="chrome", mcp=False, running=True, pids="1234", osa_error=None, osa_calls=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pgrep = root / "pgrep"
            pgrep.write_text('''#!/bin/bash
[ "$MOCK_BROWSER_RUNNING" = 1 ] || exit 1
[ "$1" = -qx ] || printf '%s\\n' "$MOCK_BROWSER_PIDS"
''')
            pgrep.chmod(0o755)
            ps = root / "ps"
            ps.write_text('''#!/bin/bash
[ "$*" = '-p 1234 -o lstart=' ] || exit 1
printf '%s\\n' "$MOCK_BROWSER_START"
''')
            ps.chmod(0o755)
            mock = root / "osascript"
            mock.write_text("""#!/bin/bash
if [ "$1" = -e ]; then
  printf '%s\\n' "${@: -2:1}" >> "$MOCK_DIR/notifications"
  exit 1
fi
cat "$1" >> "$MOCK_DIR/osa-scripts"
printf '%s\\n' "$2" >> "$MOCK_DIR/osa-targets"
if [ -f "$MOCK_DIR/osa-error" ]; then
  cat "$MOCK_DIR/osa-error" >&2
  exit 1
fi
if [ "$MOCK_REJECT_EVAL" = 1 ] && grep -q 'eval(' "$3"; then
  echo 'err:string eval blocked'
  exit 0
fi
if [ -f "$MOCK_DIR/page.js" ]; then
  exec node -e 'const fs = require("node:fs"), vm = require("node:vm");
    vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
    console.log(vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8")));' "$MOCK_DIR/page.js" "$3"
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
            if osa_error is not None:
                (root / "osa-error").write_text(osa_error)
            for index, probe in enumerate(probes):
                (root / f"probe-{index}").write_text(probe)
            cli_args = [command, "--timeout", "3", *(args or [])]
            if command == "wait" and args is None:
                cli_args.append("false")
            if file_source is not None:
                source = root / "user.js"
                source.write_text(file_source)
                cli_args.append(str(source))
            if page_fixture is not None:
                (root / "page.js").write_text(page_fixture)
            result = subprocess.run([CLI, "--browser", browser, *cli_args], text=True,
                                    capture_output=True, timeout=8,
                                    env=dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", MOCK_DIR=directory,
                                             BROWSER_CLI_MCP=str(int(mcp)), MOCK_BROWSER_RUNNING=str(int(running)),
                                             MOCK_BROWSER_PIDS=pids, MOCK_BROWSER_START=START_TIME,
                                             MOCK_REJECT_EVAL=str(int(reject_eval))))
            if osa_calls is not None and (root / "osa-scripts").exists():
                osa_calls.append(((root / "osa-scripts").read_text(), (root / "osa-targets").read_text()))
            notice = root / "notifications"
            return result, notice.read_text() if notice.exists() else ""

    def test_info_does_not_use_string_eval(self):
        result, _ = self.run_cli("info", "str:challenge: none", reject_eval=True)
        self.assertEqual((result.returncode, result.stdout), (0, "challenge: none"))

    def test_guarded_read_returns_structured_json_without_eval(self):
        result, _ = self.run_cli("read-page", 'ok:{"status":"verification_required"}', reject_eval=True,
                                 args=["--tab", "id:42", "--include-links", "--", "https://example.com/"])
        self.assertEqual((result.returncode, result.stdout), (0, '{"status":"verification_required"}\n'))

    def test_guarded_read_checks_url_and_verification_before_body(self):
        url = "https://example.com/"
        for title, current, expected in (("Attention required", url, "verification_required"),
                                         ("Example", url + "other", "target_changed"),
                                         ("Just a moment...", url, "passive_challenge")):
            with self.subTest(expected=expected):
                fixture = (f"global.location = new URL({current!r}); global.document = "
                           f"{{title:{title!r}, querySelectorAll:()=>[], get body(){{throw Error('unexpected read')}}}};")
                result, _ = self.run_cli("read-page", args=["--tab", "id:42", "--selector", "[", "--", url],
                                         page_fixture=fixture)
                self.assertEqual((result.returncode, result.stdout), (0, f'{{"status":"{expected}"}}\n'))

    def test_guarded_read_returns_visible_text(self):
        url = "https://example.com/"
        fixture = f"""global.location = new URL({url!r});
global.document = {{title:'Example', readyState:'complete', querySelectorAll:()=>[], querySelector:()=>null,
  body:{{innerText:'Example Domain', querySelectorAll:()=>[]}}}};
"""
        result, _ = self.run_cli("read-page", args=["--tab", "id:42", "--", url], page_fixture=fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"text":"Example Domain"', result.stdout)

    def test_safari_guarded_read_resolves_unique_case_sensitive_url_substring(self):
        url = "https://example.com/Path"
        calls = []
        result, _ = self.run_cli("read-page", 'ok:{"status":"tab_ambiguous"}', browser="safari",
                                 args=["--tab", "/Path", "--", url], osa_calls=calls)
        self.assertEqual((result.returncode, result.stdout), (0, '{"status":"tab_ambiguous"}\n'))
        script, spec = calls[0]
        self.assertEqual(spec, "/Path\n")
        self.assertIn("considering case", script)
        self.assertIn("u contains spec", script)
        self.assertIn("if matched is not {} then return {-2, 0}", script)
        self.assertIn("if matched is {} then return {-1, 0}", script)
        self.assertLess(script.index("set r to resolve(item 1 of argv)"),
                        script.index('tell application "Safari" to return do JavaScript code in tab (item 2 of r)'))
        fixture = f"global.location = new URL({(url + '/other')!r});"
        result, _ = self.run_cli("read-page", browser="safari", args=["--tab", "/Path", "--", url],
                                 page_fixture=fixture)
        self.assertEqual((result.returncode, result.stdout), (0, '{"status":"target_changed"}\n'))

    def test_guarded_read_scopes_text_and_links_to_first_match(self):
        url = "https://example.com/"
        fixture = f"""global.location = new URL({url!r});
global.getComputedStyle = () => ({{visibility:'visible'}});
const visible = {{closest:()=>null, getBoundingClientRect:()=>({{width:1,height:1}})}};
const root = {{...visible, innerText:'Scoped link', href:'https://example.com/inside',
  matches:s=>s === 'a[href]', querySelectorAll:()=>[]}};
global.document = {{title:'Example', readyState:'complete', querySelectorAll:()=>[],
  querySelector:s=>s === '[data-target="scope"]' ? root : null,
  body:{{get innerText(){{throw Error('unexpected full text read')}},
         querySelectorAll:s=>{{if(s === 'a[href]') throw Error('unexpected full link read'); return [];}}}}}};
"""
        result, _ = self.run_cli("read-page", args=["--tab", "id:42", "--include-links", "--selector",
                                                   '[data-target="scope"]', "--", url], page_fixture=fixture)
        self.assertEqual(result.returncode, 0, result.stderr)
        page = json.loads(result.stdout)
        self.assertEqual((page["status"], page["text"], page["links"]),
                         ("ok", "Scoped link", [{"text": "Scoped link", "url": url + "inside"}]))

    def test_guarded_read_selector_errors_follow_full_page_login_check(self):
        url = "https://example.com/"
        for path, selector, status in (("", "#missing", "no_match"), ("", "[", "invalid_selector"),
                                       ("sign-in", "[", "login_required")):
            with self.subTest(status=status):
                current = url + path
                fixture = f"""global.location = new URL({current!r});
global.document = {{title:'Example', readyState:'complete', querySelectorAll:()=>[],
  querySelector:s=>{{if (s === '[') throw SyntaxError('invalid syntax'); return null;}},
  body:{{innerText:'Whole page', querySelectorAll:()=>[]}}}};
"""
                result, _ = self.run_cli("read-page", args=["--tab", "id:42", "--selector", selector,
                                                           "--", current], page_fixture=fixture)
                self.assertEqual((result.returncode, result.stdout), (0, f'{{"status":"{status}"}}\n'))

    def test_mcp_status_skips_stopped_browser_and_reports_unknown(self):
        for browser in ("safari", "chrome"):
            with self.subTest(browser=browser):
                calls = []
                result, _ = self.run_cli("status", mcp=True, running=False, browser=browser, osa_calls=calls)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unknown\n", ""))
                self.assertEqual(calls, [])
        calls = []
        result, _ = self.run_cli("status", "unknown", mcp=True, osa_calls=calls)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unknown\n", ""))
        self.assertIn('if application "Google Chrome" is not running then return "unknown"', calls[0][0])
        result, _ = self.run_cli("status", "unknown", running=False)
        self.assertEqual((result.returncode, result.stdout), (1, "unknown\n"))
        self.assertIn("no tab is open", result.stderr)

    def test_status_reports_permission_off(self):
        diagnostic = 'status.applescript: execution error: Allow JavaScript from Apple Events (-2700)\n'
        for mcp, code in ((False, 1), (True, 25)):
            with self.subTest(mcp=mcp):
                result, _ = self.run_cli("status", mcp=mcp, osa_error=diagnostic)
                self.assertEqual((result.returncode, result.stdout), (code, "off\n"))
                self.assertIn("JavaScript from Apple Events is off", result.stderr)

    def test_mcp_apple_event_missing_object_preserves_original_diagnostic(self):
        diagnostic = "eval.applescript:12:23: execution error: Can't get tab id 42. (-1728)\n"
        args = ["--tab", "id:42", "--", "https://example.com/"]
        result, _ = self.run_cli("read-page", mcp=True, args=args, osa_error=diagnostic)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (26, "", diagnostic))
        result, _ = self.run_cli("read-page", args=args, osa_error=diagnostic)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "browser-cli: AppleScript error -1728: Can't get tab id 42.\n")

    def test_close_created_requires_mcp_chrome_and_numeric_tab_id(self):
        calls = []
        for browser, mcp, tab in (("chrome", False, "id:42"), ("safari", True, "1:2"),
                                  ("chrome", True, "1:2"), ("chrome", True, "id:xx")):
            with self.subTest(browser=browser, mcp=mcp, tab=tab):
                result, _ = self.run_cli("close-created", mcp=mcp, browser=browser,
                                         args=["--tab", tab, "--", CHROME_INSTANCE], osa_calls=calls)
                self.assertEqual(result.returncode, 2)
        result, _ = self.run_cli("close-created", mcp=True, args=["--tab", "id:42"], osa_calls=calls)
        self.assertEqual(result.returncode, 2)
        for running, instance in ((False, CHROME_INSTANCE), (True, CHROME_INSTANCE.replace("00:00", "00:01"))):
            with self.subTest(running=running, instance=instance):
                result, _ = self.run_cli("close-created", mcp=True, running=running,
                                         args=["--tab", "id:42", "--", instance], osa_calls=calls)
                self.assertEqual((result.returncode, result.stdout), (0, "not_found\n"))
        self.assertEqual(calls, [])

    def test_close_created_targets_tab_and_window_by_id(self):
        calls = []
        for status in ("closed", "not_found"):
            with self.subTest(status=status):
                result, _ = self.run_cli("close-created", status, mcp=True,
                                         args=["--tab", "id:42", "--", CHROME_INSTANCE], osa_calls=calls)
                self.assertEqual((result.returncode, result.stdout), (0, status + "\n"))
        script, tab_id = calls[0]
        self.assertEqual(tab_id, "42\n")
        self.assertIn('if application "Google Chrome" is not running', script)
        self.assertIn("if tabIDs contains wantedID then", script)
        self.assertLess(script.index("if my chromeInstance() is not expectedInstance"),
                        script.index("close tab id wantedID of window id wi"))
        self.assertEqual([line.strip() for line in script.splitlines() if line.strip().startswith("close ")],
                         ["close tab id wantedID of window id wi"])

    def test_chrome_instance_is_internal_and_requires_unambiguous_process(self):
        for mcp, browser in ((False, "chrome"), (True, "safari")):
            result, _ = self.run_cli("chrome-instance", mcp=mcp, browser=browser)
            self.assertEqual(result.returncode, 2)
        calls = []
        result, _ = self.run_cli("chrome-instance", mcp=True, osa_calls=calls)
        self.assertEqual((result.returncode, result.stdout), (0, CHROME_INSTANCE + "\n"))
        for running, pids in ((False, "1234"), (True, "1234\n5678")):
            result, _ = self.run_cli("chrome-instance", mcp=True, running=running, pids=pids, osa_calls=calls)
            self.assertEqual((result.returncode, result.stdout), (0, "unknown\n"))
        self.assertEqual(calls, [])

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
