# If you are an AI agent working on qocha

Qocha is a small public library with one large downstream consumer: the
Vira app (github.com/MattaDurham/vira) pins it by git tag in its
requirements.txt and adapts it (server/vault.py there) against a live
production index of thousands of notes. That coupling is what most of
these rules protect.

## Tests

    python -m unittest discover tests

Stdlib unittest - no pytest, no extra installs. Run it before and after
any engine change.

## Invariants

- **Vault-relative paths are POSIX on every platform.** Stored paths are
  logical identifiers - citations validate against them and wikilinks
  resolve through them - so the host separator must never leak in. See
  the rel-path comment in `qocha/vault.py` and
  `tests/test_vault.py::test_paths_are_posix_on_every_platform`, which
  exists because a Windows run once stored `wiki\note.md` and broke
  citation validation. Containment checks use `Path.relative_to`, never
  a `/`-hardcoded startswith.
- **The sqlite schema is a compatibility surface, not an implementation
  detail.** "Delete the index and rescan" is true for a fresh vault, but
  the downstream consumer serves a production index that must keep
  working WITHOUT a re-index. A schema change needs a migration story,
  not just an edited CREATE TABLE.
- **The injection seams are public API.** `Vault(root, embedder=,
  answerer=)`, `chunk_markdown`, and `CHUNK_MAX` are imported and
  patched by the downstream adapter and its tests. Renaming or reshaping
  them is a breaking change - version it accordingly.

## Release ritual

1. Bump the version in BOTH `pyproject.toml` and `qocha/__init__.py`
   (`tests/test_version.py` pins the two to each other - the 0.2.0 vs
   0.3.0 drift of July 2026 is why).
2. Commit and push, then tag:
   `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.
3. The downstream pin advances by tag - a bump that is never tagged, or
   a tag the pin never advances to, reaches no one.
