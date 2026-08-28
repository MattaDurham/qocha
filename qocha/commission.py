"""Commissioning — turning an unfamiliar folder into a working vault.

`qocha init` makes a folder *shaped* like a vault in a few milliseconds.
It cannot make it *useful*: the pile of PDFs, exports and clippings that
was already there is still opaque, and no amount of scaffolding reads
it. That second step needs a model, and this module is the deterministic
half of it — everything that can be known by walking the disk, so the
model is asked only for the part that genuinely needs judgment.

Two functions, and the split between them is the whole design:

- `survey()` answers "what is actually here, and which layers exist
  already?" with no model, no network, and no writes. It is also the
  progress signal: every layer it reports is an artifact on disk, so
  completion is observed rather than claimed. A flag saying a
  commissioning run succeeded would go stale the moment someone deleted
  the wiki; a count of wiki pages cannot.
- `commission_prompt()` composes the survey into the working contract
  for one bounded tranche, following harness/ingest-agent-prompt.md.

The prompt is deliberately RESUMABLE rather than exhaustive. A folder of
four thousand documents cannot be commissioned in one pass, and a run
that tries produces a truncated wiki and no record of what it missed.
`unprocessed()` derives the next tranche from the wiki itself — a source
file is unprocessed when no source-summary page cites it — so running
the same prompt again picks up exactly where the last one stopped, with
no cursor to keep in sync and nothing to corrupt if a run dies halfway.
"""
import os
import re
from datetime import date
from pathlib import Path

from .config import EXCLUDE_DIRS, Config

# A bounded walk. A commissioning survey runs on a user's click, so it
# must not wander a home directory for a minute; past the cap the survey
# reports `truncated` and the count it did reach, never a quiet round
# number that reads like the whole corpus.
SURVEY_CAP = 5000

# Generated surfaces — qocha's own output, never source material.
GENERATED_DIRS = {"wiki", "logs", "pending-user-deletion"}
GENERATED_FILES = {"CLAUDE.md", "index.md", "log.md", "qocha.json"}

# Machine litter. Excluded from the source count because nobody means
# these when they say "my files", but counted and reported — a number
# that silently shrank is worse than one that is explained.
JUNK_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".localized"}
JUNK_SUFFIXES = (".tmp", ".part", ".crdownload", ".swp")

# Saved-webpage mirrors: one `page_files/` beside `page.html` holds
# hundreds of css/js/font assets. Real content, technically; junk as
# sources, and enough of it to swamp the count of what matters.
_MIRROR_DIR = re.compile(r".+_files$")

_FM_BLOCK = re.compile(r"^---\n(.*?)\n---\n", re.S)
_SOURCE_LINE = re.compile(r'^source:\s*"\[\[(.+?)\]\]"\s*$', re.M)
# The unfilled {{params}} the schema starter ships with. Their absence is
# what "the vault has rules of its own" means, and it is checkable.
_UNFILLED = re.compile(r"\{\{.+?\}\}", re.S)

# One extraction command per filetype present — the adaptation
# checklist's extraction table, prefilled for what the corpus actually
# holds. Anything not listed is read directly or described by a
# multimodal model.
EXTRACTION = {
    ".pdf": "pdftotext -layout <file> -",
    ".docx": "textutil -convert txt -stdout <file>  (macOS) "
             "or pandoc -t plain <file>",
    ".doc": "textutil -convert txt -stdout <file>",
    ".rtf": "textutil -convert txt -stdout <file>",
    ".pptx": "unzip -p <file> 'ppt/slides/slide*.xml' | "
             "sed -e 's/<[^>]*>/ /g'",
    ".xlsx": "python -c \"import openpyxl\" if available, else describe "
             "the sheet names and skip the cells",
    ".epub": "pandoc -t plain <file>",
    ".html": "strip tags, or read directly if small",
    ".htm": "strip tags, or read directly if small",
    ".eml": "read directly; the headers are part of the content",
    ".vtt": "read directly and drop the timestamps",
    ".srt": "read directly and drop the timestamps",
}
_READ_DIRECTLY = {".md", ".markdown", ".txt", ".text", ".csv", ".tsv",
                  ".json", ".yaml", ".yml", ".rst", ".org"}
_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tiff",
          ".bmp", ".svg"}


