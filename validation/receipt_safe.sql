-- Contract-conforming variant of receipt_fanout.sql.
-- A real control, not a trivial one: it JOINS a child, but pre-aggregates it to
-- receipt grain first, so no key is shared by two or more relations and the
-- measure is never emitted below its authorized grain.
with receipt as (select * from {{ ref('stg_receipt') }}),
     items   as (select * from {{ ref('stg_receipt_item') }}),
     item_agg as (
         select receipt_id, sum(item_amount) as items_total
         from items
         group by receipt_id
     ),
final as (
    select
        receipt.receipt_id,
        receipt.receipt_total,
        item_agg.items_total
    from receipt
    left join item_agg on item_agg.receipt_id = receipt.receipt_id
)
select * from final
