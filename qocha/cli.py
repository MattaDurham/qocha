"""The qocha command line.

Engine:
    qocha index  <root> [--db PATH] [--no-embed] [--embed-limit N] [--watch]
    qocha search <root> "query" [--db PATH] [-n N] [--json]
    qocha ask    <root> "question" [--db PATH] [-k N] [--model NAME] [--json]
    qocha status <root> [--db PATH]

Harness (see conventions/ and harness/ in the repo):
    qocha init      <root> [--raw-dir raw] [--attached] [--name NAME]
    qocha survey    <root> [--raw-dir DIR] [--json]
    qocha commission <root> [--raw-dir DIR] [--tranche N]
    qocha lint      <root> [--raw-dir raw] [--wiki-dir wiki]
                           [--allow-unresolved-links]
    qocha preflight <root> [--raw-dir raw] [--wiki-dir wiki]
                           [--pending-dir pending-user-deletion]

The vault root may carry a qocha.json overriding engine defaults (dirs,
owner, db, ollama_url, embed_model, answer_model); flags win over the
file.
"""
import argparse
import json
import sys
import time

from .commission import commission_prompt, survey
from .config import Config
from .lint import lint_vault, preflight
from .scaffold import init_vault
from .vault import Vault


def _raw_dir(args):
    """The corpus directory: an explicit flag, else whatever the vault
    recorded at init. An attached vault stores "." and would otherwise be
    linted against a raw/ that deliberately does not exist."""
    return getattr(args, "raw_dir", None) or Config.load(args.root).raw_dir


def _vault(args):
    overrides = {"db": getattr(args, "db", None)}
    if getattr(args, "model", None):
        overrides["answer_model"] = args.model
    return Vault(args.root, **overrides)


def cmd_index(args):
    v = _vault(args)
    t0 = time.time()
    out = v.scan()
    if "error" in out:
        print(out["error"], file=sys.stderr)
        return 1
    print(f"scanned {out['seen']} notes "
          f"({out['changed']} changed, {out['removed']} removed) "
          f"in {time.time() - t0:.1f}s")
    if not args.no_embed:
        t0 = time.time()
        n = v.embed_pending(limit=args.embed_limit)
        if n:
            print(f"embedded {n} chunks in {time.time() - t0:.1f}s")
        else:
            st = v.status()
            if st["vectors"] < st["chunks"]:
                print(f"embedder unreachable — {st['chunks'] - st['vectors']}"
                      " chunks pending, full-text search still works")
    if args.watch:
        print("watching (Ctrl-C to stop)")
        try:
            while True:
                time.sleep(args.interval)
                out = v.scan()
                if out.get("changed") or out.get("removed"):
                    print(f"rescan: {out['changed']} changed, "
                          f"{out['removed']} removed")
                v.embed_pending()
        except KeyboardInterrupt:
            pass
    return 0


def cmd_search(args):
    v = _vault(args)
    hits = v.search(args.query, limit=args.n)
    if args.json:
        print(json.dumps(hits, indent=1))
        return 0
    if not hits:
        print("no hits")
        return 0
    for h in hits:
        print(f"{h['path']}  ::  {h['heading']}")
        snippet = " ".join(h["text"].split())[:180]
        print(f"    {snippet}")
    return 0


def cmd_ask(args):
    v = _vault(args)
    out = v.ask(args.question, k=args.k)
    if args.json:
        print(json.dumps(out, indent=1))
        return 0
    print(out["answer"].strip())
    if out["citations"]:
        print("\nsources:")
        for c in out["citations"]:
            print(f"  [[{c['ref']}]] -> {c['path']}")
    return 0


def cmd_status(args):
    v = _vault(args)
    st = v.status()
    for k, val in st.items():
        print(f"{k:>10}: {val}")
    return 0


def cmd_init(args):
    created = init_vault(args.root, name=args.name, raw_dir=args.raw_dir,
                         attached=args.attached)
    if created:
        for rel in created:
            print(f"created {rel}")
    else:
        print("nothing to do — vault already has every layer")
    if args.attached:
        print("attached: your existing files are the corpus and were not "
              "moved.\nnext: `qocha commission .` for the writing pass that "
              "turns them into linked pages")
    elif created:
        print("next: fill the {{params}} in CLAUDE.md "
              "(conventions/adaptation-checklist.md walks it)")
    return 0


def cmd_survey(args):
    facts = survey(args.root, raw_dir=getattr(args, "raw_dir", None))
    if args.json:
        print(json.dumps(facts, indent=1))
        return 0
    lay = facts["layers"]
    print(f"      corpus: {facts['raw_dir']}"
          f"{'  (attached — the vault root)' if facts['attached'] else ''}")
    print(f"       files: {facts['files']}"
          f"{'  (capped, more remain)' if facts['truncated'] else ''}")
    print(f" unprocessed: {facts['unprocessed']}")
    print(f"      schema: {'adapted' if lay['schema_adapted'] else
                           'present, still templated' if lay['schema']
                           else 'missing'}")
    print(f"        wiki: {lay['wiki']['pages']} pages")
    print(f"       index: {'built' if lay['index'] else 'not built'}")
    print(f" commissioned: {'yes' if lay['logs']['commissioned'] else 'no'}")
    for row in facts["extensions"][:10]:
        print(f"    {row['count']:>6}  {row['ext']}")
    return 0


