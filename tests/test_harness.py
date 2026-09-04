import shutil
import tempfile
import unittest
from pathlib import Path

from qocha.lint import lint_vault, preflight
from qocha.scaffold import init_vault

DEMO = Path(__file__).resolve().parent.parent / "demo" / "vault"

GOOD_SUMMARY = """---
title: A fine source
type: source-summary
source_type: memo
tags: [alpha, beta]
created: 2026-01-01
updated: 2026-01-01
source: "[[memo.txt]]"
---

# A fine source

**Gist:** fine.
"""


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class LintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qocha-lint-"))
        self.vault = self.tmp / "vault"
        (self.vault / "raw").mkdir(parents=True)
        (self.vault / "wiki").mkdir()
        write(self.vault, "raw/memo.txt", "the memo body")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def msgs(self, **kw):
        return [m for _, m in lint_vault(self.vault, **kw)]

    def test_clean_vault_passes(self):
        write(self.vault, "wiki/a-fine-source.md", GOOD_SUMMARY)
        self.assertEqual(self.msgs(), [])

    def test_missing_frontmatter_and_keys(self):
        write(self.vault, "wiki/bare.md", "# Bare\nno frontmatter")
        write(self.vault, "wiki/thin.md",
              "---\ntitle: Thin\ntype: concept\n---\n# Thin\nbody")
        msgs = self.msgs()
        self.assertIn("no frontmatter block", msgs)
        self.assertTrue(any("missing frontmatter key tags" in m
                            for m in msgs))
        self.assertTrue(any("missing frontmatter key created" in m
                            for m in msgs))

    def test_meta_types_exempt_from_tags(self):
        write(self.vault, "wiki/build.md",
              "---\ntitle: B\ntype: build-log\ncreated: 2026-01-01\n"
              "updated: 2026-01-01\n---\n# B\nbody")
        self.assertEqual(self.msgs(), [])

    def test_dangling_source_edge(self):
        write(self.vault, "wiki/gone.md",
              GOOD_SUMMARY.replace("memo.txt", "vanished.txt")
              .replace("a-fine-source", "gone"))
        msgs = self.msgs()
        self.assertTrue(any("source target not found" in m for m in msgs))

    def test_roundup_and_orphan_exemptions(self):
        roundup = ("---\ntitle: R\ntype: source-summary\n"
                   "source_type: catalog\ntags: [alpha]\n"
                   "created: 2026-01-01\nupdated: 2026-01-01\n"
                   "source_count: 3\n---\n# R\n- [[memo.txt]]\n")
        orphan = ("---\ntitle: O\ntype: source-summary\n"
                  "source_type: memo\ntags: [alpha]\n"
                  "created: 2026-01-01\nupdated: 2026-01-01\n"
                  "source_count: 0\n---\n# O\n> Note: source removed\n")
        bad = ("---\ntitle: X\ntype: source-summary\nsource_type: memo\n"
               "tags: [alpha]\ncreated: 2026-01-01\nupdated: 2026-01-01\n"
               "---\n# X\nno source line\n")
        write(self.vault, "wiki/roundup.md", roundup)
        write(self.vault, "wiki/orphan.md", orphan)
        write(self.vault, "wiki/bad.md", bad)
        msgs = self.msgs()
        self.assertEqual(len([m for m in msgs if "source: missing" in m]),
                         1)

    def test_unquoted_frontmatter_wikilink(self):
        write(self.vault, "wiki/uq.md",
              "---\ntitle: U\ntype: topic\ntags: [alpha]\n"
              "created: 2026-01-01\nupdated: 2026-01-01\n"
              "sources:\n  - [[a-fine-source]]\n---\n# U\nbody")
        write(self.vault, "wiki/a-fine-source.md", GOOD_SUMMARY)
        msgs = self.msgs()
        self.assertTrue(any("unquoted frontmatter wikilink" in m
                            for m in msgs))

    def test_unresolved_wikilink_and_allow_flag(self):
        write(self.vault, "wiki/linky.md",
              "---\ntitle: L\ntype: topic\ntags: [alpha]\n"
              "created: 2026-01-01\nupdated: 2026-01-01\n---\n"
              "# L\nsee [[no-such-page]]")
        msgs = self.msgs()
        self.assertTrue(any("unresolved wikilink [[no-such-page]]" in m
                            for m in msgs))
        self.assertEqual(self.msgs(allow_unresolved=True), [])

    def test_links_resolve_across_vault_and_raw(self):
        write(self.vault, "Sessions/2026-01-05-retro.md", "# Retro\nbody")
        write(self.vault, "wiki/linky.md",
              "---\ntitle: L\ntype: topic\ntags: [alpha]\n"
              "created: 2026-01-01\nupdated: 2026-01-01\n---\n# L\n"
              "see [[2026-01-05-retro]] and [[Sessions/2026-01-05-retro]]"
              " and [[memo]] and [[index]]\n")
        write(self.vault, "index.md", "# Index\n")
        self.assertEqual(self.msgs(), [])

    def test_duplicate_slug(self):
        write(self.vault, "wiki/a/dupe.md",
              "---\ntitle: D\ntype: topic\ntags: [a]\n"
              "created: 2026-01-01\nupdated: 2026-01-01\n---\n# D\n")
        write(self.vault, "wiki/b/dupe.md",
              "---\ntitle: D\ntype: topic\ntags: [a]\n"
              "created: 2026-01-01\nupdated: 2026-01-01\n---\n# D\n")
        msgs = self.msgs()
        self.assertTrue(any("duplicate slug 'dupe'" in m for m in msgs))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qocha-pf-"))
        self.vault = self.tmp / "vault"
        (self.vault / "raw").mkdir(parents=True)
        (self.vault / "wiki").mkdir()
        write(self.vault, "raw/memo.txt", "body")
        write(self.vault, "wiki/a-fine-source.md", GOOD_SUMMARY)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean(self):
        out = preflight(self.vault)
        self.assertEqual(out, {"orphans": [], "pending": []})

    def test_orphan_detected(self):
        (self.vault / "raw" / "memo.txt").unlink()
        out = preflight(self.vault)
        self.assertEqual(out["orphans"],
                         [("wiki/a-fine-source.md", "memo.txt")])

    def test_path_qualified_source_edges_resolve(self):
        # A vault that writes `source:` with its raw/ directory (the form
        # TC-IL adopted 2026-08-11) must not read as 7,640 orphans.
        write(self.vault, "raw/family/note.md", "body")
        for i, target in enumerate(["raw/family/note.md",
                                    "raw/family/note",
                                    "raw/memo.txt",
                                    "raw/family"]):
            write(self.vault, f"wiki/qualified-{i}.md",
                  GOOD_SUMMARY.replace("[[memo.txt]]", f"[[{target}]]"))
        out = preflight(self.vault)
        self.assertEqual(out, {"orphans": [], "pending": []})
        problems = [m for _, m in lint_vault(self.vault)
                    if "source target not found" in m]
        self.assertEqual(problems, [])

    def test_path_qualified_orphan_still_detected(self):
        write(self.vault, "wiki/gone.md",
              GOOD_SUMMARY.replace("[[memo.txt]]", "[[raw/family/gone.md]]"))
        out = preflight(self.vault)
        self.assertEqual(out["orphans"],
                         [("wiki/gone.md", "raw/family/gone.md")])

    def test_pending_is_info_not_orphan(self):
        pending = self.vault / "pending-user-deletion"
        pending.mkdir()
        (self.vault / "raw" / "memo.txt").rename(pending / "memo.txt")
        out = preflight(self.vault)
        self.assertEqual(out["orphans"], [])
        self.assertEqual(out["pending"],
                         [("wiki/a-fine-source.md", "memo.txt")])


class InitTests(unittest.TestCase):
    def test_seeds_and_never_clobbers(self):
        tmp = Path(tempfile.mkdtemp(prefix="qocha-init-"))
        try:
            vault = tmp / "notes"
            vault.mkdir()
            created = init_vault(vault, name="Notes")
            self.assertIn("CLAUDE.md", created)
            self.assertIn("raw/", created)
            self.assertIn("wiki/", created)
            self.assertIn("qocha.json", created)
            schema = (vault / "CLAUDE.md").read_text()
            self.assertIn("Notes — Schema", schema)
            self.assertIn("{{CORPUS_DESCRIPTION", schema)
            (vault / "CLAUDE.md").write_text("owner edited")
            self.assertEqual(init_vault(vault, name="Notes"), [])
            self.assertEqual((vault / "CLAUDE.md").read_text(),
                             "owner edited")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DemoVaultTests(unittest.TestCase):
    """The bundled demo vault is the pattern's exemplar — it must pass
    its own machinery."""

    def test_demo_lints_clean(self):
        self.assertEqual(lint_vault(DEMO), [])

    def test_demo_preflight_clean(self):
        out = preflight(DEMO)
        self.assertEqual(out["orphans"], [])


if __name__ == "__main__":
    unittest.main()
