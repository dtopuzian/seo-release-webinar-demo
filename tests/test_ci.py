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
            "checks": [{"path": "/", "code": "unexpected_noindex" if blocked else "pass", "passed": not blocked,
                        "http_status": 200, "robots_directive": "noindex" if blocked else None}] if has_gate else [],
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

    def test_cancelled_ssh_leaves_no_reusable_positive_receipt(self):
        self.result_path.write_text(json.dumps({**payload("published"), "verified": True}), encoding="utf-8")
        with self.assertRaises(KeyboardInterrupt):
            self.invoke("release", side_effect=KeyboardInterrupt)
        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertFalse(result["verified"])
        self.assertEqual(result["outcome"], "unconfirmed")
        self.assertIsNone(result["published"])

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

    def test_exact_single_main_page_and_validated_factual_details(self):
        invalid = []
        for status in (True, 99, 600, "404", {"value": 404}):
            value = payload()
            value["checks"][0]["http_status"] = status
            invalid.append(value)
        for directive in ("PRIVATE_INPUT", [], False):
            value = payload()
            value["checks"][0]["robots_directive"] = directive
            invalid.append(value)
        for path in ("/catalog.html", "/index.html"):
            value = payload("checked")
            value["checks"][0]["path"] = path
            invalid.append(value)
        value = payload("checked")
        value["checks"].append({"path": "/extra.html", "code": "pass", "passed": True})
        invalid.append(value)
        for field, content in (("http_status", 404), ("http_status", None), ("robots_directive", "none")):
            value = payload("checked")
            value["checks"][0][field] = content
            invalid.append(value)
        value = payload("checked")
        del value["checks"][0]["http_status"]
        invalid.append(value)
        for value in invalid:
            with self.subTest(checks=value["checks"]), self.assertRaises(ValueError):
                client.validated_result(value, EXPECTED, "status", 0)

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

    def test_blocked_has_only_fact_and_two_clickable_links(self):
        value = self.verified()
        text = notify.message(value, "checked")
        self.assertEqual(text, '⛔️ Деплой остановлен.\n\nПричина: на главной странице найден noindex.\n\n'
                         'Ссылка на run: <a href="https://github.com/dtopuzian/seo-release-webinar-demo/actions/runs/12345">Открыть запуск</a>\n'
                         'Ссылка на отчёт: <a href="' + value["report_url"] + '">Открыть отчёт</a>')
        self.assertNotIn("✅", text)
        # Extra page findings and old live receipts do not lengthen the message.
        value["checks"] += [{"path": f"/page{i}.html", "code": "unexpected_noindex", "passed": False} for i in range(19)]
        value["published"] = {key: value[key] for key in client.BINDING_KEYS}
        self.assertEqual(notify.message(value, "checked"), text)

    def test_http_and_incomplete_reasons_are_factual(self):
        value = self.verified()
        for status, expected in ((404, "Главная страница вернула HTTP 404."),
                                 (503, "Главная страница вернула HTTP 503."),
                                 (200, "Не удалось завершить SEO-проверку."),
                                 (None, "Не удалось завершить SEO-проверку.")):
            with self.subTest(status=status):
                value["checks"] = [{"path": "/", "code": "required_http_200_complete", "passed": False, "http_status": status}]
                self.assertIn("Причина: " + expected, notify.message(value, "checked"))
        value["checks"] = []
        value["report_url"] = None
        text = notify.message(value, "checked")
        self.assertIn("Не удалось завершить SEO-проверку", text)
        self.assertNotIn("Открыть отчёт", text)

        # A fresh receiver status after the collector crashed confirms that
        # checking did not finish, even though it could not produce a report.
        raw = payload("error")
        raw.update(reason_codes=["check_incomplete"], report_url=None)
        error = client.validated_result(raw, EXPECTED, "status", 0)
        text = notify.message(error, "checked")
        self.assertIn("⛔️ Деплой остановлен.", text)
        self.assertIn("Не удалось завершить SEO-проверку.", text)
        self.assertNotIn("Открыть отчёт", text)

    def test_none_and_old_receipts_do_not_invent_literal_noindex(self):
        value = self.verified()
        value["checks"][0]["robots_directive"] = "none"
        self.assertIn("на главной странице найден none.", notify.message(value, "checked"))
        del value["checks"][0]["robots_directive"]
        text = notify.message(value, "checked")
        self.assertIn("Не удалось завершить SEO-проверку.", text)
        self.assertNotIn("найден noindex", text)

    def test_success_shows_only_run_link_and_no_report_or_version(self):
        text = notify.message(self.verified("published"), "checked")
        self.assertEqual(text, '✅ Деплой выполнен.\n\nСсылка на run: <a href="https://github.com/dtopuzian/seo-release-webinar-demo/actions/runs/12345">Открыть запуск</a>')

    def test_none_and_invalid_urls_are_ignored_without_error(self):
        invalid = [None, [], {}, 123, "https://evil.example/report.html",
                   client.ORIGIN + "/_reports/12345/checked/../unsafe/report.html",
                   client.ORIGIN + "/_reports/999/checked/report.html",
                   client.ORIGIN + "/_reports/12345/checked/report.html?token=PRIVATE"]
        for url in invalid:
            with self.subTest(url=url), patch.dict(os.environ, {"GITHUB_SERVER_URL": "https://evil.example/"}):
                text = notify.message({**self.verified(), "report_url": url}, "checked")
                self.assertNotIn("Открыть отчёт", text)
                self.assertNotIn("Открыть запуск", text)
                self.assertNotIn("PRIVATE", text)

    def test_unverified_success_has_no_success_heading_or_report(self):
        value = {**payload("published"), "verified": False}
        text = notify.message(value, "checked")
        self.assertIn("Деплой не подтверждён", text)
        self.assertNotIn("✅", text)
        self.assertNotIn("Открыть отчёт", text)

    def test_github_link_binds_exact_repository_and_current_run(self):
        value = self.verified()
        self.assertIn("https://github.com/dtopuzian/seo-release-webinar-demo/actions/runs/12345",
                      notify.message(value, "checked"))
        for change in ({"GITHUB_REPOSITORY": "other/repository"}, {"GITHUB_RUN_ID": "999"}):
            with patch.dict(os.environ, change):
                self.assertNotIn("Открыть запуск", notify.message(value, "checked"))

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
        data = json.loads(request.data)
        self.assertEqual(data["parse_mode"], "HTML")
        text = data["text"]
        self.assertIn("Деплой не подтверждён", text)
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

    def test_cancellation_cleans_up_ssh_files_without_sending_telegram(self):
        with patch.object(runner, "execute", side_effect=KeyboardInterrupt) as child:
            with self.assertRaises(KeyboardInterrupt):
                runner.main(["notify", "checked"])
        self.assertEqual(child.call_count, 1)
        self.assertEqual(child.call_args.args[:2], ("client.py", ["status"]))
        self.assertEqual(list(self.root.glob("webinar-ssh-*")), [])
        self.assertFalse(json.loads(client.RESULT_FILE.read_text())["verified"])

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

    def step(self, text, name):
        match = re.search(r"^      - name: [^\n]*\n        id: " + re.escape(name) + r"\n((?:[ ]{8}.*\n|\n)*)", text, re.MULTILINE)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_scenario_names_short_steps_and_reset_without_telegram(self):
        expected = {
            "1-without-seo.yml": ("1. Выпустить без SEO-проверки", ["Подготовка", "Публикация", "Telegram"]),
            "2-with-seo.yml": ("2. Выпустить с SEO-проверкой", ["Подготовка", "SEO-проверка", "Публикация", "Telegram"]),
            "3-reset.yml": ("3. Вернуть обе витрины к v1", ["Подготовка", "SEO-проверка", "Публикация", "Результат"]),
        }
        for filename, (title, steps) in expected.items():
            text = self.workflows[filename]
            self.assertEqual(text.splitlines()[0], f"name: '{title}'")
            self.assertEqual(re.findall(r"^      - name: '([^']+)'$", text, re.MULTILINE), steps)
        self.assertNotIn("DEMO_TELEGRAM", self.workflows["3-reset.yml"])
        self.assertNotIn("ci/run.py notify", self.workflows["3-reset.yml"])

    def test_manual_workflows_share_queue_with_one_pinned_checkout_and_one_job(self):
        self.assertEqual(len(self.workflows), 3)
        for text in self.workflows.values():
            self.assertIn("on:\n  workflow_dispatch:\n", text)
            self.assertIn("permissions:\n  contents: read\n", text)
            self.assertIn("concurrency:\n  group: webinar-demo\n  queue: max\n  cancel-in-progress: false\n", text)
            self.assertRegex(text, r"DEMO_RUNTIME_SHA: '[0-9a-f]{40}'")
            jobs = text.split("jobs:\n", 1)[1]
            self.assertEqual(re.findall(r"^  [a-z_]+:", jobs, re.MULTILINE), ["  release:"])
            self.assertEqual(re.findall(r"uses: (\S+)", text), ["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"])
            self.assertEqual(text.count("persist-credentials: false"), 1)
            self.assertIn("github.repository == 'dtopuzian/seo-release-webinar-demo'", text)
            self.assertIn("github.ref == format('refs/heads/{0}', github.event.repository.default_branch)", text)
            self.assertNotIn("continue-on-error", text)
            self.assertNotIn("upload-artifact", text)
            self.assertNotIn("secrets.", text.split("    steps:\n", 1)[0])
            self.assertNotIn("DEMO_TELEGRAM", self.step(text, "prepare"))
            self.assertNotIn("DEMO_TELEGRAM", self.step(text, "publish"))

    def test_failed_steps_skip_publish_and_final_step_requires_checkout_without_cancellation(self):
        final_condition = "if: ${{ !cancelled() && steps.checkout.outcome == 'success' }}"
        for name, text in self.workflows.items():
            final = "status" if name == "3-reset.yml" else "notify"
            # Ordinary steps retain GitHub's default success() condition. Only the
            # final status/notification runs after failure, never after checkout
            # failure or cancellation. No local Actions expression interpreter.
            for operation in ("prepare", "publish"):
                self.assertNotIn("if:", self.step(text, operation))
            if name != "1-without-seo.yml":
                check = self.step(text, "check")
                self.assertNotIn("if:", check)
                self.assertNotIn("||", check)
                self.assertLess(text.index("id: check"), text.index("id: publish"))
            self.assertIn(final_condition, self.step(text, final))
            self.assertNotIn("always()", text)
        self.assertNotIn("id: check\n", self.workflows["1-without-seo.yml"])
        self.assertIn("ci/run.py check unsafe", self.workflows["3-reset.yml"])
        self.assertIn("ci/run.py check checked", self.workflows["3-reset.yml"])

    def test_real_reset_shell_reads_both_scenes_even_when_first_fails(self):
        shell = shutil.which("sh")
        if not shell:
            self.skipTest("POSIX shell unavailable")
        self.assertNotIn("DEMO_TELEGRAM", self.workflows["3-reset.yml"])
        block = self.step(self.workflows["3-reset.yml"], "status")
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