def _is_junk(name):
    return (name in JUNK_NAMES or name.startswith("~$")
            or name.lower().endswith(JUNK_SUFFIXES))


def _skip_dir(name, at_corpus_root):
    """Directories the walk never descends into."""
    if name in EXCLUDE_DIRS or name.startswith("."):
        return True
    # wiki/ and logs/ are only generated surfaces at the vault root; a
    # corpus may legitimately contain a folder called "logs".
    return at_corpus_root and name in GENERATED_DIRS


def corpus_root(root, raw_dir=None):
    """Where the source files live: the vault root for an attached
    folder, `<root>/<raw_dir>` otherwise."""
    root = Path(root).expanduser().resolve()
    if raw_dir is None:
        raw_dir = Config.load(root).raw_dir
    raw_dir = (raw_dir or "raw").strip()
    return (root if raw_dir in (".", "") else root / raw_dir), raw_dir


def _walk(root, raw_dir=None, cap=SURVEY_CAP):
    """Walk the corpus once. Returns (files, junk, mirrors, truncated).

    `files` are corpus-relative POSIX paths — the vault's stored paths
    are logical identifiers on every platform, so the host separator
    must not leak in here either.
    """
    base, _ = corpus_root(root, raw_dir)
    files, junk, mirrors = [], [], {}
    truncated = False
    if not base.is_dir():
        return files, junk, mirrors, truncated
    for dirpath, dirnames, filenames in os.walk(base):
        here = Path(dirpath)
        at_corpus_root = here == base
        dirnames[:] = sorted(
            d for d in dirnames if not _skip_dir(d, at_corpus_root))
        # Count a saved-page mirror, then refuse to descend into it.
        for d in list(dirnames):
            if _MIRROR_DIR.match(d):
                dirnames.remove(d)
                n = sum(len(f) for _, _, f in os.walk(here / d))
                mirrors[str((here / d).relative_to(base).as_posix())] = n
        for name in sorted(filenames):
            if at_corpus_root and name in GENERATED_FILES:
                continue
            rel = (here / name).relative_to(base).as_posix()
            if _is_junk(name):
                junk.append(rel)
                continue
            if len(files) >= cap:
                truncated = True
                return files, junk, mirrors, truncated
            files.append(rel)
    return files, junk, mirrors, truncated


def _cited_sources(root):
    """Every raw file already cited by a wiki source-summary page.

    This is the forward-only ingest state the harness defines: no
    cursor, no manifest, just the wiki saying what it has covered. It
    survives a crashed run, a hand-written page, and a deleted index.
    """
    wiki = Path(root).expanduser().resolve() / "wiki"
    cited = set()
    if not wiki.is_dir():
        return cited
    for page in wiki.rglob("*.md"):
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _FM_BLOCK.match(text)
        if not fm:
            continue
        for m in _SOURCE_LINE.finditer(fm.group(1)):
            target = m.group(1).replace('\\"', '"').strip()
            cited.add(target)
            cited.add(Path(target).name)
    return cited


def unprocessed(root, raw_dir=None, cap=SURVEY_CAP):
    """Corpus files no wiki source-summary cites yet, in walk order."""
    files, _, _, _ = _walk(root, raw_dir, cap)
    cited = _cited_sources(root)
    return [f for f in files
            if f not in cited and Path(f).name not in cited]


