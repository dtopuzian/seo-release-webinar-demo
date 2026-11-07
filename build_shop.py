#!/usr/bin/env python3
"""Build the self-contained webinar shop; no network or product imports."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import sys


SOURCE = Path(__file__).resolve().parent / "shop"
VERSIONS = {"good": "v1", "bad": "v2"}


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def artifact_digest(files: dict) -> str:
    """SHA-256 of canonical JSON for the exact {path: {sha256, size}} map."""
    return hashlib.sha256(canonical_json(files)).hexdigest()


def render(template: str, **values: str) -> str:
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values[match[1]], template)


def read_template(name: str) -> str:
    return (SOURCE / "templates" / name).read_text(encoding="utf-8")


def money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " ₽"


def build(variant: str, output: Path) -> dict:
    if variant not in VERSIONS:
        raise ValueError("variant must be good or bad")
    output = output.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("output must be a new or empty directory; existing artifacts are never overwritten")
    if output == SOURCE or SOURCE in output.parents:
        raise ValueError("output must not be inside the shop source directory")

    version = VERSIONS[variant]
    products = json.loads((SOURCE / "products.json").read_text(encoding="utf-8"))
    layout = read_template("layout.html")
    card_template = read_template("card.html")
    cards = []
    for product in products:
        cards.append(render(card_template, ID=product["id"], NAME=html.escape(product["name"]),
                            TYPE=html.escape(product["type"]), MATERIAL=html.escape(product["material"]),
                            PRICE=money(product["price"]), COLOR=product["color"],
                            LABEL=html.escape(product["label"])))
    all_cards = "\n".join(cards)
    pages = {
        "index.html": ("СЛОЙ — предметы для дома, в котором хочется остаться",
                       "Свет, керамика и текстиль СЛОЙ. Небольшая коллекция выразительных предметов для спокойного, тёплого дома. Учебная витрина.",
                       render(read_template("index.html"), CARDS=all_cards), True, "home"),
        "catalog.html": ("Коллекция предметов для дома — СЛОЙ",
                         "Настольная лампа, керамическая ваза, поднос и мягкий плед. Рассмотрите материалы, оттенки и детали коллекции СЛОЙ.",
                         render(read_template("catalog.html"), CARDS=all_cards), True, "catalog"),
        "about.html": ("О подходе СЛОЙ — меньше вещей, больше смысла",
                       "История учебного бренда СЛОЙ: натуральные оттенки, простые формы и внимание к повседневным ритуалам дома.",
                       read_template("about.html"), False, "about"),
        "cart.html": ("Ваша корзина — СЛОЙ",
                      "Соберите свою подборку предметов СЛОЙ: измените количество и посмотрите итог. Демонстрационная корзина без оформления заказа и оплаты.",
                      read_template("cart.html"), False, "cart"),
    }
    for product in products:
        details = "".join(f'<div><dt>{html.escape(key)}</dt><dd>{html.escape(value)}</dd></div>'
                          for key, value in product["details"].items())
        related = "\n".join(card for item, card in zip(products, cards) if item["id"] != product["id"])
        body = render(read_template("product.html"), ID=product["id"], NAME=html.escape(product["name"]),
                      TYPE=html.escape(product["type"]), PRICE=money(product["price"]),
                      DESCRIPTION=html.escape(product["description"]), DETAILS=details,
                      COLOR=product["color"], COLOR_NAME=html.escape(product["color_name"]),
                      MATERIAL=html.escape(product["material"]), RELATED=related)
        pages[product["id"] + ".html"] = (
            product["name"] + " — " + product["type"].lower() + " | СЛОЙ",
            product["description"] + " Учебная витрина СЛОЙ.", body, True, "product")

    payload: dict[str, bytes] = {}
    for name, (title, description, content, important, section) in pages.items():
        robots = "noindex,follow" if variant == "bad" and important else "index,follow"
        document = render(layout, TITLE=html.escape(title), DESCRIPTION=html.escape(description, quote=True),
                          ROBOTS=robots, CONTENT=content, VERSION=version, SECTION=section,
                          CATALOG_CURRENT=' aria-current="page"' if section == "catalog" else "",
                          ABOUT_CURRENT=' aria-current="page"' if section == "about" else "")
        payload[name] = document.encode("utf-8")
    for path in sorted((SOURCE / "assets").rglob("*")):
        if path.is_symlink():
            raise ValueError("source assets must not contain symlinks")
        if path.is_file():
            payload[path.relative_to(SOURCE).as_posix()] = path.read_bytes()
    payload["assets/products.json"] = canonical_json(products) + b"\n"
    payload["robots.txt"] = b"User-agent: *\nAllow: /\n"
    files = {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
             for name, data in sorted(payload.items())}
    manifest = {"schema_version": 1, "version": version, "variant": variant,
                "artifact_sha256": artifact_digest(files), "files": files,
                "important_pages": [name for name, values in pages.items() if values[3]]}
    # manifest.json binds the payload but does not hash itself. No timestamps:
    # identical source and variant produce an identical deployable directory.
    output.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(payload.items()):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    # A manifest is only present once every payload file has been written.
    (output / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("variant", choices=VERSIONS)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        manifest = build(args.variant, args.output)
    except (OSError, ValueError) as error:
        parser.exit(2, f"build failed: {error}\n")
    print(json.dumps({"output": str(args.output.resolve()), "version": manifest["version"],
                      "artifact_sha256": manifest["artifact_sha256"],
                      "file_count": len(manifest["files"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
