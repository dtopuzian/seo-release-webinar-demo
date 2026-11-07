#!/usr/bin/env python3
"""Build one self-contained webinar landing page without network access."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import sys


SOURCE = Path(__file__).resolve().parent / "landing"
VERSIONS = {"good": "v1", "bad": "v2"}
SITES = {
    "unsafe": {
        "NUMBER": "01", "THEME": "cat", "NAME": "Кот на удалёнке",
        "TITLE": "Кот на удалёнке — работаем лапками",
        "DESCRIPTION": "Дедлайны подождут. Кот на удалёнке помогает сделать паузу, выдохнуть и вернуться к работе с мурчащим настроением.",
        "EYEBROW": "Ваш самый пушистый коллега",
        "HEADLINE": "Работаем лапками.<br>Отдыхаем с душой.",
        "SUBTITLE": "Закрываем задачи. Открываем вкусняшки.<br>Главное, чтобы созвон не мешал тихому часу.",
        "BUTTON": "Позвать на созвон", "REACTION": "Мяу. Кот уже на созвоне. Камеру не включит.",
        "ASIDE": "На связи. Но это не точно.", "FOOTNOTE": "Всё под контролем. Особенно клавиатура.",
        "IMAGE_ALT": "Рыжий кот в очках работает за ноутбуком рядом с чашкой кофе",
    },
    "checked": {
        "NUMBER": "02", "THEME": "goose", "NAME": "Гусь на связи",
        "TITLE": "Гусь на связи — слышно отлично, понятно не всё",
        "DESCRIPTION": "Гусь на связи принимает важные звонки, поддерживает боевой настрой команды и всегда готов сказать уверенное га-га.",
        "EYEBROW": "Поддержка с характером",
        "HEADLINE": "Слышно отлично.<br>Понятно не всё.",
        "SUBTITLE": "Если что-то пошло не так, зовите гуся.<br>Он выслушает. Кивнёт. И громко поддержит.",
        "BUTTON": "Связаться с гусём", "REACTION": "Га-га! Ваш вопрос очень важен для нашего гуся.",
        "ASIDE": "Готов к важному «га-га».", "FOOTNOTE": "Ни один созвон не останется без внимания.",
        "IMAGE_ALT": "Белый гусь в жёлтой гарнитуре отвечает на звонок за рабочим столом",
    },
}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def artifact_digest(files: dict) -> str:
    """Bind the exact {path: {sha256, size}} map, not a mutable version label."""
    return hashlib.sha256(canonical_json(files)).hexdigest()


def render(template: str, **values: str) -> str:
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values[match[1]], template)


def build(variant: str, output: Path, target: str = "unsafe") -> dict:
    if variant not in VERSIONS or target not in SITES:
        raise ValueError("variant must be good or bad; target must be unsafe or checked")
    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output must be a new or empty directory; existing artifacts are never overwritten")
    if output == SOURCE or SOURCE in output.parents:
        raise ValueError("output must not be inside the landing source directory")

    site = SITES[target]
    values = {key: html.escape(value, quote=True) for key, value in site.items()}
    for key in ("HEADLINE", "SUBTITLE"):
        values[key] = "<br>".join(html.escape(part) for part in site[key].split("<br>"))
    values.update(VERSION=VERSIONS[variant], ROBOTS="noindex,follow" if variant == "bad" else "index,follow")
    payload = {"index.html": render((SOURCE / "index.html").read_text(encoding="utf-8"), **values).encode("utf-8"),
               "robots.txt": b"User-agent: *\nAllow: /\n"}
    for name in ("style.css", "landing.js", site["THEME"] + ".svg"):
        path = SOURCE / "assets" / name
        if path.is_symlink():
            raise ValueError("source assets must not contain symlinks")
        payload["assets/" + name] = path.read_bytes()
    files = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
             for name, data in sorted(payload.items())}
    manifest = {"schema_version": 1, "version": VERSIONS[variant], "variant": variant,
                "artifact_sha256": artifact_digest(files), "files": files,
                "important_pages": ["index.html"]}
    output.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(payload.items()):
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (output / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("variant", choices=VERSIONS)
    parser.add_argument("output", type=Path)
    parser.add_argument("--target", choices=SITES, default="unsafe")
    args = parser.parse_args()
    try:
        manifest = build(args.variant, args.output, args.target)
    except (OSError, ValueError) as error:
        parser.exit(2, f"build failed: {error}\n")
    print(json.dumps({"output": str(args.output.resolve()), "target": args.target,
                      "version": manifest["version"], "artifact_sha256": manifest["artifact_sha256"],
                      "file_count": len(manifest["files"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