def _extension_table(files):
    counts = {}
    for f in files:
        counts[Path(f).suffix.lower() or "(no extension)"] = counts.get(
            Path(f).suffix.lower() or "(no extension)", 0) + 1
    return [{"ext": e, "count": n}
            for e, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _extraction_for(extensions):
    """The extraction table, narrowed to the filetypes actually present."""
    out = {}
    for row in extensions:
        ext = row["ext"]
        if ext in EXTRACTION:
            out[ext] = EXTRACTION[ext]
        elif ext in _READ_DIRECTLY:
            out[ext] = "read the file directly"
        elif ext in _IMAGE:
            out[ext] = "read the image directly and describe what it shows"
    return out


def survey(root, raw_dir=None, cap=SURVEY_CAP):
    """What is on disk right now: the corpus, and which layers exist.

    Deterministic, read-only, and never raises on an awkward tree — an
    unreadable subdirectory is a fact about the corpus, not a reason to
    fail the survey that was going to report it.
    """
    vault = Path(root).expanduser().resolve()
    if not vault.is_dir():
        raise ValueError(f"{vault} is not a directory")
    cfg = Config.load(vault)
    base, raw_name = corpus_root(vault, raw_dir if raw_dir is not None
                                 else cfg.raw_dir)
    files, junk, mirrors, truncated = _walk(vault, raw_name, cap)
    extensions = _extension_table(files)

    schema = vault / "CLAUDE.md"
    schema_text = ""
    if schema.is_file():
        try:
            schema_text = schema.read_text(encoding="utf-8", errors="replace")
        except OSError:
            schema_text = ""
    wiki_pages = sorted((vault / "wiki").rglob("*.md")) \
        if (vault / "wiki").is_dir() else []
    try:
        commission_logs = sorted(
            p.name for p in (vault / "logs").glob("*-commission*.md"))
    except OSError:
        commission_logs = []
    cited = _cited_sources(vault)
    todo = [f for f in files
            if f not in cited and Path(f).name not in cited]

    return {
        "root": str(vault),
        "raw_dir": raw_name,
        "attached": raw_name in (".", ""),
        "files": len(files),
        "unprocessed": len(todo),
        "cap": cap,
        "truncated": truncated,
        "extensions": extensions,
        "extraction": _extraction_for(extensions),
        "junk": {"files": len(junk), "mirrors": mirrors},
        "layers": {
            # The schema exists AND says something vault-specific. A
            # starter still full of {{params}} is a file, not a contract.
            "schema": schema.is_file(),
            "schema_adapted": bool(
                schema_text and not _UNFILLED.search(schema_text)),
            "wiki": {"pages": len(wiki_pages),
                     "sources_cited": len(
                         {c for c in cited if "." in c})},
            "logs": {"commission_records": commission_logs,
                     "commissioned": bool(commission_logs)},
            "index": bool(cfg.db and Path(cfg.db).exists()),
            "raw_dir_exists": base.is_dir(),
        },
    }


def _inventory_block(facts):
    lines = []
    ext = ", ".join(f"{r['count']} {r['ext']}" for r in facts["extensions"][:12])
    lines.append(f"- {facts['files']} source files"
                 + (f" (survey capped at {facts['cap']}; more remain beyond it)"
                    if facts["truncated"] else "")
                 + (f": {ext}" if ext else ""))
    lines.append(f"- {facts['unprocessed']} of them have no wiki page yet")
    lines.append(f"- {facts['layers']['wiki']['pages']} wiki pages exist")
    if facts["junk"]["files"]:
        lines.append(f"- {facts['junk']['files']} machine-litter files "
                     "(.DS_Store and similar) — already excluded, ignore them")
    for path, n in sorted(facts["junk"]["mirrors"].items()):
        lines.append(f"- `{path}/` is a saved-webpage mirror ({n} assets) — "
                     "SKIP it; summarize the sibling .html page instead")
    return "\n".join(lines)


def commission_prompt(root, tranche=40, raw_dir=None, cap=SURVEY_CAP):
    """The working contract for one bounded commissioning pass.

    Self-contained on purpose: it is handed to a session that has no
    other context about this vault, so every rule it must follow is
    stated here rather than assumed from a file it may never open.
    """
    tranche = max(1, int(tranche))
    facts = survey(root, raw_dir=raw_dir, cap=cap)
    vault = facts["root"]
    todo = unprocessed(root, facts["raw_dir"], cap)[:tranche]
    remaining = max(0, facts["unprocessed"] - len(todo))
    today = date.today().isoformat()
    corpus = ("the vault root itself — the files that were here before "
              "qocha" if facts["attached"] else f"{facts['raw_dir']}/")

    extraction = "\n".join(f"  {ext}  ->  {cmd}"
                           for ext, cmd in sorted(facts["extraction"].items()))
    listing = "\n".join(f"  {f}" for f in todo) or "  (none)"
    schema_state = ("The schema still carries unfilled {{params}} — adapting "
                    "it is task 1." if not facts["layers"]["schema_adapted"]
                    else "The schema is already adapted; extend it only where "
                         "this tranche teaches you something new.")

    return f"""You are commissioning a knowledge vault at:

  {vault}

Commissioning means turning a folder of raw source files into a linked,
searchable knowledge system: a schema the vault runs on, one page per
source, the concepts and entities those sources are about, and an index
that retrieval can reach. Work only inside the vault.

THE ONE INVARIANT

The source corpus is IMMUTABLE. It lives in:

  {corpus}

Never create, rename, move, edit, or delete a source file — not to tidy
them, not to sort them into folders, not to fix an extension. Everything
you write goes to `wiki/`, `logs/`, `index.md`, `log.md`, or
`CLAUDE.md`. If you believe a source must change, write that in the log
instead and leave the file alone.

WHAT IS HERE NOW

{_inventory_block(facts)}

{schema_state}

YOUR TRANCHE — {len(todo)} files, and only these

{listing}

This pass is deliberately bounded: {remaining} source files remain after
it, and they are the next pass's job, not yours. Do not widen the scope
to finish the corpus. A complete tranche that reports what it left is
worth more than a rushed pass over everything.

EXTRACTION

{extraction or "  read each file directly"}

If a file will not extract, still write its page with whatever is
knowable (filename, size, type, any readable fragment), note
"Extraction thin:" in the body with the reason, and list it under
blockers in your log. Never silently skip a file.

TASKS, IN ORDER

1. ADAPT THE SCHEMA. Read a representative sample across the corpus,
   then fill `CLAUDE.md`: what this vault is, its category taxonomy
   (one `cat/<x>` tag per top-level stratum), the extraction table
   above, the junk rules, the privacy lines (what must never enter a
   wiki page), and slug conventions. Replace every {{{{param}}}}
   placeholder. This file is what makes future passes consistent with
   this one.

2. WRITE ONE PAGE PER SOURCE to `wiki/<slug>.md`, slug = kebab-case of
   the filename without extension. Exact frontmatter:

---
title: <human title, cleaned up from the filename or content>
type: source-summary
source_type: <emergent — academic-paper, clipped-article, memo, photo>
tags: [<one cat/ tag>, <2-5 content tags>]
created: {today}
updated: {today}
source: "[[<EXACT filename byte-for-byte, including extension>]]"
---

   Body: an H1 title; one bold line "**Gist:** ..."; a Summary of 1-2
   tight paragraphs; optional Notable details bullets; a Connections
   section linking related pages in this tranche.

   Tags describe CONTENT only — subjects, people, entities, concepts,
   kebab-case. Never tag file format or platform.

   For a homogeneous run of near-identical documents (statement sets,
   draft variants, a bookmark folder), write ONE catalog page instead of
   N: same frontmatter but `source_type: catalog`, `source_count: <N>`,
   no `source:` line, and a Contents table with one row per file
   `| [[Exact Filename.ext]] | what it is | note |`.

3. BUILD THE CONNECTIVE LAYER. From the pages you just wrote, create
   `type: concept` pages for the recurring ideas and `type: entity`
   pages for the people, organizations and products that appear across
   several sources — this is what makes the vault answer questions the
   individual documents do not. Create `type: topic` hub pages for the
   clusters you found. Link them both ways. A page with no inbound link
   is invisible.

4. RECORD IT. Add catalog rows to `index.md` and update its coverage
   table; add a two-line entry to `log.md`; write the detail page
   `logs/{today}-commission-01.md` (bump the number if that name is
   taken) with: what you covered, the taxonomy you chose and why, every
   extraction blocker by filename with its reason, what you deferred,
   and what the next pass should start with.

5. VALIDATE, THEN INDEX. Run:

     qocha lint "{vault}" --raw-dir {facts['raw_dir']}
     qocha preflight "{vault}" --raw-dir {facts['raw_dir']}

   Fix everything they flag — unresolved wikilinks and dangling source
   edges are the two failures that make a vault look built and answer
   nothing. Then build retrieval:

     qocha index "{vault}"

   and confirm with one domain-relevant `qocha search "{vault}" "..."`
   that the pages you wrote actually surface.

STYLE

Plain professional prose. No emojis anywhere. Never invent a fact that
is not in the source; if something is unclear, say so on the page.

WHEN YOU ARE DONE, report: how many pages you wrote, the taxonomy you
chose, every blocker by name, and what the next tranche should cover.
"""
