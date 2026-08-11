"""The Qocha engine: one class owning a vault's index, search, and ask.

A Vault binds a folder of markdown notes to a regenerable sqlite sidecar
with two retrieval layers — FTS5 for exact recall, local embedding
vectors for semantic recall — fused with reciprocal-rank fusion. ask()
retrieves, sends only the retrieved excerpts to the answer backend, and
validates every citation against the retrieved paths, so a citation
never points at a note the model was not shown.

Everything degrades gracefully: no embedder (or its daemon down) leaves
FTS working and vectors resuming later; no answerer leaves search
working. The index is never the source of truth — delete it and rescan.

Index shape (all regenerable):
  notes(path PK, title, mtime, size)      one row per markdown file
  chunks(id PK, path, seq, heading, text) heading-path chunks
  chunks_fts (FTS5: title, heading, text) rowid = chunks.id
  vecs(chunk_id PK, vec BLOB float16)     unit vectors, brute-force numpy
  meta(k, v)                              status + generation counters
"""
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
except ImportError:                      # vector layer optional; FTS works
    np = None

from .answer import ClaudeCLI
from .chunker import chunk_markdown, title_of
from .config import Config
from .embed import OllamaEmbedder

EMBED_BATCH = 32
RRF_K = 60
NOTE_CAP = 40_000            # recommended cap for CONTEXT-WINDOW callers.
# note_text() does not apply it unless asked -- see note_text's docstring.
TRUNCATED = ("\n\n[truncated: showing the first {shown:,} of {total:,} "
             "characters of {path}. This is a FRAGMENT, not the whole note. "
             "Re-read with a larger cap before summarizing or concluding "
             "anything about what the note does or does not contain.]")

ASK_PROMPT = """You are {owner}'s personal research assistant. You answer \
questions by drawing on excerpts from {owner}'s notes vault — a second \
brain of projects, people, decisions, and personal notes.

Below are the user's question and numbered CONTEXT CHUNKS, each labelled \
with its source note path and the heading path within that note. Treat the \
chunks as ground truth about {owner}'s world, but not as exhaustive.

How to answer:
- Lead with the direct answer; synthesize across notes only when the \
question calls for it.
- Cite every substantive claim inline as [[note-name]] where note-name is \
the source path without the .md extension (folder-qualified when needed, \
e.g. [[wiki/some-note]]). Only cite paths that appear in the chunks.
- If chunks contradict each other, surface the conflict and cite both.
- If the chunks do not contain the answer, say plainly: "I don't see this \
in your notes." No padding, no general-knowledge guessing.
- Personal, frank register — this is {owner}'s own brain.

Question: {question}

CONTEXT CHUNKS:
{chunks}
"""


_DEFAULT = object()      # distinguishes "use the default backend" from
                         # an explicit None ("no backend")


