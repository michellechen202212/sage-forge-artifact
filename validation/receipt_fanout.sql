-- The worked example from the paper: receipt joined to two independent children.
with receipt as (select * from {{ ref('stg_receipt') }}),
     items as (select * from {{ ref('stg_receipt_item') }}),
     tenders as (select * from {{ ref('stg_receipt_tender') }}),
final as (
    select
        receipt.receipt_id,
        receipt.receipt_total,
        items.item_id,
        items.item_amount,
        tenders.tender_id,
        tenders.tender_amount
    from receipt
    left join items   on items.receipt_id   = receipt.receipt_id
    left join tenders on tenders.receipt_id = receipt.receipt_id
)
select * from final
