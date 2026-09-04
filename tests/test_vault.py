import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from qocha import Config, Vault
from qocha.vault import NOTE_CAP


class FakeEmbedder:
    """Deterministic embeddings: a tiny keyword space so semantic tests
    are stable offline. Vectors are unit-normalized over a fixed vocab."""

    VOCAB = ("comet", "orbit", "bread", "oven", "meeting")

    def __init__(self):
        self.down = False

    def _vec(self, text):
        text = text.lower()
        v = np.array([float(text.count(w)) for w in self.VOCAB],
                     dtype="float32")
        n = np.linalg.norm(v)
        return v / n if n else v + 1e-6

    def embed_documents(self, texts):
        if self.down:
            return None
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        if self.down:
            return None
        return self._vec(text)


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qocha-test-"))
        self.root = self.tmp / "vault"
        self.root.mkdir()
        write(self.root, "wiki/comets.md",
              "# Comets\nA comet has an orbit around the sun. "
              "Comet tails point away from the sun.")
        write(self.root, "wiki/baking.md",
              "# Baking\nBread goes in the oven at high heat. "
              "Steam makes the crust.")
        write(self.root, "notes.md",
              "# Notes\nA standing meeting about nothing in particular.")
        self.embedder = FakeEmbedder()
        self.vault = Vault(self.root, embedder=self.embedder,
                           answerer=lambda p: "unused")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_counts_and_status(self):
        out = self.vault.scan()
        self.assertEqual(out["seen"], 3)
        self.assertEqual(out["changed"], 3)
        st = self.vault.status()
        self.assertEqual(st["notes"], 3)
        self.assertGreaterEqual(st["chunks"], 3)
        self.assertTrue(st["available"])

    def test_rescan_is_incremental(self):
        self.vault.scan()
        out = self.vault.scan()
        self.assertEqual(out["changed"], 0)

    def test_modify_and_remove(self):
        self.vault.scan()
        time.sleep(0.01)
        write(self.root, "wiki/comets.md",
              "# Comets\nRewritten: perihelion is the closest approach.")
        (self.root / "wiki" / "baking.md").unlink()
        out = self.vault.scan()
        self.assertEqual(out["changed"], 1)
        self.assertEqual(out["removed"], 1)
        hits = self.vault.search("perihelion")
        self.assertTrue(hits and hits[0]["path"] == "wiki/comets.md")
        self.assertFalse(self.vault.search("crust"))

    def test_fts_search_without_vectors(self):
        self.vault.scan()
        hits = self.vault.search("oven crust")
        self.assertEqual(hits[0]["path"], "wiki/baking.md")

    def test_hybrid_search_semantic_recall(self):
        self.vault.index()
        st = self.vault.status()
        self.assertEqual(st["vectors"], st["chunks"])
        # "orbit" appears only in the comets note; the fake embedding
        # space puts a comet query nearest that chunk too
        hits = self.vault.search("orbit")
        self.assertEqual(hits[0]["path"], "wiki/comets.md")

    def test_vector_rank_semantic(self):
        self.vault.index()
        con = self.vault._connect()
        try:
            ids = self.vault._rank_vec(con, "comet orbit", floor=0.1)
            self.assertTrue(ids)
            row = con.execute("SELECT path FROM chunks WHERE id=?",
                              (ids[0],)).fetchone()
            self.assertEqual(row["path"], "wiki/comets.md")
        finally:
            con.close()

    def test_embedder_down_degrades(self):
        self.vault.scan()
        self.embedder.down = True
        self.assertEqual(self.vault.embed_pending(), 0)
        hits = self.vault.search("bread")     # FTS still works
        self.assertEqual(hits[0]["path"], "wiki/baking.md")

    def test_excluded_dirs_skipped(self):
        write(self.root, ".obsidian/workspace.md", "# Hidden\nnever index")
        write(self.root, ".trash/old.md", "# Old\nnever index")
        out = self.vault.scan()
        self.assertEqual(out["seen"], 3)

    def test_explicit_dirs_mode(self):
        write(self.root, "Archive/deep.md", "# Deep\narchived text")
        cfg = Config(root=self.root, dirs=["wiki"],
                     db=self.tmp / "scoped.sqlite")
        scoped = Vault(self.root, config=cfg, embedder=self.embedder,
                       answerer=lambda p: "unused")
        out = scoped.scan()
        # root *.md + wiki/, but not Archive/
        self.assertEqual(out["seen"], 3)
        self.assertFalse(scoped.search("archived"))

    def test_a_listed_dir_still_skips_excluded_and_dot_dirs(self):
        # Until v0.4.3 the explicit-dirs branch applied NEITHER filter, so
        # listing a directory dragged in every vendored .venv package under
        # it and there was no way to take a tree while skipping one branch.
        write(self.root, "raw/talk.md", "# Talk\nfull transcript here")
        write(self.root, "raw/clips/one.md", "# Clip\nclipping one")
        write(self.root, "raw/.venv/lib/LICENSE.md", "# Vendored\nlicense")
        cfg = Config(root=self.root, dirs=["raw"],
                     exclude_dirs={"clips"},
                     db=self.tmp / "filtered.sqlite")
        scoped = Vault(self.root, config=cfg, embedder=self.embedder,
                       answerer=lambda p: "unused")
        scoped.scan()
        paths = {h["path"] for h in scoped.search("transcript")}
        self.assertIn("raw/talk.md", paths)
        self.assertFalse(scoped.search("clipping"))
        self.assertFalse(scoped.search("Vendored"))

    def test_note_text_and_traversal_guard(self):
        self.vault.scan()
        self.assertIn("Comets", self.vault.note_text("wiki/comets.md"))
        for bad in ("../outside.md", "wiki/../../etc/passwd.md",
                    "wiki/comets.txt"):
            with self.assertRaises(ValueError):
                self.vault.note_text(bad)

    def test_a_long_note_is_served_whole_by_default(self):
        # The regression: a 97k-char transcript was served at 41% with no
        # signal, which reads as a broken ingest of a complete file.
        big = "x" * (NOTE_CAP * 3) + "\nTHE-END\n"
        (self.root / "wiki" / "long.md").write_text(big, encoding="utf-8")
        out = self.vault.note_text("wiki/long.md")
        self.assertEqual(len(out), len(big))
        self.assertTrue(out.endswith("THE-END\n"))

    def test_an_explicit_cap_truncates_but_says_so_in_band(self):
        big = "x" * 5_000
        (self.root / "wiki" / "long.md").write_text(big, encoding="utf-8")
        out = self.vault.note_text("wiki/long.md", cap=1_000)
        self.assertTrue(out.startswith("x" * 1_000))
        # the marker must name BOTH numbers -- a bare "truncated" leaves a
        # model unable to tell whether it is missing 1% or 99%
        self.assertIn("1,000", out)
        self.assertIn("5,000", out)
        self.assertIn("FRAGMENT", out)

    def test_a_cap_the_note_fits_under_adds_no_marker(self):
        (self.root / "wiki" / "short.md").write_text("hi", encoding="utf-8")
        self.assertEqual(self.vault.note_text("wiki/short.md", cap=999), "hi")

    def test_paths_are_posix_on_every_platform(self):
        # Vault paths are logical identifiers — citations validate against
        # them, wikilinks resolve through them — so they are "/"-joined
        # whatever the host separator (the Windows CI regression,
        # 2026-07-21: stored 'wiki\\note.md' broke citation validation).
        self.vault.scan()
        hits = self.vault.search("comets", limit=3)
        self.assertTrue(hits)
        for h in hits:
            self.assertNotIn("\\", h["path"])
        root = self.vault.config.root
        self.assertEqual(self.vault._rel(root / "wiki" / "comets.md"),
                         "wiki/comets.md")

    def test_db_sidecar_location(self):
        self.vault.scan()
        self.assertTrue((self.root / ".qocha" / "index.sqlite").exists())
        # and the sidecar itself is never indexed
        self.assertEqual(self.vault.scan()["seen"], 3)


class AskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qocha-ask-"))
        self.root = self.tmp / "vault"
        self.root.mkdir()
        write(self.root, "wiki/comets.md",
              "# Comets\nA comet has an orbit. Halley returns every 76 "
              "years.")
        write(self.root, "wiki/baking.md",
              "# Baking\nBread goes in the oven.")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _vault(self, answer):
        v = Vault(self.root, embedder=None, answerer=lambda p: answer)
        v.scan()
        return v

    def test_citations_validated_against_hits(self):
        v = self._vault("Halley returns every 76 years [[comets]]. "
                        "Also trust me on this [[not-a-real-note]].")
        out = v.ask("How often does Halley return?")
        refs = [c["ref"] for c in out["citations"]]
        self.assertEqual(refs, ["comets"])
        self.assertEqual(out["citations"][0]["path"], "wiki/comets.md")

    def test_citation_spellings_accepted(self):
        for spelling in ("comets", "comets.md", "wiki/comets",
                         "wiki/comets.md", "Wiki/Comets"):
            v = self._vault(f"An answer [[{spelling}]].")
            out = v.ask("comet orbit")
            self.assertEqual(out["citations"][0]["path"], "wiki/comets.md",
                             spelling)

    def test_no_hits_short_circuits(self):
        calls = []
        v = Vault(self.root, embedder=None,
                  answerer=lambda p: calls.append(p) or "x")
        v.scan()
        out = v.ask("zzzznothing matches this")
        self.assertEqual(out["citations"], [])
        self.assertEqual(calls, [], "answerer must not be called on no hits")

    def test_prompt_carries_owner_and_chunks(self):
        prompts = []
        v = Vault(self.root, embedder=None, owner="Ada",
                  answerer=lambda p: prompts.append(p) or "fine [[comets]]")
        v.scan()
        v.ask("comet orbit")
        self.assertIn("Ada's personal research assistant", prompts[0])
        self.assertIn("wiki/comets.md", prompts[0])


class ConfigTests(unittest.TestCase):
    def test_json_and_overrides(self):
        tmp = Path(tempfile.mkdtemp(prefix="qocha-cfg-"))
        try:
            (tmp / "qocha.json").write_text(
                '{"owner": "Ada", "answer_model": "opus", "dirs": ["wiki"]}')
            cfg = Config.load(tmp, answer_model="haiku")
            self.assertEqual(cfg.owner, "Ada")
            self.assertEqual(cfg.answer_model, "haiku")   # override wins
            self.assertEqual(cfg.dirs, ["wiki"])
            self.assertEqual(cfg.db, tmp.resolve() / ".qocha"
                             / "index.sqlite")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_json_ignored(self):
        tmp = Path(tempfile.mkdtemp(prefix="qocha-cfg-"))
        try:
            (tmp / "qocha.json").write_text("{not json")
            cfg = Config.load(tmp)
            self.assertEqual(cfg.owner, "the owner")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