class Vault:
    def __init__(self, root, config=None, embedder=_DEFAULT,
                 answerer=_DEFAULT, **overrides):
        self.config = config or Config.load(root, **overrides)
        self.embedder = (OllamaEmbedder(self.config.ollama_url,
                                        self.config.embed_model)
                         if embedder is _DEFAULT else embedder)
        self.answerer = (ClaudeCLI(self.config.answer_model)
                         if answerer is _DEFAULT else answerer)
        self._lock = threading.Lock()    # serialize writers
        self._vec_state = {"gen": -1, "ids": None, "mat": None}

    # ---------- storage ----------

    def _connect(self):
        db = self.config.db
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db, timeout=60)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @staticmethod
    def _init(con):
        con.executescript(
            "CREATE TABLE IF NOT EXISTS notes("
            " path TEXT PRIMARY KEY, title TEXT, mtime REAL, size INTEGER);"
            "CREATE TABLE IF NOT EXISTS chunks("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " path TEXT, seq INTEGER, heading TEXT, text TEXT);"
            "CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);"
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
            " title, heading, text);"
            "CREATE TABLE IF NOT EXISTS vecs("
            " chunk_id INTEGER PRIMARY KEY, vec BLOB);"
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);")
        con.commit()

    @staticmethod
    def _meta_get(con, k, default=""):
        row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    @staticmethod
    def _meta_set(con, k, v):
        con.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    # ---------- scanning / indexing ----------

    def _iter_files(self):
        root = self.config.root
        if not root.is_dir():
            return
        exclude = self.config.exclude_dirs
        if self.config.dirs is None:
            for f in sorted(root.rglob("*.md")):
                rel_parts = f.relative_to(root).parts[:-1]
                if any(p in exclude or p.startswith(".") for p in rel_parts):
                    continue
                yield f
        else:
            yield from sorted(root.glob("*.md"))
            for d in self.config.dirs:
                base = root / d
                if base.is_dir():
                    yield from sorted(base.rglob("*.md"))

    def _rel(self, path):
        # Vault paths are logical identifiers — citations validate against
        # them, wikilinks resolve through them, API payloads carry them —
        # so they are ALWAYS posix-slashed, whatever the host separator.
        try:
            return Path(path).relative_to(self.config.root).as_posix()
        except ValueError:
            return Path(path).as_posix()

    def scan(self):
        """One incremental pass: (re)index changed files, drop removed ones.
        Returns counts. Cheap when nothing changed (stat-only)."""
        root = self.config.root
        if not root.is_dir():
            return {"error": f"vault root missing: {root}"}
        seen, changed, removed = set(), 0, 0
        with self._lock:
            con = self._connect()
            try:
                self._init(con)
                known = {r["path"]: (r["mtime"], r["size"]) for r in
                         con.execute("SELECT path, mtime, size FROM notes")}
                for f in self._iter_files():
                    rel = self._rel(f)
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    seen.add(rel)
                    if known.get(rel) == (st.st_mtime, st.st_size):
                        continue
                    try:
                        text = f.read_text(errors="replace")
                    except OSError:
                        continue
                    title = title_of(rel, text)
                    self._drop_path(con, rel)
                    for i, (heading, chunk) in enumerate(
                            chunk_markdown(text, title)):
                        cur = con.execute(
                            "INSERT INTO chunks(path, seq, heading, text) "
                            "VALUES(?,?,?,?)", (rel, i, heading, chunk))
                        con.execute(
                            "INSERT INTO chunks_fts"
                            "(rowid, title, heading, text) VALUES(?,?,?,?)",
                            (cur.lastrowid, title, heading, chunk))
                    con.execute(
                        "INSERT INTO notes(path,title,mtime,size)"
                        " VALUES(?,?,?,?)"
                        " ON CONFLICT(path) DO UPDATE SET"
                        " title=excluded.title, mtime=excluded.mtime,"
                        " size=excluded.size",
                        (rel, title, st.st_mtime, st.st_size))
                    changed += 1
                    if changed % 200 == 0:
                        con.commit()
                for gone in set(known) - seen:
                    self._drop_path(con, gone)
                    con.execute("DELETE FROM notes WHERE path=?", (gone,))
                    removed += 1
                self._meta_set(
                    con, "last_scan",
                    datetime.now(timezone.utc).isoformat(timespec="seconds"))
                if changed or removed:
                    self._meta_set(
                        con, "gen",
                        int(self._meta_get(con, "gen", "0") or 0) + 1)
                con.commit()
            finally:
                con.close()
        return {"changed": changed, "removed": removed, "seen": len(seen)}

    @staticmethod
    def _drop_path(con, rel):
        for r in con.execute("SELECT id FROM chunks WHERE path=?",
                             (rel,)).fetchall():
            con.execute("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
            con.execute("DELETE FROM vecs WHERE chunk_id=?", (r["id"],))
        con.execute("DELETE FROM chunks WHERE path=?", (rel,))

    def embed_pending(self, limit=2000):
        """Embed chunks that have no vector yet, in batches. Stops
        (resumably) when the embedder is unreachable. Returns the number
        embedded."""
        if np is None or self.embedder is None:
            return 0
        done = 0
        while done < limit:
            with self._lock:
                con = self._connect()
                try:
                    self._init(con)
                    rows = con.execute(
                        "SELECT c.id, c.heading, c.text FROM chunks c "
                        "LEFT JOIN vecs v ON v.chunk_id=c.id "
                        "WHERE v.chunk_id IS NULL LIMIT ?",
                        (EMBED_BATCH,)).fetchall()
                    if not rows:
                        return done
                    texts = [f"{r['heading']}\n{r['text']}" for r in rows]
                    vecs = self.embedder.embed_documents(texts)
                    if vecs is None:
                        return done      # backend down — resume next tick
                    for r, v in zip(rows, vecs):
                        con.execute(
                            "INSERT OR REPLACE INTO vecs(chunk_id, vec) "
                            "VALUES(?,?)",
                            (r["id"], v.astype("float16").tobytes()))
                    self._meta_set(
                        con, "gen",
                        int(self._meta_get(con, "gen", "0") or 0) + 1)
                    con.commit()
                    done += len(rows)
                finally:
                    con.close()
        return done

    def index(self, embed_limit=2000):
        """scan() + embed_pending() in one call — the common operation."""
        out = self.scan()
        if "error" not in out:
            out["embedded"] = self.embed_pending(limit=embed_limit)
        return out

    # ---------- query ----------

    @staticmethod
    def _fts_queries(q):
        terms = re.findall(r"[A-Za-z0-9']+", q)
        if not terms:
            return []
        all_q = " ".join(f'"{t}"' for t in terms)
        any_q = " OR ".join(f'"{t}"' for t in terms)
        return [all_q, any_q] if len(terms) > 1 else [any_q]

    def _rank_fts(self, con, q):
        out, seen = [], set()
        for fq in self._fts_queries(q):
            try:
                rows = con.execute(
                    "SELECT rowid, bm25(chunks_fts) AS r FROM chunks_fts "
                    "WHERE chunks_fts MATCH ? ORDER BY r LIMIT 200",
                    (fq,)).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in rows:
                if r["rowid"] not in seen:
                    seen.add(r["rowid"])
                    out.append(r["rowid"])
            if len(out) >= 200:
                break
        return out[:200]

    def _vec_matrix(self, con):
        """(ids, matrix) cache, rebuilt when the store generation moves."""
        if np is None:
            return None
        gen = int(self._meta_get(con, "gen", "0") or 0)
        st = self._vec_state
        if st["gen"] == gen and st["ids"] is not None:
            return st["ids"], st["mat"]
        rows = con.execute("SELECT chunk_id, vec FROM vecs").fetchall()
        if not rows:
            st.update(gen=gen, ids=None, mat=None)
            return None
        ids = np.array([r["chunk_id"] for r in rows], dtype="int64")
        mat = np.vstack([np.frombuffer(r["vec"], dtype="float16")
                         .astype("float32") for r in rows])
        st.update(gen=gen, ids=ids, mat=mat)
        return ids, mat

    def _rank_vec(self, con, q, floor=0.35):
        state = self._vec_matrix(con)
        if state is None or self.embedder is None:
            return []
        qv = self.embedder.embed_query(q)
        if qv is None:
            return []
        ids, mat = state
        sims = mat @ qv
        order = np.argsort(-sims)
        out = []
        for i in order[:200]:
            if sims[i] < floor:
                break
            out.append(int(ids[i]))
        return out

    def search(self, q, limit=10):
        """RRF-fused chunk hits: [{path, title, heading, text, score}]."""
        q = (q or "").strip()
        if not q:
            return []
        con = self._connect()
        try:
            self._init(con)
            ranks = {}
            for lst in (self._rank_fts(con, q), self._rank_vec(con, q)):
                for r, cid in enumerate(lst):
                    ranks[cid] = ranks.get(cid, 0.0) + 1.0 / (RRF_K + r)
            top = sorted(ranks, key=ranks.get, reverse=True)[:limit]
            out = []
            for cid in top:
                row = con.execute(
                    "SELECT c.path, c.heading, c.text, n.title FROM chunks c "
                    "LEFT JOIN notes n ON n.path=c.path WHERE c.id=?",
                    (cid,)).fetchone()
                if row:
                    out.append({"path": row["path"], "title": row["title"],
                                "heading": row["heading"],
                                "text": row["text"], "score": ranks[cid]})
            return out
        finally:
            con.close()

    def note_text(self, path, cap=None):
        """Full markdown of one note, path-checked under the vault root.

        UNCAPPED BY DEFAULT. A reader that stops half way through a note
        with no signal is indistinguishable from an ingest that only
        captured half of it -- measured 2026-08-11 on a 97,396-char
        podcast transcript served at 41%, which read as a broken ingest
        of a file that was complete on disk. 493 of 16,605 notes in the
        owner's vault exceed the old ceiling; the worst were served at 1%.

        `cap` is for callers feeding a context window, and it is HONEST:
        a truncated return says so IN BAND, so a model summarizing a
        transcript cannot mistake a fragment for the whole note. Pass
        NOTE_CAP for the recommended model-facing ceiling.
        """
        root = self.config.root.resolve()
        target = (root / path).resolve()
        try:
            target.relative_to(root)  # containment, separator-agnostic
        except ValueError:
            raise ValueError("path outside the vault") from None
        if target.suffix != ".md":
            raise ValueError("path outside the vault")
        text = target.read_text(errors="replace")
        if cap is not None and cap >= 0 and len(text) > cap:
            return text[:cap] + TRUNCATED.format(
                shown=cap, total=len(text), path=path)
        return text

    def status(self):
        con = self._connect()
        try:
            self._init(con)
            notes = con.execute(
                "SELECT COUNT(*) c FROM notes").fetchone()["c"]
            chunks = con.execute(
                "SELECT COUNT(*) c FROM chunks").fetchone()["c"]
            vecs = con.execute("SELECT COUNT(*) c FROM vecs").fetchone()["c"]
            return {"root": str(self.config.root), "db": str(self.config.db),
                    "notes": notes, "chunks": chunks, "vectors": vecs,
                    "last_scan": self._meta_get(con, "last_scan"),
                    "available": self.config.root.is_dir()}
        finally:
            con.close()

    # ---------- grounded ask ----------

    def ask(self, question, k=10, hits=None):
        """Grounded answer over the vault. Citations validated against the
        retrieved paths — a citation never points at a note the model was
        not shown.

        hits: answer over a caller-supplied hit set instead of retrieving.
        A caller that has already narrowed the corpus (a date window, a
        recency sort) must be able to keep that narrowing, or the answer
        would contradict the list the caller is showing."""
        if hits is None:
            hits = self.search(question, limit=k)
        if not hits:
            return {"answer": "I don't see anything about this in your "
                              "notes (no index hits).",
                    "citations": [], "hits": []}
        blocks = []
        for i, h in enumerate(hits, 1):
            blocks.append(f"--- CHUNK {i} | {h['path']} | {h['heading']}\n"
                          f"{h['text'][:2400]}")
        prompt = ASK_PROMPT.format(owner=self.config.owner,
                                   question=question,
                                   chunks="\n\n".join(blocks)[:60_000])
        answer = self.answerer(prompt)
        by_stem = {}
        for h in hits:
            stem = Path(h["path"]).with_suffix("").name.lower()
            # models cite every spelling: bare stem, folder-qualified, with
            # or without .md — accept all of them
            for key in (stem, stem + ".md",
                        str(Path(h["path"]).with_suffix("")).lower(),
                        h["path"].lower()):
                by_stem.setdefault(key, h["path"])
        citations, seen = [], set()
        for ref in re.findall(r"\[\[([^\]|]+?)\]\]", answer):
            path = by_stem.get(ref.strip().lower())
            if path and path not in seen:
                seen.add(path)
                title = next(
                    (h["title"] for h in hits if h["path"] == path),
                    Path(path).stem)
                citations.append({"ref": ref.strip(), "path": path,
                                  "title": title})
        return {"answer": answer, "citations": citations,
                "hits": [{k2: h[k2] for k2 in ("path", "title", "heading")}
                         for h in hits]}
