-- Every order's items_amount in fct_orders must equal the sum of its line
-- amounts in stg_order_items, to the cent. Orders without lines must be 0.

with lines as (

    select
        order_id,
        sum(line_amount) as items_amount
    from {{ ref('stg_order_items') }}
    group by order_id

)

select
    fct_orders.order_id,
    fct_orders.items_amount,
    zeroifnull(lines.items_amount) as expected_items_amount
from {{ ref('fct_orders') }} as fct_orders
left join lines
    on lines.order_id = fct_orders.order_id
where fct_orders.items_amount <> zeroifnull(lines.items_amount)
