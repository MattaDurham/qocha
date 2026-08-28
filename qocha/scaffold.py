"""`qocha init` — seed a three-layer vault.

Creates the missing layers and starter files for the pattern described
in conventions/the-pattern.md. Strictly additive: an existing file or
directory is never touched, so running it on a live vault only fills
gaps. Returns the list of vault-relative paths it created.

Two modes, and the only difference is WHERE the source corpus is.

Plain init assumes an empty new vault: sources have not arrived yet, so
it makes a `raw/` for them to land in.

`attached=True` is the unfamiliar-folder path — a directory that already
holds someone's files, pointed at qocha for the first time. Moving that
pile into `raw/` would be the one thing the pattern forbids (layer 1 is
immutable, and a folder the owner arranged is layer 1 whether or not it
is named raw), so attached mode records the vault root ITSELF as the
corpus and adds only the generated surfaces beside it. Nothing is moved,
renamed, or rewritten; `raw_dir: "."` in qocha.json is what makes lint,
preflight and the commissioning survey agree about that afterwards.
"""
import json
from datetime import date
from pathlib import Path

SCHEMA_STARTER = """# {name} — Schema

> **See also:** the qocha wiki spec (conventions/wiki-spec.md in the
> qocha repo) — this vault extends that foundation (three-layer
> pattern, ingest/query/lint/clean operations, density ladder,
> frontmatter shape). Everything below adds {name}-specific rules.
> Fill the {{{{params}}}} using conventions/adaptation-checklist.md.

## Purpose

{{{{CORPUS_DESCRIPTION — what this vault is, whose knowledge it holds,
whether sources are a live intake stream or a fixed archive.}}}}

## Directory layout

```
{name}/
├── CLAUDE.md          # this file
├── qocha.json         # engine config (owner, dirs, models)
├── index.md           # content catalog — read first on any query
├── log.md             # thin chronological index — 2-line entry per batch
├── logs/              # per-batch detail pages — type: log-detail
{corpus_layout}
└── wiki/              # agent-owned — all generated pages live here
```

**Invariant:** {corpus_invariant} Never create, rename, edit, or delete
files there.

## {name}-specific rules

- **Discipline layer:** {{{{git commit-per-op, or synced log-per-op}}}}
- **Extraction commands:** {{{{one line per source filetype}}}}
- **Taxonomy:** {{{{the category tags — top-level strata}}}}
- **Junk-drawer rule:** {{{{what is skipped at ingest, no stub}}}}
- **Privacy rules:** {{{{what never enters wiki pages}}}}
- **Hub pages:** {{{{the type: topic pages that organize the corpus}}}}

## Ingest state

Forward-only: a raw file is unprocessed if no source-summary references
it. Preflight every ingest with `qocha preflight .`; postflight with
`qocha lint .`.
"""

INDEX_STARTER = """# {name} — index

The content catalog. Read first on any query; updated by every ingest.

## Ingest coverage

| Tranche | Status | Notes |
|---|---|---|
| (first tranche) | pending | plan it in CLAUDE.md |

## Pages

(catalog rows land here as ingests run)
"""

LOG_STARTER = """# {name} — log

Thin chronological index: two lines per operation, details in `logs/`.

- {today} — vault initialized by `qocha init`.
"""


def init_vault(root, name=None, raw_dir=None, attached=False):
    """Create the missing pieces of a three-layer vault under `root`.

    Returns vault-relative paths created (empty when everything already
    existed). With `attached`, the vault root itself is the corpus: no
    `raw/` is made and the existing files are left exactly where they
    are. An explicit `raw_dir` still wins in either mode, which is how a
    corpus that already uses `sources/` or `RAW/` keeps its own name.
    """
    vault = Path(root).expanduser().resolve()
    name = name or vault.name
    if raw_dir is None:
        raw_dir = "." if attached else "raw"
    created = []

    def mkdir(rel):
        p = vault / rel
        if not p.is_dir():
            p.mkdir(parents=True)
            created.append(rel + "/")

    def seed(rel, content):
        p = vault / rel
        if not p.exists():
            p.write_text(content)
            created.append(rel)

    # "." is the vault root, which exists by definition — making it would
    # be a no-op at best and, on a corpus dir, a claim we reorganized it.
    if raw_dir not in (".", ""):
        mkdir(raw_dir)
    mkdir("wiki")
    mkdir("logs")
    today = date.today().isoformat()
    if raw_dir in (".", ""):
        layout = ("├── <your files>       # IMMUTABLE — the corpus that was "
                  "here first")
        invariant = ("every file that was in this folder before qocha is "
                     "read-only for the agent.")
    else:
        layout = f"├── {raw_dir}/             # IMMUTABLE — never modify, only read"
        invariant = f"`{raw_dir}/` is read-only for the agent."
    seed("CLAUDE.md", SCHEMA_STARTER.format(
        name=name, corpus_layout=layout, corpus_invariant=invariant))
    seed("index.md", INDEX_STARTER.format(name=name))
    seed("log.md", LOG_STARTER.format(name=name, today=today))
    seed("qocha.json", json.dumps(
        {"owner": "the owner", "answer_model": "sonnet",
         "raw_dir": raw_dir}, indent=2) + "\n")
    return created
