#!/usr/bin/env python3
"""Fetch the pinned public dbt projects and draw the fixed-seed stratified sample.

Every repository is pinned to an explicit commit below, fetched by SHA, so the
corpus is byte-identical on re-run. Writes sample.json (paths relative to the
corpus root) and verifies COMMITS.txt.

Usage:  python3 build_corpus.py [corpus_root]
        SAGE_CORPUS=/path python3 build_corpus.py
Default corpus root: ./corpus next to this script.
"""
import os, sys, glob, json, random, subprocess, collections

try:
    import yaml
except ImportError:
    sys.exit("pip install pyyaml")

SEED = 20261030
HERE = os.path.dirname(os.path.abspath(__file__))

# repo -> pinned commit. Fetched by SHA; re-running cannot drift.
PINS = {
    # application projects
    "make-open-data/make-open-data":                 "177caad0d3d21be49a287b787cb36803bfc4f203",
    "stacktonic-com/stacktonic-dbt-example-project": "c4e83f45d48b40e4a446dac82a0c42407ce3dd49",
    "gmyrianthous/dbt-dummy":                        "8cbe76f9a4d97d46fa4b27accaf74f5c3a989d6c",
    "dbt-labs/jaffle-shop":                          "5beb145b00f5465ec759cfcdd9745e858818cf95",
    "dbt-labs/mrr-playbook":                         "f7921cf7f67e57e8eb1ce86dc16fa3a9e03cee94",
    "dbt-labs/attribution-playbook":                 "54e5543f7fc914f0701c259b83d43be77be519bd",
    # package repositories
    "fivetran/dbt_shopify":                          "03e91d7aeb83151f968105f8580d13110f5375e4",
    "fivetran/dbt_hubspot":                          "439facd0cddafc1835ca7fe960e83c3a66e8f3f4",
    "fivetran/dbt_netsuite":                         "84bfa8ab446434e30df5cf3a06035abf10d2c343",
    "dbt-labs/snowplow":                             "82d63829766e4c2ef0dfa64f0a4acda563241441",
    "dbt-labs/dbt-event-logging":                    "7b289a58cfe5a0219d0a291f5439aa7c639076f2",
}
# The third validation case is a public model deliberately held OUTSIDE the
# corpus. Pinned and hash-verified here so its provenance is mechanically
# checkable rather than a claim in a comment.
VALIDATION = {
    "repo": "dbt-labs/jaffle_shop",
    "sha": "fd7bfacae4f497ff044a6a0275268676bf1b64c3",
    "path": "models/customers.sql",
    "sha256": "455b90a31f418ae776213ad9932c7cb72d19a5269a8c722bd9f4e44957313ce8",
    "local": "validation/preaggregated_children.sql",
    "header_lines": 5,
}

APPLICATION = [r for r in list(PINS)[:6]]
PACKAGE = [r for r in list(PINS)[6:]]

# Integration-test subprojects ship inside package repos but are test fixtures,
# not the package's own transformation models. Excluded so "package models"
# means what it says.
EXCLUDED_SUBPROJECTS = ("integration_test", "integration_tests")


def corpus_root():
    if len(sys.argv) > 1:
        return os.path.abspath(sys.argv[1])
    return os.path.abspath(os.environ.get("SAGE_CORPUS", os.path.join(HERE, "corpus")))


def fetch_pinned(repo, sha, root):
    """Fetch exactly the pinned commit."""
    d = os.path.join(root, repo.replace("/", "_"))
    head = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if head == sha:
        return d
    os.makedirs(d, exist_ok=True)
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "remote", "remove", "origin"],
                   capture_output=True)
    subprocess.run(["git", "-C", d, "remote", "add", "origin",
                    f"https://github.com/{repo}.git"], check=True)
    subprocess.run(["git", "-C", d, "fetch", "-q", "--depth", "1", "origin", sha], check=True)
    subprocess.run(["git", "-C", d, "checkout", "-q", "FETCH_HEAD"], check=True)
    got = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    if got != sha:
        sys.exit(f"{repo}: expected {sha}, got {got}")
    return d


