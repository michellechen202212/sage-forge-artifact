# Validation cases

Held **outside** the 40-model corpus. No model in the 40-model sample exhibited
the fan-out shape; across the whole pinned pool 8 of 59 analysable models do
(`run_corpus.py <root> --all`).

| file | expected |
|---|---|
| `receipt_fanout.sql` | shape `receipt.receipt_id -> {items, tenders}`, with `receipt_total` among the columns projected from that set. The paper's FAIL condition, from artifact evidence alone. |
| `receipt_safe.sql` | a real control: it *does* join a child, but pre-aggregates it to receipt grain first, so no key is shared by two or more relations. No shape, `E3` answered. |
| `preaggregated_children.sql` | the **same** shape with pre-aggregated children — `dbt-labs/jaffle_shop@fd7bfac`, `models/customers.sql`, hash-verified by `build_corpus.py`. `E3` **refuses** (`joins_upstream_of_grain_scope`): joins inside those children sit upstream of the grain scope, so the adapter declines to conclude rather than clearing the model. |

Run: `python3 ../sage_adapter.py receipt_fanout.sql`
