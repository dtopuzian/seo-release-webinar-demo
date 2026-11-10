"""Build once and admit a release only after all three prerequisite jobs pass."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build_shop import artifact_digest
from ci import client, run as runner


def built_digest(folder):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    client.require(type(manifest) is dict and manifest.get("schema_version") == 1
                   and manifest.get("version") == "v2" and manifest.get("variant") == "bad"
                   and manifest.get("important_pages") == ["index.html"])
    expected_files = {"index.html", "robots.txt", "assets/style.css",
                      "assets/landing.js", "assets/goose.svg"}
    files = {}
    for path in folder.rglob("*"):
        client.require(not path.is_symlink())
        if path.is_dir():
            continue
        name = path.relative_to(folder).as_posix()
        if name == "manifest.json":
            continue
        client.require(path.is_file() and name in expected_files)
        data = path.read_bytes()
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    client.require(set(files) == expected_files and manifest.get("files") == files)
    digest = artifact_digest(files)
    client.require(client.matches(r"[0-9a-f]{64}", manifest.get("artifact_sha256"))
                   and manifest["artifact_sha256"] == digest)
    return digest


def build(environment):
    output = environment.get("GITHUB_OUTPUT")
    client.require(bool(output) and Path(output).is_file())
    with tempfile.TemporaryDirectory(prefix="webinar-build-", dir=environment.get("RUNNER_TEMP")) as folder:
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "build_shop.py"), "bad", folder, "--target", "checked"],
            check=True, capture_output=True, timeout=30)
        result.check_returncode()
        digest = built_digest(Path(folder))
    with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"artifact_sha256={digest}\n")
    print(f"Сборка v2 проверена: {digest}")
    return 0


def release(environment):
    unit, built, seo = (environment.get(name) for name in
                        ("DEMO_UNIT_RESULT", "DEMO_BUILD_RESULT", "DEMO_SEO_RESULT"))
    if (unit, built, seo) != ("success", "success", "success"):
        reason = ("SEO-проверки не пройдены" if (unit, built, seo) == ("success", "success", "failure")
                  else "предыдущие этапы не завершились успешно")
        print(f"Релиз заблокирован: {reason}. Публикация не выполнялась", file=sys.stderr)
        return 2
    # The helper cannot fall back to the legacy optional-digest route.
    client.require(environment.get("DEMO_REQUIRE_ARTIFACT_SHA256") == "true")
    client.expected_binding({**environment, "DEMO_SCENE": "checked"})
    # The existing client obtains fresh status, validates the full binding and
    # invokes the server gate; no local receipt can authorise publication.
    result = runner.main(["release", "checked"])
    if result:
        print("Ошибка публикации: успешный выпуск не подтверждён.", file=sys.stderr)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("build", "release"))
    args = parser.parse_args(argv)
    try:
        return {"build": build, "release": release}[args.stage](dict(os.environ))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        message = ("Сборка не подтверждена." if args.stage == "build"
                   else "Ошибка публикации: контекст выпуска не подтверждён.")
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