def project_models(repo_dir):
    """Only .sql under paths the project itself declares as model-paths.

    Reading dbt_project.yml rather than assuming models/ matters: at least one
    corpus project keeps models outside models/ and macros under 5_macros/,
    which a path-substring filter silently misclassifies as models.
    """
    out = []
    for cfg in glob.glob(os.path.join(repo_dir, "**", "dbt_project.yml"), recursive=True):
        rel_cfg = os.path.relpath(cfg, repo_dir).replace(os.sep, "/")
        if "dbt_packages" in rel_cfg or "/target/" in rel_cfg:
            continue
        if any(x in rel_cfg for x in EXCLUDED_SUBPROJECTS):
            continue
        try:
            y = yaml.safe_load(open(cfg, encoding="utf-8", errors="replace")) or {}
        except Exception:
            y = {}
        mp = y.get("model-paths") or y.get("source-paths") or ["models"]
        excl = {os.path.normpath(v) for k in ("macro-paths", "snapshot-paths",
                "analysis-paths", "test-paths", "seed-paths", "data-paths")
                for v in (y.get(k) or [])}
        pdir = os.path.dirname(cfg)
        for m in mp:
            for f in glob.glob(os.path.join(pdir, m, "**", "*.sql"), recursive=True):
                rel = os.path.relpath(f, pdir)
                segs = rel.replace(os.sep, "/").split("/")
                if any(e in rel for e in excl):
                    continue
                if any(s in ("macros", "snapshots", "tests", "analysis", "analyses",
                             "target", "dbt_packages") or s in EXCLUDED_SUBPROJECTS
                       for s in segs):
                    continue
                if os.path.getsize(f):
                    out.append(f)
    return sorted(set(out))


def draw(pools, repos, n, rng):
    avail = {r: pools[r] for r in repos if pools.get(r)}
    per = max(1, n // len(avail))
    picked = [(r, f) for r, fs in sorted(avail.items())
              for f in rng.sample(fs, min(per, len(fs)))]
    chosen = set(picked)
    rest = [(r, f) for r, fs in sorted(avail.items()) for f in fs if (r, f) not in chosen]
    rng.shuffle(rest)
    return (picked + rest)[:n]


def verify_validation_case(root):
    """Fetch the held-out validation model and verify it byte-for-byte."""
    import hashlib
    d = fetch_pinned(VALIDATION["repo"], VALIDATION["sha"], root)
    upstream = open(os.path.join(d, VALIDATION["path"]), encoding="utf-8").read()
    local = open(os.path.join(HERE, VALIDATION["local"]), encoding="utf-8").read()
    body = "\n".join(local.split("\n")[VALIDATION["header_lines"]:])
    up_h = hashlib.sha256(upstream.encode()).hexdigest()
    lo_h = hashlib.sha256(body.encode()).hexdigest()
    if not (up_h == lo_h == VALIDATION["sha256"]):
        sys.exit(f"validation case mismatch: upstream={up_h} local={lo_h} "
                 f"expected={VALIDATION['sha256']}")
    print(f"validation case verified: {VALIDATION['repo']}@{VALIDATION['sha'][:10]} "
          f"{VALIDATION['path']} sha256 ok")


def main():
    root = corpus_root()
    os.makedirs(root, exist_ok=True)
    rng = random.Random(SEED)
    pools, lines = {}, []
    for repo, sha in PINS.items():
        d = fetch_pinned(repo, sha, root)
        key = repo.replace("/", "_")
        pools[key] = project_models(d)
        lines.append(f"{key} {sha}")
        print(f"{repo:<48} {len(pools[key]):>4} models @ {sha[:10]}")
    open(os.path.join(HERE, "COMMITS.txt"), "w").write("\n".join(lines) + "\n")

    sample = []
    for stratum, repos in (("application", [r.replace('/', '_') for r in APPLICATION]),
                           ("package", [r.replace('/', '_') for r in PACKAGE])):
        for r, f in draw(pools, repos, 20, rng):
            sample.append({"stratum": stratum, "repo": r,
                           # relative to the corpus root, so the manifest travels
                           "path": os.path.relpath(f, os.path.join(root, r)).replace(os.sep, "/"),
                           "bytes": os.path.getsize(f)})
    json.dump(sample, open(os.path.join(HERE, "sample.json"), "w"), indent=1)
    verify_validation_case(root)
    print("\nsample:", len(sample), dict(collections.Counter(x["stratum"] for x in sample)))
    print("corpus root:", root)


if __name__ == "__main__":
    main()
