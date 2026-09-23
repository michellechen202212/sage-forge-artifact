# SAGE evidence adapter — prototype and feasibility study

Anonymized artifact accompanying the FORGE 2027 submission *"Beyond Passing Tests: Semantic Assurance for AI-Generated Data Transformations."*

Prototype for the **evidence side** of the SAGE assurance harness. It extracts
mechanical evidence from data-transformation code and **refuses explicitly**
when it cannot faithfully analyse a construct a requirement depends on.

Public dbt models supply no semantic authority, so nothing here is a claim
about their correctness. This measures what evidence is obtainable, not
whether the gate changes outcomes.

## Files
| file | what it is |
|---|---|
| `sage_adapter.py` | the adapter: templating pre-pass, parse, structural evidence, five evidence questions |
| `build_corpus.py` | fetches the 11 **commit-pinned** repositories and draws the fixed-seed stratified sample |
| `run_corpus.py` | runs the adapter over the sample and prints the tables in the paper |
| `test_adapter.py` | 31-case regression suite: every bug adversarial review found, plus the behaviours it asked to be pinned down |
| `requirements.txt` | pinned dependency versions |
| `sample.json` | corpus manifest, 40 models (20 application / 20 package), paths relative to the corpus root |
| `COMMITS.txt` | the pinned commit of each of the 11 repositories |
| `results.json` | per-model evidence and per-question verdicts |
| `RESULTS.txt` | the printed summary |
| `validation/` | three named cases, held **outside** the corpus |

## Reproduce
```
pip install -r requirements.txt   # sqlglot==30.18.0, jinja2==3.1.6, pyyaml==6.0.3
python3 build_corpus.py          # fetches each repo AT ITS PINNED SHA -> ./corpus
python3 run_corpus.py            # prints RESULTS.txt
python3 run_corpus.py --all      # whole-pool fan-out incidence
python3 test_adapter.py          # regression suite (no network, no corpus)
python3 sage_adapter.py <model.sql>      # single model, JSON to stdout
```
Both scripts take an optional corpus root (or `SAGE_CORPUS=/path`); the default
is `./corpus` beside the scripts. `build_corpus.py` fetches each repository by
SHA and aborts if the checkout does not match, so re-running cannot drift.

## What the five questions actually compute
Narrower than the architectural dimensions they correspond to — the prototype
is deliberately not the vision:

| question | computes | does **not** compute |
|---|---|---|
| `E1_grain_construct` | which construct fixes output grain (GROUP BY / DISTINCT / window-dedup / base×joins) | grain of external inputs |
| `E2_join_structure` | joins (incl. `USING`), keys, and whether ≥2 relations share one join key. Accepts **only a conjunction of qualified column-to-column equalities**; anything else refuses | key uniqueness — no catalog |
| `E3_fanout_measure` | the structural multi-child fan-out shape, and which columns of such a parent are projected | whether children are actually non-unique; general duplication analysis |
| `E4_time_columns` | columns whose **name** carries a time-like suffix, and their use in filters/windows | timestamp types, temporal semantics |
| `E5_relation_lineage` | source relations and output column names | column-level lineage, transformation paths, aggregation lineage |

## What it refuses, and why
Refusal is a first-class outcome. The adapter resolves dbt's relation
functions (`ref`, `source`, `this`) and executes template control flow, because
those expansions are fixed by the project graph and the model text. It refuses
when expansion depends on a **project variable** or a **macro body it does not
hold** — guessing would make the adapter a second semantic oracle, the role
violation the architecture exists to prevent. It refuses `SELECT *` over an
**external** relation, whose columns are genuinely unknown without a catalog.
One dialect is pinned with **no fallback reparsing**: a silent reparse under
another grammar would make "the pinned adapter" untrue.

## Headline result (N=40)
| slice | models analysed | evidence answered |
|---|---:|---:|
| All | 14/40 | 59/200 |
| Application repositories | 12/20 | 49/100 |
| Package repositories | 2/20 | 10/100 |
| Analysed models only | — | 59/70 |

**130 of the 141 refusals arose before SQL analysis, during unresolved dbt
templating** (16 undefined macros, 8 project variables, 2 target config). No
model that reached the parser failed to parse. Of the remaining 11: eight are
`SELECT *` over an external relation, three are fan-out questions whose joins
sit upstream of the grain-determining scope.

Two caveats on the 59/70: `E4_time_columns` has no refusal path in this
implementation, and `E1_grain_construct`'s only refusal (set operations) did
not occur — so 28 of the 59 answers follow from parsing alone.

