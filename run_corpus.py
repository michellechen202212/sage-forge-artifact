#!/usr/bin/env python3
"""Run the adapter over the sampled corpus and print the tables in the paper.

Usage:  python3 run_corpus.py [corpus_root]
        python3 run_corpus.py [corpus_root] --all   # whole pinned pool, not the sample
        SAGE_CORPUS=/path python3 run_corpus.py
Default corpus root: ./corpus next to this script.
"""
import json, os, sys, collections

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sage_adapter import run, QUESTIONS, PRIMARY_DIALECT  # noqa: E402
import sqlglot  # noqa: E402


def corpus_root():
    # positional arg only; flags such as --all must not be read as a path
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        return os.path.abspath(args[0])
    return os.path.abspath(os.environ.get("SAGE_CORPUS", os.path.join(HERE, "corpus")))


def run_whole_pool(root):
    """Every eligible model in the pinned pool, not just the 40-model sample.

    Reports how often the fan-out shape actually occurs, which the sample is
    too small to show.
    """
    import build_corpus as bc
    missing = [r for r in bc.PINS
               if not os.path.isdir(os.path.join(root, r.replace("/", "_")))]
    if missing:
        sys.exit(f"corpus not found under {root} (missing {len(missing)} repos).\n"
                 f"Run build_corpus.py first, or pass the corpus root.")
    pool = [(r.replace("/", "_"), f) for r in bc.PINS
            for f in bc.project_models(os.path.join(root, r.replace("/", "_")))]
    analysed, shapes = 0, []
    for key, f in pool:
        res = run(open(f, encoding="utf-8", errors="replace").read())
        if res["stage"] != "analysed":
            continue
        analysed += 1
        fs = res["evidence"]["fanout_shape"]
        if fs:
            shapes.append((os.path.basename(f), fs,
                           res["evidence"]["columns_at_multiplied_grain"],
                           res["questions"]["E3_fanout_measure"]["status"]))
    print(f"whole pinned pool: {len(pool)} eligible, {analysed} analysable, "
          f"{len(shapes)} exhibit the fan-out shape")
    for n, fs, cols, st in shapes:
        k = list(fs)[0]
        tag = "union discriminator" if "source_relation" in k else "business key"
        print(f"   [{tag:<20}] {n[:44]:<46} key={k}  E3={st}")
    return len(pool), analysed, len(shapes)


def main():
    root = corpus_root()
    if "--all" in sys.argv:
        run_whole_pool(root)
        return
    sample = json.load(open(os.path.join(HERE, "sample.json")))
    rows = []
    for m in sample:
        f = os.path.join(root, m["repo"], m["path"])
        if not os.path.exists(f):
            sys.exit(f"missing {f}\nRun build_corpus.py first, or pass the corpus root.")
        r = run(open(f, encoding="utf-8", errors="replace").read())
        rows.append({**{k: m[k] for k in ("stratum", "repo", "path", "bytes")}, **r})
    json.dump(rows, open(os.path.join(HERE, "results.json"), "w"), indent=1, default=str)

    def block(label, rs):
        N = len(rs)
        an = sum(1 for r in rs if r["stage"] == "analysed")
        tot = N * len(QUESTIONS)
        a = sum(1 for r in rs for q in QUESTIONS if r["questions"][q]["status"] == "ANSWERED")
        print(f"\n--- {label}: N={N} ---")
        print(f"  templating resolved + parsed : {an}/{N}")
        tmpl = collections.Counter(r["detail"] for r in rs if r["stage"] == "templating")
        if tmpl:
            print(f"  templating REFUSE reasons    : {dict(tmpl)}")
        perr = [r for r in rs if r["stage"] == "parse"]
        if perr:
            print(f"  parse REFUSE                 : {len(perr)}")
            for r in perr:
                print("      ", r["repo"], r["path"], "->", str(r["detail"])[:80])
        print(f"  evidence questions answered  : {a}/{tot} ({100*a/tot:.0f}%)")
        for q in QUESTIONS:
            qa = sum(1 for r in rs if r["questions"][q]["status"] == "ANSWERED")
            rea = collections.Counter(
                r["questions"][q].get("reason") for r in rs
                if r["questions"][q]["status"] == "REFUSED"
                and not str(r["questions"][q].get("reason", "")).startswith("unresolved_templating"))
            extra = ("  | beyond templating: " + ", ".join(f"{k}={v}" for k, v in rea.most_common(2))) if rea else ""
            print(f"     {q:<20} {qa:>3}/{N}{extra}")
        ok = [r for r in rs if r["stage"] == "analysed"]
        if ok:
            a2 = sum(1 for r in ok for q in QUESTIONS if r["questions"][q]["status"] == "ANSWERED")
            print(f"  among analysed models        : {a2}/{len(ok)*len(QUESTIONS)} ({100*a2/(len(ok)*len(QUESTIONS)):.0f}%)")

    print(f"corpus N={len(rows)}  pinned dialect={PRIMARY_DIALECT}  sqlglot={sqlglot.__version__}")
    block("ALL", rows)
    for st in ("application", "package"):
        block(st, [r for r in rows if r["stratum"] == st])

    ok = [r for r in rows if r["stage"] == "analysed"]
    fo = [r for r in ok if r["evidence"]["fanout_shape"]]
    print(f"\nmulti-child join shape on one parent key: {len(fo)}/{len(ok)} analysed corpus models")
    for r in fo:
        print(f"   [{r['stratum']}] {r['path'].split('/')[-1]}  parents={list(r['evidence']['fanout_shape'])[:2]}")

    # compact table as printed in the paper
    def pair(rs):
        return (sum(1 for r in rs if r["stage"] == "analysed"), len(rs),
                sum(1 for r in rs for q in QUESTIONS if r["questions"][q]["status"] == "ANSWERED"),
                len(rs) * len(QUESTIONS))
    app = [r for r in rows if r["stratum"] == "application"]
    pkg = [r for r in rows if r["stratum"] == "package"]
    okr = ok
    print("\n=== paper table ===")
    print(f"{'slice':<24}{'analysed':>10}{'answered':>12}")
    for lbl, rs in (("All", rows), ("Application repos", app), ("Package repos", pkg)):
        p_, pn, a_, an_ = pair(rs)
        print(f"{lbl:<24}{f'{p_}/{pn}':>10}{f'{a_}/{an_}':>12}")
    _, _, a_, an_ = pair(okr)
    print(f"{'Analysed models only':<24}{'-':>10}{f'{a_}/{an_}':>12}")
    tmpl_ref = sum(1 for r in rows for q in QUESTIONS
                   if str(r["questions"][q].get("reason", "")).startswith("unresolved_templating"))
    total_ref = sum(1 for r in rows for q in QUESTIONS if r["questions"][q]["status"] == "REFUSED")
    print(f"\n{tmpl_ref}/{total_ref} refusals arose before SQL analysis, during unresolved dbt templating.")


if __name__ == "__main__":
    main()
