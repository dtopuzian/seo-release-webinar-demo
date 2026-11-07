"""GitHub-to-demo SSH client. Only verified, bounded fields leave it."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HOST = "93.99.105.4"
USER = "seo-webinar-demo"
ORIGIN = "https://shop.93-99-105-4.sslip.io:9443"
REPOSITORY = "dtopuzian/seo-release-webinar-demo"
RESULT_FILE = Path("demo-result.json")
BINDING_KEYS = ("run", "target", "variant", "artifact_sha256", "version", "source_sha")
OUTCOMES = {"prepared", "checked", "blocked", "published", "error"}
CHECK_CODES = {"pass", "unexpected_noindex", "page_not_checked", "required_http_200_complete",
               "meta_robots_unknown", "meta_robots_not_checked"}
REASON_CODES = (CHECK_CODES - {"pass"}) | {"check_incomplete", "incomplete_check"}


def matches(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def require(condition):
    if not condition:
        raise ValueError("unverified_demo_result")


def expected_binding(environment):
    expected = {"run": environment.get("GITHUB_RUN_ID"), "target": environment.get("DEMO_SCENE"),
                "variant": environment.get("DEMO_VARIANT", "bad"),
                "source_sha": environment.get("DEMO_RUNTIME_SHA")}
    require(matches(r"[1-9][0-9]{0,19}", expected["run"]) and expected["target"] in {"unsafe", "checked"}
            and expected["variant"] in {"good", "bad"}
            and matches(r"[0-9a-f]{40}", expected["source_sha"])
            and workflow_url(environment, expected["run"]) is not None)
    return expected


def workflow_url(environment, run):
    """A link is built only for this configured repository and numeric run."""
    if (environment.get("GITHUB_REPOSITORY") != REPOSITORY
            or environment.get("GITHUB_SERVER_URL") != "https://github.com"
            or not matches(r"[1-9][0-9]{0,19}", run)
            or environment.get("GITHUB_RUN_ID") != run):
        return None
    return f"https://github.com/{REPOSITORY}/actions/runs/{run}"


def binding(value):
    require(type(value) is dict)
    clean = {key: value.get(key) for key in BINDING_KEYS}
    require(matches(r"[1-9][0-9]{0,19}", clean["run"])
            and clean["target"] in {"unsafe", "checked"}
            and clean["variant"] in {"good", "bad"}
            and matches(r"[0-9a-f]{40}", clean["source_sha"])
            and matches(r"[0-9a-f]{64}", clean["artifact_sha256"])
            and clean["version"] == {"good": "v1", "bad": "v2"}[clean["variant"]])
    return clean


def report_url(value, run, target):
    expected = f"{ORIGIN}/_reports/{run}/{target}/report.html"
    return value if type(value) is str and value == expected else None


def validated_result(value, expected, action, exit_code):
    """Validate the current receiver protocol; never echo unrecognised data."""
    require(type(value) is dict and action in {"prepare", "status", "check", "release"})
    candidate = binding(value)
    require(all(candidate[key] == item for key, item in expected.items()))
    require(value.get("digest") == candidate["artifact_sha256"])
    outcome = value.get("outcome")
    require(type(outcome) is str and outcome in OUTCOMES)
    raw_checks, reasons = value.get("checks"), value.get("reason_codes")
    require(type(raw_checks) is list and len(raw_checks) <= 20
            and type(reasons) is list and len(reasons) <= 20
            and all(type(code) is str and code in REASON_CODES for code in reasons))
    checks = []
    for item in raw_checks:
        require(type(item) is dict)
        path, code, passed = item.get("path"), item.get("code"), item.get("passed")
        http_status = item.get("http_status")
        robots_directive = item.get("robots_directive")
        require(matches(r"/(?:[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*\.html)?", path)
                and type(code) is str and code in CHECK_CODES and type(passed) is bool
                and passed == (code == "pass") and path not in {row["path"] for row in checks})
        require(http_status is None or (type(http_status) is int and 100 <= http_status <= 599))
        require(robots_directive is None or (type(robots_directive) is str
                                             and robots_directive in {"noindex", "none"}))
        require(code != "pass" or http_status == 200)
        require(robots_directive is None or code == "unexpected_noindex")
        checks.append({"path": path, "code": code, "passed": passed,
                       "http_status": http_status, "robots_directive": robots_directive})
    published = value.get("published")
    if published is not None:
        published = binding(published)
        require(published["target"] == candidate["target"])
    url = value.get("report_url")
    require(url is None or report_url(url, candidate["run"], candidate["target"]) is not None)
    if outcome == "checked" or (outcome == "published" and candidate["target"] == "checked"):
        require(len(checks) == 1 and checks[0]["path"] == "/" and checks[0]["passed"]
                and not reasons and url is not None)
    if outcome == "blocked":
        require(bool(reasons))
    if outcome == "published":
        require(published == candidate)
    # An SSH error with a forged PASS, or a zero exit with BLOCKED, is never success.
    require(type(exit_code) is int)
    if action == "check":
        require((exit_code, outcome) in {(0, "checked"), (10, "blocked")})
    elif action == "release":
        require(exit_code == 0 and outcome == "published")
    else:
        require(exit_code == 0)
    return {**candidate, "digest": candidate["artifact_sha256"], "outcome": outcome,
            "reason_codes": reasons, "checks": checks, "report_url": url, "published": published,
            "verified": True, "action": action, "exit_code": exit_code}


def unconfirmed(expected=None):
    return {**(expected or {}), "outcome": "unconfirmed", "reason_codes": ["operation_unverified"],
            "checks": [], "report_url": None, "published": None, "verified": False}


def save_result(value):
    RESULT_FILE.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "status", "check", "release"))
    args = parser.parse_args(argv)
    expected = None
    try:
        # Revoke any inherited/stale positive receipt before validation or SSH.
        save_result(unconfirmed())
        expected = expected_binding(os.environ)
        save_result(unconfirmed(expected))
        key, known = (os.environ.get(name) for name in ("DEMO_SSH_KEY_FILE", "DEMO_SSH_KNOWN_HOSTS_FILE"))
        require(bool(key and known and Path(key).is_file() and Path(known).is_file()))
        command = ["ssh", "-i", key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                   "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}",
                   "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                   "-o", "ServerAliveCountMax=3", f"{USER}@{HOST}"]

        def request(action):
            remote = f"demo-v1 {action} {expected['target']} {expected['variant']} {expected['run']}"
            result = subprocess.run(command + [remote], capture_output=True, timeout=240, check=False)
            require(len(result.stdout) <= 65536)
            return validated_result(json.loads(result.stdout), expected, action, result.returncode)

        # Refuse a wrong runtime SHA before either running the gate or publishing.
        # The receiver rechecks its stored binding while holding its own lock.
        if args.action in {"check", "release"}:
            request("status")
        clean = request(args.action)
        save_result(clean)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        print("Demo operation did not return a verified result.", file=sys.stderr)
        return 2
    print(json.dumps(clean, ensure_ascii=False, indent=2))
    return clean["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
