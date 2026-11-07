"""Run one fixed demo operation with short-lived SSH files and safe failures."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile

try:
    from . import client
except ImportError:
    import client


SECRETS = frozenset({"DEMO_SSH_KEY", "DEMO_SSH_KNOWN_HOSTS",
                     "DEMO_TELEGRAM_TOKEN", "DEMO_TELEGRAM_CHAT"})
HERE = Path(__file__).resolve().parent


def child_environment(environment, scene):
    result = {key: value for key, value in environment.items() if key not in SECRETS}
    result["DEMO_SCENE"] = scene
    result.pop("DEMO_SSH_KEY_FILE", None)
    result.pop("DEMO_SSH_KNOWN_HOSTS_FILE", None)
    return result


@contextmanager
def ssh_files(environment, child):
    key = environment.get("DEMO_SSH_KEY", "")
    known = environment.get("DEMO_SSH_KNOWN_HOSTS", "")
    if not 0 < len(key) <= 16384 or not 0 < len(known) <= 65536:
        raise ValueError("SSH configuration unavailable")
    previous = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="webinar-ssh-", dir=environment.get("RUNNER_TEMP")) as folder:
            result = dict(child)
            for name, value, variable in (("key", key, "DEMO_SSH_KEY_FILE"),
                                          ("known_hosts", known, "DEMO_SSH_KNOWN_HOSTS_FILE")):
                path = Path(folder) / name
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(value.replace("\r\n", "\n").strip() + "\n")
                result[variable] = str(path)
            yield result
    finally:
        os.umask(previous)


def execute(script, arguments, environment):
    return subprocess.run([sys.executable, "-B", str(HERE / script), *arguments],
                          env=environment, check=False, timeout=270).returncode


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "check", "release", "status", "notify"))
    parser.add_argument("scene", choices=("unsafe", "checked"))
    args = parser.parse_args(argv)
    environment = dict(os.environ)
    child = child_environment(environment, args.scene)
    # Credential preparation may fail before the client starts. Revoke a local
    # receipt first so a later notification cannot reuse a stale success.
    try:
        client.save_result(client.unconfirmed())
        client.save_result(client.unconfirmed(client.expected_binding(child)))
    except (OSError, ValueError, TypeError):
        print("Demo context could not be verified.", file=sys.stderr)
        return 2
    result = 2
    try:
        with ssh_files(environment, child) as secured:
            result = execute("client.py", ["status" if args.action == "notify" else args.action], secured)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Demo operation could not be verified.", file=sys.stderr)
    if args.action == "notify":
        # A failed fresh status still reaches the explicit unconfirmed notice.
        # SSH secrets/files are absent from this process's environment.
        notification = {**child, **{key: environment.get(key, "")
                                   for key in ("DEMO_TELEGRAM_TOKEN", "DEMO_TELEGRAM_CHAT")}}
        try:
            notified = execute("notify.py", [], notification)
        except (OSError, ValueError, subprocess.SubprocessError):
            print("Telegram notification unavailable; release state is unchanged.", file=sys.stderr)
            notified = 1
        return result if result != 0 else notified
    return result


if __name__ == "__main__":
    raise SystemExit(main())
