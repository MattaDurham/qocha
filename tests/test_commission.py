"""Commissioning: the survey, the tranche contract, and attached mode.

The load-bearing property under test is that completion is DERIVED. The
downstream consumer shows a setup step as done off these numbers, so a
survey that reported an intention rather than an artifact would let a
half-built vault claim it was finished.
"""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from qocha import commission_prompt, survey, unprocessed
from qocha.cli import main as cli_main
from qocha.commission import SURVEY_CAP
from qocha.scaffold import init_vault


def write(root, rel, text=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def page(source, slug="page", extra=""):
    return (f"---\ntitle: {slug}\ntype: source-summary\ntags: [cat/x]\n"
            f"created: 2026-08-28\nupdated: 2026-08-28\n"
            f'source: "[[{source}]]"\n{extra}---\n\n# {slug}\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qocha-commission-"))
        self.root = self.tmp / "vault"
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class AttachedInit(Base):
    def test_existing_files_are_never_moved_or_rewritten(self):
        original = write(self.root, "Report.pdf", "the original bytes")
        nested = write(self.root, "notes/thoughts.md", "# mine")
        init_vault(self.root, attached=True)
        self.assertEqual("the original bytes",
                         original.read_text(encoding="utf-8"))
        self.assertEqual("# mine", nested.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "wiki").is_dir())
        self.assertTrue((self.root / "logs").is_dir())

    def test_attached_makes_no_raw_dir_and_records_the_root_as_corpus(self):
        write(self.root, "a.pdf")
        init_vault(self.root, attached=True)
        self.assertFalse((self.root / "raw").exists())
        cfg = json.loads((self.root / "qocha.json").read_text(encoding="utf-8"))
        self.assertEqual(".", cfg["raw_dir"])

    def test_plain_init_still_makes_raw_and_says_so(self):
        init_vault(self.root)
        self.assertTrue((self.root / "raw").is_dir())
        cfg = json.loads((self.root / "qocha.json").read_text(encoding="utf-8"))
        self.assertEqual("raw", cfg["raw_dir"])

    def test_an_explicit_raw_dir_wins_in_attached_mode(self):
        # A corpus that already calls its sources sources/ keeps the name.
        write(self.root, "sources/a.pdf")
        init_vault(self.root, raw_dir="sources", attached=True)
        cfg = json.loads((self.root / "qocha.json").read_text(encoding="utf-8"))
        self.assertEqual("sources", cfg["raw_dir"])

    def test_init_never_overwrites_an_existing_schema(self):
        write(self.root, "CLAUDE.md", "my own rules")
        init_vault(self.root, attached=True)
        self.assertEqual("my own rules",
                         (self.root / "CLAUDE.md").read_text(encoding="utf-8"))


class SurveyShape(Base):
    def test_counts_sources_and_ignores_generated_surfaces(self):
        write(self.root, "a.pdf")
        write(self.root, "b.docx")
        write(self.root, "deep/c.md")
        init_vault(self.root, attached=True)      # adds wiki/, logs/, CLAUDE.md
        write(self.root, "wiki/generated.md", page("a.pdf"))
        facts = survey(self.root)
        self.assertEqual(3, facts["files"])
        self.assertTrue(facts["attached"])
        self.assertEqual(1, facts["layers"]["wiki"]["pages"])

    def test_machine_litter_is_excluded_but_reported(self):
        write(self.root, "a.pdf")
        write(self.root, ".DS_Store")
        write(self.root, "~$draft.docx")
        facts = survey(self.root, raw_dir=".")
        self.assertEqual(1, facts["files"])
        self.assertEqual(2, facts["junk"]["files"])

    def test_saved_page_mirrors_are_skipped_and_named(self):
        write(self.root, "article.html")
        for i in range(5):
            write(self.root, f"article_files/asset{i}.css")
        facts = survey(self.root, raw_dir=".")
        self.assertEqual(1, facts["files"])
        self.assertEqual({"article_files": 5}, facts["junk"]["mirrors"])

    def test_extension_table_and_extraction_cover_what_is_present(self):
        write(self.root, "a.pdf")
        write(self.root, "b.pdf")
        write(self.root, "c.md")
        facts = survey(self.root, raw_dir=".")
        self.assertEqual({"ext": ".pdf", "count": 2}, facts["extensions"][0])
        self.assertIn(".pdf", facts["extraction"])
        self.assertIn(".md", facts["extraction"])
        self.assertNotIn(".docx", facts["extraction"])

    def test_the_walk_is_capped_and_says_so(self):
        for i in range(12):
            write(self.root, f"f{i}.txt")
        facts = survey(self.root, raw_dir=".", cap=5)
        self.assertTrue(facts["truncated"])
        self.assertEqual(5, facts["files"])
        self.assertEqual(5, facts["cap"])

    def test_survey_refuses_a_path_that_is_not_a_directory(self):
        with self.assertRaises(ValueError):
            survey(self.root / "nope")

    def test_raw_mode_reads_only_the_corpus_dir(self):
        init_vault(self.root)
        write(self.root, "raw/a.pdf")
        write(self.root, "stray-at-root.pdf")
        facts = survey(self.root)
        self.assertEqual("raw", facts["raw_dir"])
        self.assertEqual(1, facts["files"])


class DerivedCompletion(Base):
    """Every completion signal is an artifact, never a claim."""

    def test_a_templated_schema_does_not_count_as_adapted(self):
        init_vault(self.root, attached=True)      # seeds {{params}}
        facts = survey(self.root)
        self.assertTrue(facts["layers"]["schema"])
        self.assertFalse(facts["layers"]["schema_adapted"])

    def test_a_filled_schema_counts_as_adapted(self):
        init_vault(self.root, attached=True)
        write(self.root, "CLAUDE.md", "# Vault\n\nReal rules, no params.\n")
        self.assertTrue(survey(self.root)["layers"]["schema_adapted"])

    def test_commission_records_are_listed_from_the_logs_dir(self):
        init_vault(self.root, attached=True)
        facts = survey(self.root)
        self.assertFalse(facts["layers"]["logs"]["commissioned"])
        write(self.root, "logs/2026-08-28-commission-01.md", "done")
        facts = survey(self.root)
        self.assertTrue(facts["layers"]["logs"]["commissioned"])
        self.assertEqual(["2026-08-28-commission-01.md"],
                         facts["layers"]["logs"]["commission_records"])

    def test_index_is_reported_only_once_the_db_exists(self):
        init_vault(self.root, attached=True)
        self.assertFalse(survey(self.root)["layers"]["index"])
        write(self.root, ".qocha/index.sqlite", "")
        self.assertTrue(survey(self.root)["layers"]["index"])


class Resumability(Base):
    def test_a_cited_source_is_processed_and_drops_out_of_the_tranche(self):
        write(self.root, "a.pdf")
        write(self.root, "b.pdf")
        init_vault(self.root, attached=True)
        self.assertEqual(["a.pdf", "b.pdf"], unprocessed(self.root))
        write(self.root, "wiki/a.md", page("a.pdf"))
        self.assertEqual(["b.pdf"], unprocessed(self.root))
        self.assertEqual(1, survey(self.root)["unprocessed"])

    def test_a_nested_source_is_matched_by_its_bare_filename(self):
        # The wiki cites source files by filename; the corpus keeps them
        # in subfolders. Both spellings must resolve or every nested file
        # would be re-processed on every pass.
        write(self.root, "deep/inner/c.pdf")
        init_vault(self.root, attached=True)
        write(self.root, "wiki/c.md", page("c.pdf"))
        self.assertEqual([], unprocessed(self.root))

    def test_pages_without_frontmatter_never_break_the_scan(self):
        write(self.root, "a.pdf")
        init_vault(self.root, attached=True)
        write(self.root, "wiki/loose.md", "just prose, no frontmatter")
        self.assertEqual(["a.pdf"], unprocessed(self.root))


class PromptContract(Base):
    def setUp(self):
        super().setUp()
        for i in range(5):
            write(self.root, f"doc{i}.pdf")
        init_vault(self.root, attached=True)

    def test_the_tranche_is_bounded_and_the_remainder_is_stated(self):
        text = commission_prompt(self.root, tranche=2)
        self.assertIn("doc0.pdf", text)
        self.assertIn("doc1.pdf", text)
        self.assertNotIn("doc2.pdf", text)
        self.assertIn("3 source files remain", text)

    def test_the_immutability_invariant_is_always_stated(self):
        text = commission_prompt(self.root)
        self.assertIn("IMMUTABLE", text)
        self.assertIn("Never create, rename,", text)

    def test_the_prompt_names_the_commission_log_the_survey_looks_for(self):
        # The completion signal and the instruction that produces it must
        # agree, or a perfect run would still read as unfinished.
        text = commission_prompt(self.root)
        self.assertRegex(text, r"logs/\d{4}-\d{2}-\d{2}-commission-01\.md")
        write(self.root, "logs/2026-01-01-commission-01.md", "x")
        self.assertTrue(
            survey(self.root)["layers"]["logs"]["commissioned"])

    def test_extraction_commands_cover_the_filetypes_present(self):
        self.assertIn("pdftotext", commission_prompt(self.root))

    def test_an_unadapted_schema_makes_adaptation_task_one(self):
        self.assertIn("adapting", commission_prompt(self.root).lower())

    def test_a_tranche_below_one_is_clamped_not_empty(self):
        self.assertIn("doc0.pdf", commission_prompt(self.root, tranche=0))

    def test_a_fully_processed_corpus_still_produces_a_valid_prompt(self):
        for i in range(5):
            write(self.root, f"wiki/doc{i}.md", page(f"doc{i}.pdf"))
        text = commission_prompt(self.root)
        self.assertIn("(none)", text)
        self.assertIn("0 source files remain", text)

    def test_attached_and_raw_vaults_name_their_own_corpus(self):
        self.assertIn("the vault root itself",
                      commission_prompt(self.root))
        other = self.tmp / "raw-vault"
        other.mkdir()
        init_vault(other)
        write(other, "raw/x.pdf")
        self.assertIn("raw/", commission_prompt(other))


class CliSurface(Base):
    """The CLI writes to stdout by design; these drive it, not read it."""

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli_main(argv)
        return code, out.getvalue()

    def test_init_attached_is_reachable_from_the_command_line(self):
        write(self.root, "a.pdf")
        code, _ = self.run_cli(["init", str(self.root), "--attached"])
        self.assertEqual(0, code)
        cfg = json.loads((self.root / "qocha.json").read_text(encoding="utf-8"))
        self.assertEqual(".", cfg["raw_dir"])
        self.assertFalse((self.root / "raw").exists())

    def test_survey_and_commission_commands_run(self):
        write(self.root, "a.pdf")
        self.run_cli(["init", str(self.root), "--attached"])
        code, text = self.run_cli(["survey", str(self.root), "--json"])
        self.assertEqual(0, code)
        self.assertEqual(1, json.loads(text)["files"])
        self.assertEqual(0, self.run_cli(["survey", str(self.root)])[0])
        code, text = self.run_cli(["commission", str(self.root),
                                   "--tranche", "1"])
        self.assertEqual(0, code)
        self.assertIn("a.pdf", text)

    def test_lint_follows_the_recorded_corpus_dir(self):
        # Without the config lookup this lints an attached vault against a
        # raw/ that deliberately does not exist, and every source edge
        # reads as an orphan.
        write(self.root, "a.pdf")
        self.run_cli(["init", str(self.root), "--attached"])
        write(self.root, "wiki/a.md", page("a.pdf"))
        self.assertEqual(0, self.run_cli(["lint", str(self.root)])[0])
        self.assertEqual(0, self.run_cli(["preflight", str(self.root)])[0])


class DefaultCap(unittest.TestCase):
    def test_the_shipped_cap_is_bounded(self):
        self.assertGreater(SURVEY_CAP, 0)


if __name__ == "__main__":
    unittest.main()