**Whole-pool incidence** (`python3 run_corpus.py --all`): across all
**662 eligible models**, 59 are analysable and **8 exhibit the fan-out shape**
— of which 5 key on the packages' `source_relation` union discriminator, a
column every relation carries by construction that can never indicate a parent.
Precision, not recall, is the detector's open problem. And the package
figure is near-definitional: **six of the 20 package models contain no SQL text
at all**, being a single cross-warehouse macro call. That reports packaging
style, not analysis difficulty — and points the same way: a production harness
should consume *compiled* models.

## Known limits
- One pinned dialect; cross-dialect behaviour unmeasured.
- No catalog, so key uniqueness and external star expansion are unavailable.
- Corpus is public, small, and not industrial.
- **Scope-locality cuts both ways.** A fan-out confined to a CTE whose
  aggregate lives in a later scope is not visible to the grain scope, so an
  empty shape is never reported as absence — `E3` refuses with
  `joins_upstream_of_grain_scope` instead.
- Sampling is **stratified 20/20, not equal per repository**. Within each
  stratum the draw is spread across repositories subject to how many eligible
  models each has: application counts are 6/4/3/3/3/1, package counts 4 each.
  It is not proportional to pool size — fivetran holds ~577 of ~600 package
  models but contributes 12 of 20 package samples.
- Scope discipline: grain, joins and fan-out are read from the
  grain-determining select only. `find_all` over the whole tree merges sibling
  CTEs and manufactures shapes that exist in no single scope.
- The detector reports a **shape, not a cardinality**. It refuses on an
  unqualified join key rather than dropping it. Which relation in a shared-key
  set is the *parent*, and whether the others are non-unique, is the
  contract's to state — an equality chain `a.id=b.id=c.id` is reported as a
  shared key on `b.id`, not asserted to be a fan-out.
- Integration-test subprojects inside package repositories are excluded, so
  "package models" means the packages' own models.

## Changelog
Corrections found by adversarial review of the previous revision, all of which
moved the adapter toward refusing instead of guessing:

- `var(name, default)` previously returned the default silently, inventing
  source relations from values a project overrides. It now refuses either way.
- Grain was derived from whole-tree searches, so a `GROUP BY` in any sibling
  CTE was attributed to the output. It is now read from the grain-determining
  select, following pass-through CTE references.
- Fan-out keyed off "the side that is not the joined relation", missing the
  shape whenever the parent arrived via `JOIN` rather than `FROM`, and merged
  joins across CTE scopes into false positives. Now symmetric and scope-local.
- Unqualified join keys were dropped silently; they now refuse.
- `USING (...)` joins were refused as "no ON condition". Now read.
- `target.*` was bucketed as an undefined macro; it now has its own category.
- `select * from cte alias` refused as an external star; aliases now resolve.
- Source relations dropped catalog/schema qualification, collapsing
  `prod.sales.orders` and `stage.sales.orders` into one name.
- A pass-through **subquery** (`select * from (select … group by …) x`) was
  reported as `base_relation` instead of being followed to the scope that
  fixes grain.
- `ON a.id=b.id AND a.ts<=b.ts` was read as a plain equality join, silently
  dropping the range predicate; equalities under `OR` were treated as
  conjunctive keys. Both now refuse.

### Round-3 corrections
The default in the grain and join walks was ANSWER, with refusals enumerated
as exceptions, so every construct nobody had thought of yet was cleared
affirmatively. That default is now inverted: an unrecognised source construct
refuses. Specifically:

- A set operation reached **through a CTE** silently returned its first arm as
  the output's grain (`node.find(exp.Select)` on a `Union`). Now refuses.
- `CROSS JOIN` — unconditional multiplication — was exempted from the
  no-condition check and reported as a clean join with no fan-out. Now
  refuses with `cross_join_unkeyed`.
- A fan-out in one CTE followed by an aggregate in the next — the commonest
  dbt spelling of the paper's own worked example — reported an empty shape.
  Now refuses with `joins_upstream_of_grain_scope`.
- `USING(c)` after an earlier non-USING join attributed `c` to the FROM
  relation, inventing a column that may live on an intermediate relation. Now
  attributed only when the left side is unambiguous, else refuses.
- `columns_at_multiplied_grain` named only the shared-key relation's columns,
  so on an equality chain it missed the measure entirely. It now reports
  columns from every relation in the shared-key set, and refuses when a star
  hides the projection.
- `source_relations` was a whole-tree scan, so a dead CTE contributed to
  provenance. Now walks only what the final select reaches.
- A comma join is no longer cleared as a keyed join.
- `receipt_safe.sql` was a trivial control (it performed no joins at all). It
  now joins a pre-aggregated child, so it exercises the detector properly.
