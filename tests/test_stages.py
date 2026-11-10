"""Build/output and cross-job admission contracts; no live SSH or Telegram."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import client, notify, run as runner, stages
from test_ci import ENV, completed, payload


DIGEST_ENV = {**ENV, "DEMO_REQUIRE_ARTIFACT_SHA256": "true",
              "DEMO_EXPECTED_ARTIFACT_SHA256": "b" * 64}
SUCCESS = {"DEMO_UNIT_RESULT": "success", "DEMO_BUILD_RESULT": "success",
           "DEMO_SEO_RESULT": "success"}


class StageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.output = self.root / "github-output"
        self.output.write_text("existing=preserved\n", encoding="utf-8")
        self.enterContext(patch.dict(os.environ, {**DIGEST_ENV, **SUCCESS,
            "GITHUB_OUTPUT": str(self.output)}, clear=True))
        self.enterContext(patch.object(client, "RESULT_FILE", self.root / "demo-result.json"))

    def invoke(self, action):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = stages.main([action])
        return result, output.getvalue()

    def test_one_real_build_exports_digest_of_exact_checked_bad_inventory(self):
        execute = subprocess.run
        manifests = []
        def inspect(command, **kwargs):
            result = execute(command, **kwargs)
            args = list(map(str, command))
            self.assertEqual(args[-2:], ["--target", "checked"])
            output = Path(args[args.index("bad") + 1])
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            canonical = json.dumps(manifest["files"], ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(manifest["artifact_sha256"], hashlib.sha256(canonical).hexdigest())
            self.assertEqual(manifest["variant"], "bad")
            self.assertEqual(manifest["version"], "v2")
            self.assertEqual(set(manifest["files"]),
                             {"index.html", "robots.txt", "assets/style.css", "assets/landing.js", "assets/goose.svg"})
            manifests.append(manifest)
            return result
        with patch.object(stages.subprocess, "run", side_effect=inspect) as build, \
                patch.object(runner, "main") as remote:
            code, _ = self.invoke("build")
        self.assertEqual(code, 0)
        self.assertEqual(build.call_count, 1)
        remote.assert_not_called()
        self.assertEqual(self.output.read_text(encoding="utf-8"),
                         "existing=preserved\nartifact_sha256=" + manifests[0]["artifact_sha256"] + "\n")

    def test_build_rejects_corrupted_outputs_and_never_exports_a_digest(self):
        execute = subprocess.run
        for corruption in ("digest", "bytes", "missing", "extra", "json"):
            with self.subTest(corruption=corruption):
                def corrupt(command, **kwargs):
                    result = execute(command, **kwargs)
                    args = list(map(str, command))
                    output = Path(args[args.index("bad") + 1])
                    manifest_path = output / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if corruption == "digest":
                        manifest["artifact_sha256"] = "c" * 64
                        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    elif corruption == "bytes":
                        (output / "index.html").write_text("different candidate", encoding="utf-8")
                    elif corruption == "missing":
                        (output / "assets/goose.svg").unlink()
                    elif corruption == "extra":
                        (output / "extra.html").write_text("unbound page", encoding="utf-8")
                    else:
                        manifest_path.write_text("not JSON", encoding="utf-8")
                    return result
                with patch.object(stages.subprocess, "run", side_effect=corrupt) as build:
                    code, _ = self.invoke("build")
                self.assertNotEqual(code, 0)
                self.assertEqual(build.call_count, 1)
                self.assertEqual(self.output.read_text(encoding="utf-8"), "existing=preserved\n")

    def test_failed_builder_does_not_emit_output(self):
        with patch.object(stages.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)):
            self.assertNotEqual(self.invoke("build")[0], 0)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "existing=preserved\n")

    def test_each_failed_skipped_cancelled_or_missing_need_blocks_even_with_stale_pass(self):
        client.save_result({**payload("checked"), "verified": True})
        for variable in SUCCESS:
            for result in ("failure", "skipped", "cancelled", "", None):
                environment = dict(os.environ)
                environment.pop(variable)
                if result is not None:
                    environment[variable] = result
                with self.subTest(need=variable, result=result), \
                        patch.dict(os.environ, environment, clear=True), patch.object(runner, "main") as release:
                    code, text = self.invoke("release")
                self.assertNotEqual(code, 0)
                release.assert_not_called()
                self.assertIn("Релиз заблокирован", text)
                self.assertIn("Публикация не выполнялась", text)

    def test_successful_needs_allow_one_release_and_errors_are_publication_errors(self):
        for result in (0, 2, 10):
            with self.subTest(result=result), patch.object(runner, "main", return_value=result) as release:
                code, text = self.invoke("release")
            release.assert_called_once_with(["release", "checked"])
            self.assertEqual(code, result)
            if result:
                self.assertIn("Ошибка публикации", text)
                self.assertNotIn("SEO-проверки не пройдены", text)


class RequiredDigestTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.key, self.known = self.root / "key", self.root / "known"
        self.key.write_text("TEST KEY", encoding="utf-8")
        self.known.write_text("TEST HOST", encoding="utf-8")
        self.enterContext(patch.dict(os.environ, {**DIGEST_ENV, **SUCCESS,
            "DEMO_SSH_KEY_FILE": str(self.key), "DEMO_SSH_KNOWN_HOSTS_FILE": str(self.known),
            "DEMO_SSH_KEY": "TEST KEY", "DEMO_SSH_KNOWN_HOSTS": "TEST HOST",
            "RUNNER_TEMP": str(self.root)}, clear=True))
        self.enterContext(patch.object(client, "RESULT_FILE", self.root / "demo-result.json"))

    def call_client(self, action, **transport):
        with patch.object(client.subprocess, "run", **transport) as ssh, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = client.main([action])
        return code, ssh, json.loads(client.RESULT_FILE.read_text(encoding="utf-8"))

    def test_digest_is_opt_in_for_legacy_and_strict_when_present(self):
        self.assertNotIn("artifact_sha256", client.expected_binding(ENV))
        self.assertEqual(client.expected_binding(DIGEST_ENV)["artifact_sha256"], "b" * 64)
        self.assertEqual(client.expected_binding({**ENV, "DEMO_EXPECTED_ARTIFACT_SHA256": "b" * 64})["artifact_sha256"], "b" * 64)
        for value in (None, "", "B" * 64, "b" * 63, "b" * 64 + "\n", "x" * 64):
            with self.subTest(value=value):
                environment = {**ENV, "DEMO_REQUIRE_ARTIFACT_SHA256": "true"}
                if value is not None:
                    environment["DEMO_EXPECTED_ARTIFACT_SHA256"] = value
                with self.assertRaises(ValueError):
                    client.expected_binding(environment)
        for flag in ("", "false", "TRUE", "0"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                client.expected_binding({**DIGEST_ENV, "DEMO_REQUIRE_ARTIFACT_SHA256": flag})
        with self.assertRaises(ValueError):
            client.expected_binding({**ENV, "DEMO_EXPECTED_ARTIFACT_SHA256": ""})

    def test_missing_or_malformed_required_digest_blocks_every_operation_before_ssh(self):
        for digest in (None, "", "invalid"):
            environment = dict(os.environ)
            environment.pop("DEMO_EXPECTED_ARTIFACT_SHA256")
            if digest is not None:
                environment["DEMO_EXPECTED_ARTIFACT_SHA256"] = digest
            for operation in ("prepare", "status", "check", "release", "notify"):
                with self.subTest(digest=digest, operation=operation), \
                        patch.dict(os.environ, environment, clear=True), \
                        patch.object(runner, "execute") as child, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = runner.main([operation, "checked"])
                self.assertNotEqual(code, 0)
                child.assert_not_called()
                self.assertFalse(json.loads(client.RESULT_FILE.read_text())["verified"])

    def test_wrong_binding_blocks_before_check_or_release_and_cannot_notify(self):
        mutations = {"artifact_sha256": "c" * 64, "run": "999", "source_sha": "c" * 40,
                     "target": "unsafe", "variant": "good"}
        for field, wrong in mutations.items():
            raw = payload("prepared")
            raw[field] = wrong
            if field == "artifact_sha256":
                raw["digest"] = wrong
            if field == "variant":
                raw["version"] = "v1"
            for operation in ("check", "release"):
                with self.subTest(field=field, operation=operation):
                    code, ssh, receipt = self.call_client(operation, return_value=completed(raw))
                    self.assertEqual(code, 2)
                    self.assertEqual(ssh.call_count, 1)
                    self.assertEqual(ssh.call_args.args[0][-1], "demo-v1 status checked bad 12345")
                    self.assertFalse(receipt["verified"])
                    self.assertIsNone(notify.message(receipt, "checked"))

    def test_preparation_and_fresh_status_also_bind_digest(self):
        for action in ("prepare", "status"):
            with self.subTest(action=action):
                raw = payload("prepared")
                raw.update(artifact_sha256="c" * 64, digest="c" * 64)
                code, ssh, receipt = self.call_client(action, return_value=completed(raw))
                self.assertEqual(code, 2)
                self.assertEqual(ssh.call_count, 1)
                self.assertFalse(receipt["verified"])

    def bridge(self, script, arguments, environment):
        # Exercise wrapper, client and notification together without launching
        # a child interpreter; only SSH and Telegram themselves are substituted.
        with patch.dict(os.environ, environment, clear=True):
            if script == "client.py":
                return client.main(arguments)
            self.assertEqual(script, "notify.py")
            with patch.object(notify.Path, "read_bytes", return_value=client.RESULT_FILE.read_bytes()):
                return notify.main()

    def test_success_and_release_only_rerun_use_fresh_status_and_same_digest(self):
        for attempt in range(2):
            with self.subTest(attempt=attempt), patch.object(runner, "execute", side_effect=self.bridge), \
                    patch.object(client.subprocess, "run", side_effect=[completed(payload("checked")), completed(payload("published"))]) as ssh, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = stages.main(["release"])
            self.assertEqual(code, 0)
            self.assertEqual([call.args[0][-1] for call in ssh.call_args_list],
                             ["demo-v1 status checked bad 12345", "demo-v1 release checked bad 12345"])
            self.assertEqual(json.loads(client.RESULT_FILE.read_text())["artifact_sha256"], "b" * 64)

    def test_noindex_blocks_release_after_one_notification_even_if_telegram_fails(self):
        for reply, notification_code in ((b'{"ok":true}', 0), (b'{"ok":false}', 1)):
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = reply
            responses = [completed(payload("prepared")), completed(payload("blocked"), 10),
                         completed(payload("blocked"))]
            with self.subTest(reply=reply), \
                    patch.dict(os.environ, {"DEMO_TELEGRAM_TOKEN": "123:TEST_ONLY", "DEMO_TELEGRAM_CHAT": "456"}), \
                    patch.object(runner, "execute", side_effect=self.bridge), \
                    patch.object(client.subprocess, "run", side_effect=responses) as ssh, \
                    patch.object(notify, "build_opener", return_value=opener), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(["check", "checked"]), 10)
                self.assertEqual(runner.main(["notify", "checked"]), notification_code)
                with patch.dict(os.environ, {"DEMO_SEO_RESULT": "failure"}):
                    self.assertNotEqual(stages.main(["release"]), 0)
            self.assertEqual([call.args[0][-1] for call in ssh.call_args_list],
                             ["demo-v1 status checked bad 12345", "demo-v1 check checked bad 12345",
                              "demo-v1 status checked bad 12345"])
            self.assertEqual(opener.open.call_count, 1)

    def test_notification_uses_fresh_digest_and_sends_only_confirmed_stop_once(self):
        stop = payload("blocked")
        foreign = deepcopy(stop)
        foreign.update(artifact_sha256="c" * 64, digest="c" * 64)
        cases = [(completed(stop), True), (completed(foreign), False),
                 (completed(payload("checked")), False),
                 (subprocess.CompletedProcess([], 255, b"not JSON", b"private error"), False)]
        for response, should_notify in cases:
            with self.subTest(response=response.returncode, notify=should_notify):
                opener = MagicMock()
                opener.open.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
                with patch.dict(os.environ, {"DEMO_TELEGRAM_TOKEN": "123:TEST_ONLY", "DEMO_TELEGRAM_CHAT": "456"}), \
                        patch.object(runner, "execute", side_effect=self.bridge), \
                        patch.object(client.subprocess, "run", return_value=response) as ssh, \
                        patch.object(notify, "build_opener", return_value=opener), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    runner.main(["notify", "checked"])
                self.assertEqual(ssh.call_count, 1)
                self.assertEqual(ssh.call_args.args[0][-1], "demo-v1 status checked bad 12345")
                self.assertEqual(opener.open.call_count, int(should_notify))
                if should_notify:
                    message = json.loads(opener.open.call_args.args[0].data)["text"]
                    self.assertIn("найден noindex", message)
                    self.assertIn(stop["report_url"], message)


if __name__ == "__main__":
    unittest.main()