def cmd_commission(args):
    """Print the commissioning prompt. Qocha composes the contract; the
    model that runs it is the caller's to choose, so this writes nothing
    and spends nothing."""
    print(commission_prompt(args.root, tranche=args.tranche,
                            raw_dir=getattr(args, "raw_dir", None)))
    return 0


def cmd_lint(args):
    args.raw_dir = _raw_dir(args)
    problems = lint_vault(args.root, raw_dir=args.raw_dir,
                          wiki_dir=args.wiki_dir,
                          allow_unresolved=args.allow_unresolved_links)
    for rel, msg in problems:
        print(f"  - {rel}: {msg}")
    print(f"{len(problems)} problem{'s' if len(problems) != 1 else ''}")
    return 1 if problems else 0


def cmd_preflight(args):
    args.raw_dir = _raw_dir(args)
    out = preflight(args.root, raw_dir=args.raw_dir,
                    wiki_dir=args.wiki_dir, pending_dir=args.pending_dir)
    for rel, target in out["pending"]:
        print(f"  [info] {rel} -> [[{target}]] is in {args.pending_dir}/ "
              "(expected after a clean op)")
    for rel, target in out["orphans"]:
        print(f"  [orphan] {rel} -> [[{target}]] missing under "
              f"{args.raw_dir}/")
    if out["orphans"]:
        return 1
    if not out["pending"]:
        print(f"OK: all source: edges resolve under {args.raw_dir}/")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="qocha",
        description="Local-first vault engine: hybrid search and grounded, "
                    "cited answers over a folder of markdown notes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("root", help="vault root directory")
        sp.add_argument("--db", default=None,
                        help="index path (default <root>/.qocha/index.sqlite)")

    sp = sub.add_parser("index", help="scan the vault and fill vectors")
    common(sp)
    sp.add_argument("--no-embed", action="store_true",
                    help="scan only, skip the vector fill")
    sp.add_argument("--embed-limit", type=int, default=1_000_000,
                    help="max chunks to embed this run")
    sp.add_argument("--watch", action="store_true",
                    help="keep rescanning in the foreground")
    sp.add_argument("--interval", type=int, default=300,
                    help="rescan interval seconds with --watch")
    sp.set_defaults(fn=cmd_index)

    sp = sub.add_parser("search", help="hybrid search, ranked chunks")
    common(sp)
    sp.add_argument("query")
    sp.add_argument("-n", type=int, default=10, help="max hits")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_search)

    sp = sub.add_parser("ask", help="grounded, cited answer over the vault")
    common(sp)
    sp.add_argument("question")
    sp.add_argument("-k", type=int, default=10, help="chunks to retrieve")
    sp.add_argument("--model", default=None,
                    help="answer model (default sonnet)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("status", help="index counts and freshness")
    common(sp)
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("init", help="seed a three-layer vault skeleton")
    sp.add_argument("root", help="vault root directory")
    sp.add_argument("--raw-dir", default=None,
                    help="corpus directory (default: raw, or the vault root "
                         "with --attached)")
    sp.add_argument("--attached", action="store_true",
                    help="the folder already holds your files: treat them as "
                         "the corpus in place and add only the generated "
                         "layers beside them")
    sp.add_argument("--name", default=None,
                    help="vault display name (default: directory name)")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("survey", help="what is in the corpus, and which "
                                       "layers exist")
    sp.add_argument("root", help="vault root directory")
    sp.add_argument("--raw-dir", default=None)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_survey)

    sp = sub.add_parser("commission",
                        help="print the commissioning prompt for one tranche")
    sp.add_argument("root", help="vault root directory")
    sp.add_argument("--raw-dir", default=None)
    sp.add_argument("--tranche", type=int, default=40,
                    help="sources this pass covers (default 40)")
    sp.set_defaults(fn=cmd_commission)

    sp = sub.add_parser("lint", help="structural lint of the wiki layer")
    sp.add_argument("root", help="vault root directory")
    sp.add_argument("--raw-dir", default=None)
    sp.add_argument("--wiki-dir", default="wiki")
    sp.add_argument("--allow-unresolved-links", action="store_true",
                    help="treat unresolved [[wikilinks]] as intentional "
                         "seed links")
    sp.set_defaults(fn=cmd_lint)

    sp = sub.add_parser("preflight",
                        help="ingest preflight: dangling source edges")
    sp.add_argument("root", help="vault root directory")
    sp.add_argument("--raw-dir", default=None)
    sp.add_argument("--wiki-dir", default="wiki")
    sp.add_argument("--pending-dir", default="pending-user-deletion")
    sp.set_defaults(fn=cmd_preflight)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
