#!/usr/bin/env python3
"""Regression suite for the SAGE evidence adapter.

Every case here is a bug adversarial review found, or a behaviour that review
asked to be pinned down. The direction that matters is uniform: where the
adapter cannot establish something, it must REFUSE rather than answer.

Run:  python3 test_adapter.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sage_adapter import run

FAILURES = []


def check(name, sql, **expect):
    r = run(sql)
    ev = r.get("evidence", {})
    q = r.get("questions", {})
    got = {
        "stage": r["stage"],
        "detail": r.get("detail"),
        "grain": ev.get("grain_construct"),
        "fanout": ev.get("fanout_shape"),
        "cols": ev.get("columns_at_multiplied_grain"),
        "sources": ev.get("source_relations"),
        "outputs": ev.get("output_columns"),
        **{k: q[k]["status"] for k in q},
        **{k + "_reason": q[k].get("reason") for k in q},
    }
    bad = {k: (v, got.get(k)) for k, v in expect.items() if got.get(k) != v}
    print(("  ok   " if not bad else "  FAIL ") + name)
    for k, (want, have) in bad.items():
        print(f"         {k}: expected {want!r}, got {have!r}")
    if bad:
        FAILURES.append(name)


print("templating — guessing an expansion is a role violation")
check("bare var refuses",
      "select a from {{ ref('x') }} where d = '{{ var(\"d\") }}'",
      stage="templating", detail="project_variable")
check("DEFAULTED var also refuses (returning the default invents a relation)",
      "select a from {{ source('raw', var('t','orders_v1')) }}",
      stage="templating", detail="project_variable")
check("unheld macro refuses",
      "select {{ dbt_utils.star(ref('x')) }} from {{ ref('x') }}",
      stage="templating", detail="undefined_macro")
check("target config is its own category, not a macro",
      "select a from {{ ref('x') }} where env = '{{ target.name }}'",
      stage="templating", detail="target_or_env_config")
check("ref/source/control flow resolve",
      "{% set cols = ['a','b'] %}select {% for c in cols %}{{ c }},{% endfor %} 1 "
      "from {{ ref('x') }}", stage="analysed")

print("grain — must come from the grain-determining scope only")
check("sibling-CTE GROUP BY is NOT the output's grain",
      """with agg as (select k, count(*) n from t group by k),
              plain as (select id, k from u)
         select p.id, a.n from plain p left join agg a on a.k = p.k""",
      grain="base_relation_times_join_multiplicity", E1_grain_construct="ANSWERED")
check("pass-through CTE chain is followed to the deciding scope",
      "with a as (select k, sum(v) s from t group by k), final as (select * from a) "
      "select * from final", grain="group_by")
check("pass-through SUBQUERY is followed, not called base_relation",
      "select * from (select a, sum(b) v from t group by a) x", grain="group_by")
check("top-level set operation refuses rather than picking an arm",
      "select a from x union all select a from y",
      E1_grain_construct="REFUSED", E2_join_structure="REFUSED",
      E5_relation_lineage="REFUSED")
check("set operation ONE LEVEL INSIDE A CTE also refuses (does not pick arm 1)",
      """with u as (select k, count(*) n from a group by k
                    union all
                    select k, n from b)
           select * from u""",
      E1_grain_construct="REFUSED",
      E1_grain_construct_reason="set_operation_arms_not_reconciled")
check("comma join is not cleared as a keyed join",
      "select a.x from a, b where a.id = b.id",
      E2_join_structure="REFUSED", E3_fanout_measure="REFUSED")

print("join structure — only a conjunction of qualified column equalities")
check("USING is read, not refused as a missing ON",
      "select r.id from receipt r left join items using (id)",
      E2_join_structure="ANSWERED")
check("unqualified join key refuses",
      "select rk from receipt left join items i on i.rk = rk left join tenders t on t.rk = rk",
      E2_join_structure="REFUSED", E2_join_structure_reason="join_key_not_qualified")
check("equality AND range predicate refuses (range must not be dropped)",
      "select r.id from receipt r join items i on i.rid = r.id and i.ts <= r.ts",
      E2_join_structure="REFUSED", E2_join_structure_reason="join_condition_not_equality")
check("equalities under OR refuse (not conjunctive keys)",
      "select r.id from receipt r join items i on i.rid = r.id or i.alt = r.id",
      E2_join_structure="REFUSED", E2_join_structure_reason="join_condition_not_equality")
check("join with no condition refuses",
      "select a.x from a join b join c on true",
      E2_join_structure="REFUSED", E2_join_structure_reason="join_without_on_condition")
check("CROSS JOIN refuses: unconditional multiplication, no key to report",
      "select r.receipt_id, r.receipt_total, i.item_id from receipt r cross join items i",
      E2_join_structure="REFUSED", E2_join_structure_reason="cross_join_unkeyed",
      E3_fanout_measure="REFUSED")
check("USING on a column merged by an earlier USING is read",
      "select r.id from receipt r left join items using (id) left join tenders using (id)",
      E2_join_structure="ANSWERED")
check("USING naming a column that may live on an intermediate relation refuses",
      "select a.id from a join b on a.id = b.id join c using (bkey)",
      E2_join_structure="REFUSED", E2_join_structure_reason="join_key_not_qualified")

print("fan-out — a shape, never a cardinality")
check("canonical chasm trap detected",
      """select r.receipt_id, r.receipt_total, i.item_id, t.tender_id
           from receipt r
           left join items i   on i.receipt_id = r.receipt_id
           left join tenders t on t.receipt_id = r.receipt_id""",
      fanout={"r.receipt_id": ["i", "t"]},
      # every relation in a shared-key set sits at a multiplied grain relative
      # to its own; which one carries the measure is the contract's call
      cols=["i.item_id", "r.receipt_id", "r.receipt_total", "t.tender_id"])
check("parent arriving via JOIN rather than FROM is still detected",
      """select r.receipt_id, r.receipt_total
           from items i
           join receipt r on i.receipt_id = r.receipt_id
           join tenders t on t.receipt_id = r.receipt_id""",
      fanout={"r.receipt_id": ["i", "t"]})
check("joins in sibling CTE scopes are NOT merged into a false shape",
      """with a as (select p.id, c1.v from p join c1 on p.id = c1.id),
              b as (select p.id, c2.v from p join c2 on p.id = c2.id)
         select a.id, a.v as v1, b.v as v2 from a join b on a.id = b.id""",
      fanout={})
check("no fan-out when the measure stays at its own grain",
      "select r.receipt_id, r.receipt_total from receipt r", fanout={})
# Documented decision, not an accident: an equality CHAIN a.id=b.id=c.id does
# establish that all three share one key, so b is reported as the shared-key
# relation. Whether a and c are non-unique -- i.e. whether this is a real
# fan-out -- is cardinality, which only the contract states.
check("transitive equality chain reports the shared key, leaving cardinality open",
      "select a.id from a join b on a.id = b.id join c on b.id = c.id",
      fanout={"b.id": ["a", "c"]})

check("fan-out upstream of the grain scope refuses: empty shape is not absence",
      """with f as (select r.receipt_id, r.receipt_total, i.item_id
                     from receipt r
                     left join items i   on i.receipt_id = r.receipt_id
                     left join tenders t on t.receipt_id = r.receipt_id),
              agg as (select receipt_id, sum(receipt_total) total from f group by receipt_id)
         select receipt_id, total from agg""",
      E3_fanout_measure="REFUSED",
      E3_fanout_measure_reason="joins_upstream_of_grain_scope")
check("a star hiding the projection under fan-out refuses",
      """with r as (select receipt_id, receipt_total from stg_receipt),
              i as (select receipt_id, item_id from stg_items),
              t as (select receipt_id, tender_id from stg_tenders)
         select r.* from r
           left join i on i.receipt_id = r.receipt_id
           left join t on t.receipt_id = r.receipt_id""",
      E3_fanout_measure="REFUSED",
      E3_fanout_measure_reason="fanout_columns_hidden_by_star")

print("lineage — no inventing what a catalog would be needed for")
check("a dead CTE does not contribute to provenance",
      "with unused as (select * from never_read_table) select id from real_table",
      sources=["real_table"])
check("star over an external relation refuses",
      "with p as (select * from stg_products) select * from p",
      E5_relation_lineage="REFUSED",
      E5_relation_lineage_reason="star_unresolved:star_over_external_relation")
check("star over an ALIASED in-query CTE resolves",
      "with base as (select id, v from t) select * from base b2",
      E5_relation_lineage="ANSWERED", outputs=["id", "v"])
check("catalog/schema qualification is preserved, not collapsed",
      "select a.id from prod.sales.orders a join stage.sales.orders b on a.id = b.id",
      sources=["prod.sales.orders", "stage.sales.orders"])
check("the dbt idiom `with x as (select * from x)` keeps x as an EXTERNAL source",
      "with orders as (select * from orders) select id from orders",
      sources=["orders"])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("all regression cases pass")
