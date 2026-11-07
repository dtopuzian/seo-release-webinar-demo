"""Demo CI trust boundary and notification regressions; no network calls."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Standard-library-only tests, including isolated Python (-I).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import client, notify, run as runner


ENV = {"GITHUB_RUN_ID": "12345", "DEMO_SCENE": "checked", "DEMO_VARIANT": "bad",
       "DEMO_RUNTIME_SHA": "a" * 40, "GITHUB_SHA": "d" * 40,
       "GITHUB_REPOSITORY": "dtopuzian/seo-release-webinar-demo",
       "GITHUB_SERVER_URL": "https://github.com"}
EXPECTED = client.expected_binding(ENV)


def payload(outcome="blocked", target="checked", variant="bad"):
    candidate = {"run": "12345", "target": target, "variant": variant,
                 "source_sha": "a" * 40, "artifact_sha256": "b" * 64,
                 "version": {"good": "v1", "bad": "v2"}[variant]}
    has_gate = outcome in {"checked", "blocked"} or (outcome == "published" and target == "checked")
    blocked = outcome == "blocked"
    return {**candidate, "digest": candidate["artifact_sha256"], "outcome": outcome,
            "reason_codes": ["unexpected_noindex"] if blocked else [],
            "checks": [{"path": "/", "code": "unexpected_noindex" if blocked else "pass", "passed": not blocked}] if has_gate else [],
            "report_url": f"{client.ORIGIN}/_reports/12345/{target}/report.html" if has_gate else None,
            "published": candidate.copy() if outcome == "published" else None}


def completed(value, code=0):
    return subprocess.CompletedProcess(["ssh"], code, json.dumps(value).encode(), b"PRIVATE_SSH_ERROR")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.result_path = self.root / "demo-result.json"
        self.key, self.known = self.root / "key", self.root / "known"
        self.key.write_text("TEST KEY PLACEHOLDER", encoding="utf-8")
        self.known.write_text("TEST HOST PLACEHOLDER", encoding="utf-8")
        self.environment = {**ENV, "DEMO_SSH_KEY_FILE": str(self.key), "DEMO_SSH_KNOWN_HOSTS_FILE": str(self.known)}
        self.enterContext(patch.dict(os.environ, self.environment, clear=True))
        self.enterContext(patch.object(client, "RESULT_FILE", self.result_path))

    def invoke(self, action, **mock_arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(client.subprocess, "run", **mock_arguments) as ssh, redirect_stdout(stdout), redirect_stderr(stderr):
            code = client.main([action])
        return code, ssh, stdout.getvalue() + stderr.getvalue(), json.loads(self.result_path.read_text(encoding="utf-8"))

    def test_success_and_blocked_keep_remote_exit_semantics(self):
        for outcome, code in (("checked", 0), ("blocked", 10)):
            with self.subTest(outcome=outcome):
                actual, ssh, text, result = self.invoke("check", side_effect=[completed(payload("prepared")), completed(payload(outcome), code)])
                self.assertEqual(actual, code)
                self.assertTrue(result["verified"])
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual([call.args[0][-1] for call in ssh.call_args_list],
                                 ["demo-v1 status checked bad 12345", "demo-v1 check checked bad 12345"])
                self.assertNotIn("PRIVATE_SSH_ERROR", text)

    def test_binding_mismatches_cannot_pass(self):
        mutations = {"source_sha": "c" * 40, "run": "999", "target": "unsafe", "variant": "good",
                     "version": "v1", "digest": "f" * 64}
        for key, value in mutations.items():
            with self.subTest(field=key):
                response = payload("prepared")
                response[key] = value
                code, _, text, result = self.invoke("prepare", return_value=completed(response))
                self.assertEqual(code, 2)
                self.assertFalse(result["verified"])
                self.assertEqual(result["outcome"], "unconfirmed")
                self.assertNotIn(value, text)

    def test_wrong_source_is_refused_before_release_command(self):
        response = payload("prepared")
        response["source_sha"] = "c" * 40
        code, ssh, _, result = self.invoke("release", return_value=completed(response))
        self.assertEqual(code, 2)
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual(ssh.call_args.args[0][-1], "demo-v1 status checked bad 12345")
        self.assertFalse(result["verified"])

    def test_ssh_error_timeout_and_invalid_json_revoke_stale_success(self):
        cases = [dict(return_value=completed(payload("published"), 255)),
                 dict(side_effect=OSError("PRIVATE_OS_ERROR")),
                 dict(side_effect=subprocess.TimeoutExpired("ssh PRIVATE_COMMAND", 240, stderr=b"PRIVATE_TIMEOUT")),
                 dict(return_value=subprocess.CompletedProcess(["ssh"], 0, b"PRIVATE_NON_JSON", b"PRIVATE_STDERR"))]
        for arguments in cases:
            with self.subTest(arguments=tuple(arguments)):
                self.result_path.write_text(json.dumps({**payload("published"), "verified": True}), encoding="utf-8")
                code, _, text, result = self.invoke("status", **arguments)
                self.assertEqual(code, 2)
                self.assertFalse(result["verified"])
                self.assertIsNone(result["published"])
                self.assertNotIn("PRIVATE_", text)

    def test_fake_pass_and_exit_outcome_inconsistency_are_refused(self):
        cases = [(payload("checked"), 10), (payload("blocked"), 0), (payload("checked"), 255),
                 ({**payload("checked"), "outcome": "PASS"}, 0),
                 ({**payload("checked"), "checks": []}, 0),
                 ({**payload("checked"), "checks": [{"path": "/", "code": "unexpected_noindex", "passed": True}]}, 0)]
        for response, code in cases:
            with self.subTest(outcome=response["outcome"], exit=code):
                with self.assertRaises(ValueError):
                    client.validated_result(response, EXPECTED, "check", code)

    def test_published_requires_current_candidate_receipt(self):
        response = payload("published")
        self.assertTrue(client.validated_result(response, EXPECTED, "release", 0)["verified"])
        for published in (None, {**response["published"], "run": "999"}, {**response["published"], "target": "unsafe"}):
            with self.subTest(published=published):
                with self.assertRaises(ValueError):
                    client.validated_result({**response, "published": published}, EXPECTED, "release", 0)

    def test_unknown_payload_fields_do_not_reach_output_or_artifact(self):
        response = payload("published")
        response["unknown"] = "PRIVATE_TOP"
        response["checks"][0]["unknown"] = "PRIVATE_CHECK"
        response["published"]["unknown"] = "PRIVATE_PUBLISHED"
        code, _, text, result = self.invoke("status", return_value=completed(response))
        self.assertEqual(code, 0)
        self.assertNotIn("PRIVATE_", text)
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_missing_key_or_source_sha_revoke_success_without_ssh(self):
        for missing in ("DEMO_RUNTIME_SHA", "DEMO_SSH_KEY_FILE"):
            with self.subTest(missing=missing), patch.dict(os.environ, {missing: ""}):
                code, ssh, _, result = self.invoke("status")
                self.assertEqual(code, 2)
                ssh.assert_not_called()
                self.assertFalse(result["verified"])

    def test_engine_binding_ignores_github_commit_but_rejects_foreign_repository(self):
        self.assertEqual(EXPECTED["source_sha"], ENV["DEMO_RUNTIME_SHA"])
        self.assertNotEqual(EXPECTED["source_sha"], ENV["GITHUB_SHA"])
        for change in ({"GITHUB_REPOSITORY": "other/seo-release-webinar-demo"},
                       {"GITHUB_SERVER_URL": "https://evil.example"}):
            with patch.dict(os.environ, change):
                code, ssh, _, result = self.invoke("prepare")
                self.assertEqual(code, 2)
                ssh.assert_not_called()
                self.assertFalse(result["verified"])


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, ENV, clear=True))

    def verified(self, outcome="blocked"):
        return client.validated_result(payload(outcome), EXPECTED, "status", 0)

    def test_blocked_reports_actual_noindex_and_retained_v1(self):
        value = self.verified()
        value["published"] = {**payload("prepared"), "variant": "good", "version": "v1", "artifact_sha256": "c" * 64}
        text = notify.message(value, "checked")
        self.assertIn("SEO-проверка остановила выпуск", text)
        self.assertIn("meta robots noindex", text)
        self.assertIn("На витрине остаётся версия v1", text)
        self.assertIn(value["report_url"], text)
        self.assertNotIn("✅", text)

    def test_already_published_artifact_never_claims_it_was_not_published(self):
        value = self.verified()
        value["published"] = {key: value[key] for key in client.BINDING_KEYS}
        text = notify.message(value, "checked")
        self.assertIn("уже был опубликован ранее", text)
        self.assertIn("Автоматический откат не выполнялся", text)
        self.assertNotIn("Кандидат не опубликован", text)

    def test_missing_state_is_not_an_invented_rollback_or_v1(self):
        text = notify.message(self.verified(), "checked")
        self.assertIn("текущее состояние витрины неизвестно", text)
        self.assertNotIn("остаётся версия", text)
        self.assertNotIn("Новая версия не опубликована", text)

    def test_none_and_invalid_urls_are_ignored_without_error(self):
        invalid = [None, [], {}, 123, "https://evil.example/report.html",
                   client.ORIGIN + "/_reports/12345/checked/../unsafe/report.html",
                   client.ORIGIN + "/_reports/999/checked/report.html",
                   client.ORIGIN + "/_reports/12345/checked/report.html?token=PRIVATE"]
        for url in invalid:
            with self.subTest(url=url), patch.dict(os.environ, {"GITHUB_SERVER_URL": "https://evil.example/"}):
                text = notify.message({**self.verified(), "report_url": url}, "checked")
                self.assertNotIn("Отчёт:", text)
                self.assertNotIn("GitHub:", text)
                self.assertNotIn("PRIVATE", text)

    def test_unverified_success_has_no_success_heading_or_report(self):
        value = {**payload("published"), "verified": False}
        text = notify.message(value, "checked")
        self.assertIn("выпуск не подтверждён", text)
        self.assertNotIn("✅", text)
        self.assertNotIn("Отчёт:", text)

    def test_github_link_binds_exact_repository_and_current_run(self):
        value = self.verified()
        self.assertIn("https://github.com/dtopuzian/seo-release-webinar-demo/actions/runs/12345",
                      notify.message(value, "checked"))
        for change in ({"GITHUB_REPOSITORY": "other/repository"}, {"GITHUB_RUN_ID": "999"}):
            with patch.dict(os.environ, change):
                self.assertNotIn("GitHub:", notify.message(value, "checked"))

    def test_stale_or_missing_receipt_sends_safe_unconfirmed_notice_only(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        opener = MagicMock()
        opener.open.return_value = response
        stale = {**self.verified("published"), "source_sha": "c" * 40}
        with patch.object(notify.Path, "read_bytes", return_value=json.dumps(stale).encode()), \
                patch.dict(os.environ, {"DEMO_TELEGRAM_TOKEN": "123:TEST_ONLY_TOKEN", "DEMO_TELEGRAM_CHAT": "456"}), \
                patch.object(notify, "build_opener", return_value=opener), redirect_stdout(io.StringIO()):
            self.assertEqual(notify.main(), 0)
        request = opener.open.call_args.args[0]
        text = json.loads(request.data)["text"]
        self.assertIn("выпуск не подтверждён", text)
        self.assertNotIn("✅", text)
        self.assertNotIn("TEST_ONLY_TOKEN", text)
        with patch.object(notify.Path, "read_bytes", side_effect=FileNotFoundError("PRIVATE_PATH")):
            self.assertFalse(notify.load_result()["verified"])


class SecretWrapperTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(patch.object(client, "RESULT_FILE", self.root / "demo-result.json"))
        self.enterContext(patch.dict(os.environ, {**ENV, "RUNNER_TEMP": folder.name,
            "DEMO_SSH_KEY": "PRIVATE_KEY_SENTINEL", "DEMO_SSH_KNOWN_HOSTS": "HOST_PIN_SENTINEL",
            "DEMO_TELEGRAM_TOKEN": "123:PRIVATE_TOKEN", "DEMO_TELEGRAM_CHAT": "456"}, clear=True))

    def invoke(self, action, effect):
        output = io.StringIO()
        with patch.object(runner, "execute", side_effect=effect) as child, \
                redirect_stdout(output), redirect_stderr(output):
            result = runner.main([action, "checked"])
        self.assertNotIn("PRIVATE_", output.getvalue())
        return result, child

    def test_temporary_key_permissions_secrets_absent_from_child_environment_and_cleanup(self):
        paths = []
        def execute(script, arguments, environment):
            self.assertEqual((script, arguments), ("client.py", ["prepare"]))
            self.assertTrue(runner.SECRETS.isdisjoint(environment))
            for variable in ("DEMO_SSH_KEY_FILE", "DEMO_SSH_KNOWN_HOSTS_FILE"):
                path = Path(environment[variable])
                self.assertTrue(path.is_file())
                if os.name == "posix":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                paths.append(path)
            return 0
        self.assertEqual(self.invoke("prepare", execute)[0], 0)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_credential_failure_revokes_stale_receipt_and_still_notifies_unconfirmed(self):
        client.save_result({**payload("published"), "verified": True})
        def execute(script, arguments, environment):
            self.assertEqual(script, "notify.py")
            self.assertNotIn("DEMO_SSH_KEY", environment)
            self.assertNotIn("DEMO_SSH_KEY_FILE", environment)
            receipt = json.loads(client.RESULT_FILE.read_text())
            self.assertFalse(receipt["verified"])
            self.assertEqual(receipt["outcome"], "unconfirmed")
            return 0
        with patch.dict(os.environ, {"DEMO_SSH_KEY": ""}):
            code, child = self.invoke("notify", execute)
        self.assertEqual(code, 2)
        self.assertEqual(child.call_count, 1)

    def test_failed_status_does_not_skip_notification_and_blocked_exit_stays_failed(self):
        for status, notified in ((2, 0), (0, 1), (0, 0)):
            with self.subTest(status=status, notified=notified):
                code, child = self.invoke("notify", [status, notified])
                self.assertEqual([call.args[0] for call in child.call_args_list], ["client.py", "notify.py"])
                self.assertEqual(code == 0, status == 0 and notified == 0)
        self.assertEqual(self.invoke("check", [10])[0], 10)

    def test_execution_exception_cleans_up_key_and_does_not_print_raw_error(self):
        self.assertEqual(self.invoke("release", OSError("PRIVATE_ERROR"))[0], 2)
        self.assertEqual(list(self.root.glob("webinar-ssh-*")), [])

    def test_unwritable_receipt_never_reuses_stale_success_or_leaks_error(self):
        with patch.object(client, "save_result", side_effect=OSError("PRIVATE_PATH")):
            code, child = self.invoke("notify", [])
        self.assertEqual(code, 2)
        child.assert_not_called()


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1] / ".github/workflows"
        cls.workflows = {path.name: path.read_text(encoding="utf-8") for path in root.glob("*.yml")}

    def block(self, text, name):
        match = re.search(r"^  " + re.escape(name) + r":\n((?:[ ]{4}.*\n|\n)*)", text, re.MULTILINE)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_manual_workflows_share_a_full_queue_and_only_pinned_readonly_checkout(self):
        self.assertEqual(len(self.workflows), 3)
        for text in self.workflows.values():
            self.assertIn("on:\n  workflow_dispatch:\n", text)
            self.assertIn("permissions:\n  contents: read\n", text)
            self.assertIn("concurrency:\n  group: webinar-demo\n  queue: max\n  cancel-in-progress: false\n", text)
            self.assertIn("DEMO_RUNTIME_SHA: '2559b7978fd5bf49c4b9b18b18840d30992eb675'", text)
            actions = re.findall(r"uses: (\S+)", text)
            self.assertTrue(actions)
            self.assertEqual(set(actions), {"actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"})
            self.assertEqual(text.count("persist-credentials: false"), len(actions))
            self.assertNotIn("continue-on-error", text)
            self.assertNotIn("upload-artifact", text)

    def test_failed_check_blocks_publish_and_always_reaches_notification(self):
        for name in ("2-with-seo.yml", "3-reset.yml"):
            text = self.workflows[name]
            final = "status" if name == "3-reset.yml" else "notify"
            check, publish, notification = (self.block(text, name) for name in ("check", "publish", final))
            self.assertIn("needs: [prepare]", check)
            self.assertIn("needs: [prepare, check]", publish)
            self.assertNotIn("always()", publish)
            self.assertNotIn("||", check)
            self.assertIn("needs: [prepare, check, publish]", notification)
            self.assertIn("always()", notification)
        self.assertNotRegex(self.workflows["1-without-seo.yml"], r"(?m)^  check:")

    def test_real_reset_shell_reads_both_scenes_even_when_first_fails(self):
        shell = shutil.which("sh")
        if not shell:
            self.skipTest("POSIX shell unavailable")
        self.assertNotIn("DEMO_TELEGRAM", self.workflows["3-reset.yml"])
        block = self.block(self.workflows["3-reset.yml"], "status")
        commands = block.split("        run: |\n", 1)[1]
        commands = "\n".join(line[10:] for line in commands.splitlines())
        for first, second in ((2, 0), (0, 1), (0, 0)):
            with self.subTest(first=first, second=second):
                fake = f'''python3() {{
  case "$4" in
    unsafe) printf 'unsafe\\n'; return {first} ;;
    checked) printf 'checked\\n'; return {second} ;;
    *) return 99 ;;
  esac
}}
'''
                run = subprocess.run([shell, "-e", "-c", fake + commands], capture_output=True, text=True, timeout=10)
                self.assertEqual(run.stdout.splitlines(), ["unsafe", "checked"])
                self.assertEqual(run.returncode == 0, first == 0 and second == 0)


if __name__ == "__main__":
    unittest.main()
