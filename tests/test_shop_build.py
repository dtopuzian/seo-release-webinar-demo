"""Artifact contracts for the webinar storefront; standard library only."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urljoin, urlsplit


SPEC = importlib.util.spec_from_file_location("build_shop", Path(__file__).parents[1] / "build_shop.py")
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class Document(HTMLParser):
    def __init__(self, text: str):
        super().__init__()
        self.references = []
        self.h1_count = 0
        self.title = ""
        self.description = ""
        self.robots = ""
        self._in_title = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.h1_count += tag == "h1"
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            if values.get("name") == "description":
                self.description = values.get("content", "")
            if values.get("name") == "robots":
                self.robots = values.get("content", "")
        for key in ("src", "href"):
            if key in values:
                self.references.append(values[key])

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


class ShopArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.good = self.root / "good"
        self.bad = self.root / "bad"
        self.good_manifest = BUILDER.build("good", self.good)
        self.bad_manifest = BUILDER.build("bad", self.bad)

    def test_reproducible_payload_and_digest_bind_every_file(self):
        second = self.root / "second"
        BUILDER.build("good", second)
        for output in (self.good, self.bad, second):
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            names = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
            self.assertEqual(names, set(manifest["files"]) | {"manifest.json"})
            for name, entry in manifest["files"].items():
                data = (output / name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"], name)
                self.assertEqual(len(data), entry["size"], name)
            canonical = json.dumps(manifest["files"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.assertEqual(hashlib.sha256(canonical).hexdigest(), manifest["artifact_sha256"])
        for path in self.good.rglob("*"):
            if path.is_file():
                self.assertEqual(path.read_bytes(), (second / path.relative_to(self.good)).read_bytes())

    def test_bad_release_changes_only_robots_and_visible_version(self):
        important = {"index.html", "catalog.html", "aura.html", "tihaya.html", "krug.html", "liniya.html"}
        self.assertEqual(set(self.bad_manifest["important_pages"]), important)
        self.assertEqual(set(self.good_manifest["files"]), set(self.bad_manifest["files"]))
        for name in self.good_manifest["files"]:
            good = (self.good / name).read_bytes()
            bad = (self.bad / name).read_bytes()
            if name.endswith(".html"):
                expected = good.replace(b"data-release-version>v1</b>", b"data-release-version>v2</b>")
                if name in important:
                    expected = expected.replace(b'content="index,follow"', b'content="noindex,follow"')
                self.assertEqual(expected, bad, name)
                self.assertEqual(Document(bad.decode("utf-8")).robots,
                                 "noindex,follow" if name in important else "index,follow")
            else:
                self.assertEqual(good, bad, name)
        self.assertEqual((self.good / "robots.txt").read_bytes(), b"User-agent: *\nAllow: /\n")
        self.assertNotEqual(self.good_manifest["artifact_sha256"], self.bad_manifest["artifact_sha256"])

    def test_static_content_and_relative_links_work_at_both_mounts(self):
        names = set(self.good_manifest["files"])
        titles, descriptions = set(), set()
        pages = {name: Document((self.good / name).read_text(encoding="utf-8"))
                 for name in names if name.endswith(".html")}
        self.assertEqual(len(pages), 8)
        for name, document in pages.items():
            self.assertEqual(document.h1_count, 1, name)
            self.assertTrue(document.title.strip(), name)
            self.assertTrue(document.description.strip(), name)
            self.assertNotIn(document.title, titles, name)
            self.assertNotIn(document.description, descriptions, name)
            titles.add(document.title)
            descriptions.add(document.description)
            text = (self.good / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\{\{[A-Z_]+\}\}")
            self.assertNotIn("<form", text)
            for mount in ("/", "/checked/"):
                for reference in document.references:
                    self.assertFalse(urlsplit(reference).scheme, reference)
                    self.assertFalse(reference.startswith("/"), reference)
                    resolved = urlsplit(urljoin("https://shop.example" + mount + name, reference))
                    self.assertEqual(resolved.netloc, "shop.example")
                    self.assertTrue(resolved.path.startswith(mount), reference)
                    self.assertIn(resolved.path[len(mount):], names, reference)
        visited, pending = set(), ["index.html"]
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            pending.extend(reference for reference in pages[name].references if reference in pages)
        self.assertEqual(visited, set(pages), "Every page must be discoverable from rendered HTML links")

    def test_builder_refuses_to_overwrite_an_artifact(self):
        before = (self.good / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            BUILDER.build("bad", self.good)
        self.assertEqual((self.good / "manifest.json").read_bytes(), before)
        with self.assertRaises(ValueError):
            BUILDER.build("unknown", self.root / "unknown")


if __name__ == "__main__":
    unittest.main()
