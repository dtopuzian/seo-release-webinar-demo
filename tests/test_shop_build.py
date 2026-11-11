"""Artifact contracts for the webinar landing pages; standard library only."""

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
        self.button_count = 0
        self.title = ""
        self.description = ""
        self.robots = ""
        self._in_title = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.h1_count += tag == "h1"
        self.button_count += tag == "button"
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


class LandingArtifactTests(unittest.TestCase):
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

    def test_both_landing_variants_change_only_robots_and_visible_version(self):
        for target in ("unsafe", "checked"):
            good, bad = self.root / target / "good", self.root / target / "bad"
            good_manifest = BUILDER.build("good", good, target)
            bad_manifest = BUILDER.build("bad", bad, target)
            self.assertEqual(bad_manifest["important_pages"], ["index.html"])
            self.assertEqual(set(good_manifest["files"]), set(bad_manifest["files"]))
            for name in good_manifest["files"]:
                expected = (good / name).read_bytes()
                if name == "index.html":
                    expected = expected.replace(b"data-release-version>v1</b>", b"data-release-version>v2</b>")
                    expected = expected.replace(b'content="index,follow"', b'content="noindex,follow"')
                    self.assertEqual(Document((bad / name).read_text(encoding="utf-8")).robots, "noindex,follow")
                self.assertEqual(expected, (bad / name).read_bytes(), (target, name))
            self.assertEqual((good / "robots.txt").read_bytes(), b"User-agent: *\nAllow: /\n")
            self.assertNotEqual(good_manifest["artifact_sha256"], bad_manifest["artifact_sha256"])

    def test_single_page_local_assets_at_both_mounts_and_distinct_sites(self):
        documents, digests = [], []
        for target, subtitle, image in (
            ("unsafe", "Это плохо, понятненько?", "mackey.png"),
            ("checked", "Щикарно", "cartman.png"),
        ):
            output = self.root / target
            manifest = BUILDER.build("good", output, target)
            names = set(manifest["files"])
            self.assertEqual({name for name in names if name.endswith(".html")}, {"index.html"})
            text = (output / "index.html").read_text(encoding="utf-8")
            document = Document(text)
            self.assertIn("SEO Checker", text)
            self.assertIn(f'<p class="subtitle">{subtitle}</p>', text)
            self.assertIn(f"assets/{image}", names)
            self.assertNotRegex(text, r"Кот на удалёнке|Гусь на связи|Позвать на созвон|Связаться с гусём")
            self.assertEqual(document.h1_count, 1)
            self.assertEqual(document.button_count, 1)
            self.assertTrue(document.title.strip())
            self.assertTrue(document.description.strip())
            self.assertEqual(document.robots, "index,follow")
            self.assertNotRegex(text, r"\{\{[A-Z_]+\}\}")
            self.assertNotIn("<form", text)
            self.assertIn('role="status"', text)
            for mount in ("/", "/checked/"):
                for reference in document.references:
                    self.assertFalse(urlsplit(reference).scheme, reference)
                    self.assertFalse(reference.startswith("/"), reference)
                    resolved = urlsplit(urljoin("https://shop.example" + mount, reference))
                    self.assertEqual(resolved.netloc, "shop.example")
                    self.assertTrue(resolved.path.startswith(mount), reference)
                    self.assertIn(resolved.path[len(mount):], names, reference)
            self.assertFalse({"cart.html", "product.html", "catalog.html"} & names)
            documents.append(text)
            digests.append(manifest["artifact_sha256"])
        self.assertNotEqual(*documents)
        self.assertNotEqual(*digests)

    def test_builder_refuses_to_overwrite_an_artifact(self):
        before = (self.good / "manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "never overwritten"):
            BUILDER.build("bad", self.good)
        self.assertEqual((self.good / "manifest.json").read_bytes(), before)
        with self.assertRaises(ValueError):
            BUILDER.build("unknown", self.root / "unknown")
        with self.assertRaises(ValueError):
            BUILDER.build("good", self.root / "unknown", "other")


if __name__ == "__main__":
    unittest.main()
