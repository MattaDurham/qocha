"""Structural lint and ingest preflight for three-layer vaults.

Enforces the machine-checkable half of conventions/wiki-spec.md:

lint_vault:
  - every wiki page has a frontmatter block with title/type/tags/
    created/updated (meta page types exempt from tags)
  - source-summary pages carry source: "[[...]]" as a QUOTED wikilink
    resolving under raw/ (roundups with source_count >= 2 and kept
    orphans with source_count: 0 legitimately omit it)
  - frontmatter wikilinks are quoted (unquoted ones break Obsidian's
    Properties pane)
  - wiki slugs are unique
  - [[wikilinks]] resolve — against wiki slugs, any vault markdown,
    raw files (with or without extension), raw directories, or a
    path-qualified spelling of any of those

preflight:
  - every source: edge points at an existing raw file/dir; edges into
    pending-user-deletion/ are reported as expected-after-clean info,
    not orphans. Run before every ingest so manual deletions self-heal.

Both are read-only. The resolver follows symlinked raw subtrees,
accepts extensionless links, and unescapes YAML double-quote escapes —
lessons from the first production deployments, where a naive resolver
produced hundreds of false positives.
"""
import os
import re
from pathlib import Path

from .config import EXCLUDE_DIRS

REQUIRED_KEYS = ("title", "type", "tags", "created", "updated")
TAGS_EXEMPT_TYPES = {"building-block", "feature", "build-log",
                     "log-detail", "operations-report"}
ROOT_FILES = {"CLAUDE.md", "index.md", "log.md"}

_FM_BLOCK = re.compile(r"^---\n(.*?)\n---\n", re.S)
_SOURCE_LINE = re.compile(r'^source:\s*"\[\[(.+?)\]\]"\s*$', re.M)
_WIKILINK = re.compile(r"\[\[([^\[\]\|]+?)(?:\|[^\]]*)?\]\]")


def _yaml_unescape(s):
    return s.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00",
                                                                 "\\")


def _walk_targets(root):
    """(file names + stems, dir names) resolvable under `root`,
    following symlinks.

    Every target is recorded under two spellings: the bare name (or stem)
    Obsidian's shortest-path resolver accepts, and the vault-relative path
    (`raw/family/note.md`, `raw/family/note`, `raw/family`) that a vault
    writing path-qualified links puts in `source:`. Paths are POSIX on
    every platform, matching the identifier rule in `qocha/vault.py`.
    `root` is `<vault>/<raw_dir>`, so the vault is `root.parent`."""
    files, dirs = set(), set()
    if not root.is_dir():
        return files, dirs
    vault = root.parent
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        here = Path(dirpath)
        for d in dirnames:
            dirs.add(d)
            dirs.add((here / d).relative_to(vault).as_posix())
        for f in filenames:
            rel = (here / f).relative_to(vault)
            files.add(f)
            files.add(Path(f).stem)
            files.add(rel.as_posix())
            files.add(rel.with_suffix("").as_posix())
    return files, dirs


def _vault_md_targets(vault, exclude=EXCLUDE_DIRS):
    """Names, stems, and vault-relative path spellings of every markdown
    file in the vault (Obsidian resolves all of these)."""
    out = set()
    for p in vault.rglob("*.md"):
        parts = p.relative_to(vault).parts[:-1]
        if any(d in exclude or d.startswith(".") for d in parts):
            continue
        rel = p.relative_to(vault)
        out.add(p.name)
        out.add(p.stem)
        out.add(str(rel))
        out.add(str(rel.with_suffix("")))
    return out


def _frontmatter(text):
    m = _FM_BLOCK.match(text)
    return m.group(1) if m else None


def lint_vault(root, raw_dir="raw", wiki_dir="wiki",
               allow_unresolved=False):
    """Lint the wiki layer. Returns [(vault-relative page, problem)]."""
    vault = Path(root).expanduser().resolve()
    wiki = vault / wiki_dir
    pages = sorted(wiki.rglob("*.md"))
    if not pages:
        return [(wiki_dir, f"no pages under {wiki}")]

    raw_files, raw_dirs = _walk_targets(vault / raw_dir)
    md_targets = _vault_md_targets(vault)
    problems = []

    stems = {}
    for p in pages:
        stems.setdefault(p.stem, []).append(p)
    for stem, dupes in stems.items():
        if len(dupes) > 1:
            rels = ", ".join(str(d.relative_to(vault)) for d in dupes)
            problems.append((rels, f"duplicate slug {stem!r}"))

    for p in pages:
        rel = str(p.relative_to(vault))
        text = p.read_text(encoding="utf-8", errors="replace")
        fm = _frontmatter(text)
        if fm is None:
            problems.append((rel, "no frontmatter block"))
            continue

        tm = re.search(r"^type:\s*(\S+)", fm, re.M)
        page_type = tm.group(1) if tm else None
        for key in REQUIRED_KEYS:
            if key == "tags" and page_type in TAGS_EXEMPT_TYPES:
                continue
            if not re.search(rf"^{key}:", fm, re.M):
                problems.append((rel, f"missing frontmatter key {key}"))

        if page_type == "source-summary":
            sm = _SOURCE_LINE.search(fm)
            scm = re.search(r"^source_count:\s*(\d+)", fm, re.M)
            source_count = int(scm.group(1)) if scm else None
            if not sm:
                # roundups/catalogs (N>=2 raws bundled via body links)
                # and kept orphans (source_count: 0) legitimately omit it
                if source_count is None or source_count == 1:
                    problems.append(
                        (rel, 'source: missing or not a quoted "[[...]]"'
                              ' wikilink'))
            else:
                target = _yaml_unescape(sm.group(1))
                if (target not in raw_files and target + ".md"
                        not in raw_files and target not in raw_dirs):
                    problems.append(
                        (rel, f"source target not found under "
                              f"{raw_dir}/: {target!r}"))

        for line in fm.splitlines():
            if (re.search(r"(^|\s)- \[\[", line)
                    or re.search(r"^source[s]?: \[\[", line)):
                problems.append(
                    (rel, f"unquoted frontmatter wikilink:"
                          f" {line.strip()[:60]}"))

        if allow_unresolved:
            continue
        for inner in _WIKILINK.findall(text):
            inner = _yaml_unescape(inner.strip())
            # file-style targets (contain a dot) are raw edges or embeds
            # — the source-edge check above owns those
            if "." in inner and inner not in ROOT_FILES:
                continue
            link = inner.split("#", 1)[0].strip()
            if not link:
                continue
            if (link in md_targets or link in ROOT_FILES
                    or link in raw_files or link in raw_dirs):
                continue
            problems.append((rel, f"unresolved wikilink [[{link}]]"))

    return problems


def preflight(root, raw_dir="raw", wiki_dir="wiki",
              pending_dir="pending-user-deletion"):
    """Dangling wiki -> raw source edges. Returns
    {"orphans": [(page, target)], "pending": [(page, target)]}."""
    vault = Path(root).expanduser().resolve()
    raw_files, raw_dirs = _walk_targets(vault / raw_dir)
    pending_files, _ = _walk_targets(vault / pending_dir)
    orphans, pending = [], []
    for p in sorted((vault / wiki_dir).rglob("*.md")):
        fm = _frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        if fm is None:
            continue
        rel = str(p.relative_to(vault))
        for m in _SOURCE_LINE.finditer(fm):
            target = _yaml_unescape(m.group(1))
            if target in raw_files or target in raw_dirs:
                continue
            if target in pending_files:
                pending.append((rel, target))
            else:
                orphans.append((rel, target))
    return {"orphans": orphans, "pending": pending}
