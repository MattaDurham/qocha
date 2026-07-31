# qocha

A local-first vault engine: hybrid semantic search and grounded, cited
answers over a folder of markdown notes.

Point it at any Obsidian-style vault (or any directory of markdown) and
you get a regenerable index with two retrieval layers - full-text for
exact recall, local embeddings for semantic recall - fused into one
ranking, plus an `ask` command whose answers cite their source notes,
with every citation validated against what the model was actually shown.

The design premise is a seam: **the engine is public; the vault it
connects to stays yours and private.** Qocha never phones home. Indexing
is fully local. `ask` sends only the retrieved excerpts and your
question to the model backend you configure, and only when you call it.

## The pattern

Qocha is built for the three-layer vault pattern popularly known as
Karpathy's LLM Wiki: an immutable `raw/` dump folder, an LLM-written
`wiki/`, and a schema file the owner co-evolves with the model. The
pattern says take the structure and make it your own. Qocha puts an
engine in it. (The engine does not require the pattern - any folder of
markdown works - but the conventions and ingest layers on the roadmap
assume it.)

## Quickstart

```bash
git clone <this repo> && cd qocha
python3 -m venv .venv && .venv/bin/pip install -e .

# try it on the bundled synthetic demo vault
.venv/bin/qocha index demo/vault
.venv/bin/qocha search demo/vault "why recoat instead of replacing"
.venv/bin/qocha ask demo/vault "What happens to open nights during the downtime?"

# then your own vault
.venv/bin/qocha index ~/notes
.venv/bin/qocha status ~/notes

# the three-layer harness (see conventions/ and harness/)
.venv/bin/qocha init ~/notes         # seed raw/, wiki/, the schema file
.venv/bin/qocha lint ~/notes         # structural lint of the wiki layer
.venv/bin/qocha preflight ~/notes    # dangling source-edge check
```

Requirements: Python 3.10+, numpy. For semantic search: a local
[Ollama](https://ollama.com) with `nomic-embed-text` pulled. For `ask`:
the Claude Code CLI logged in (or pass your own answer backend in
Python). Without Ollama, full-text search still works; without a model
backend, search still works. Every layer degrades independently:

| Missing | What still works |
|---|---|
| Ollama down | Full-text search; vectors resume when it returns |
| No model backend | index / search / status |
| numpy absent | Full-text search |

## How it works

- **Chunking** - notes split on `##`/`###` headings; each chunk carries
  a heading-path breadcrumb (`note > section > subsection`) so results
  and citations stay human-legible. Small adjacent sections merge toward
  ~1,800 chars; oversized ones hard-split at 4,000.
- **Index** - a sqlite sidecar at `<vault>/.qocha/index.sqlite`
  (override with `--db`): note rows, chunk rows, an FTS5 table, and
  float16 unit vectors matched brute-force in numpy. Regenerable by
  construction - delete it and rescan; the notes are always the source
  of truth.
- **Search** - FTS rank and vector rank fused with reciprocal-rank
  fusion, so either layer can carry a query the other misses.
- **Ask** - retrieve top chunks, prompt the backend with them, then
  validate every `[[citation]]` in the answer against the retrieved
  paths (all common spellings accepted: bare stem, folder-qualified,
  with or without `.md`). A citation never points at a note the model
  was not shown. If the notes do not contain the answer, the prompt
  instructs the model to say so plainly.
- **Incremental** - rescans are stat-only when nothing changed; edits
  and deletions reconcile per file. An embeddable `Indexer` thread (or
  `qocha index --watch`) keeps the index current.

Measured on a 15,243-note / 39,046-chunk production vault (Apple
Silicon): first scan 8 seconds, quiet rescan under one second, embedding
throughput about 40 chunks/second through Ollama.

## Configuration

Flags win over `<vault>/qocha.json`, which wins over defaults:

```json
{
  "owner": "Ada",
  "dirs": ["wiki", "Sessions"],
  "ollama_url": "http://localhost:11434",
  "embed_model": "nomic-embed-text",
  "answer_model": "sonnet"
}
```

`dirs` omitted means the whole vault, recursively (`.obsidian`,
`.trash`, and friends always excluded). `owner` personalizes the ask
prompt.

## Python API

```python
from qocha import Vault, Indexer

v = Vault("~/notes")            # defaults: Ollama embedder, Claude CLI answerer
v.index()
hits = v.search("mirror recoating tradeoff")
out = v.ask("Why did we choose the recoat?")   # out["answer"], out["citations"]
out = v.ask("Why?", hits=hits)  # answer over a hit set you already retrieved

Indexer(v).start()              # background rescan + vector fill

# bring your own backends
v = Vault("~/notes", embedder=None, answerer=my_prompt_to_text_fn)
```

An embedder is anything with `embed_documents(texts)` /
`embed_query(text)` returning unit vectors (or `None` when
unreachable); an answerer is any `prompt -> str` callable.

## Development

Tests are stdlib unittest - no extra install:

    python -m unittest discover tests

`AGENTS.md` carries the working contract for this repo - the invariants
(POSIX vault paths, sqlite schema stability, the public injection seams)
and the release ritual. Read it before changing the engine.

## Roadmap

Qocha builds out in three stages:

1. **Engine** (v0.1) - the index, hybrid search, and grounded cited
   ask described above.
2. **Conventions + harness** (v0.2) - the content
   conventions for the three-layer pattern (`conventions/`: the
   pattern, the wiki spec, a schema template, an adaptation
   checklist), the parallel ingest-agent contract (`harness/`), and
   the structural tooling: `qocha init` seeds a vault, `qocha lint`
   enforces the machine-checkable spec, `qocha preflight` catches
   dangling source edges before every ingest. The bundled demo vault
   now demonstrates the pattern and passes its own lint.
3. **Feeders + operator kit** - intake pipelines that keep a vault
   filling itself, each dormant until configured: an inbox for web
   clips (pair it with the official Obsidian Web Clipper; Qocha ships
   only the receiving side), Apple Notes export, audio-recorder
   transcripts, chat-history exports, image ingest - plus the operator
   skills and routine templates that run them on a schedule. Also in
   this stage: optional media extractors (OCR, image scene vectors,
   face matching, transcripts) as engine plugins, and an MCP surface.

## Provenance

Qocha is the extracted engine of a personal production system - a
local-first AI chief-of-staff app whose vault search this code powered
first - operated and maintained daily, solo-built with AI coding agents
used openly and steered deliberately. The bundled demo vault is fully
synthetic.

## License

MIT.
