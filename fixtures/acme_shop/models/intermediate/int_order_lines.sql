with order_items as (

    select * from {{ ref('stg_order_items') }}

),

orders as (

    select * from {{ ref('stg_orders') }}

),

products as (

    select * from {{ ref('stg_products') }}

),

joined as (

    select
        order_items.order_item_id,
        order_items.order_id,
        orders.customer_id,
        orders.order_date,
        orders.order_month,
        orders.status                                   as order_status,
        orders.channel,
        order_items.product_id,
        products.sku,
        products.category_name,
        order_items.quantity,
        order_items.unit_price,
        order_items.discount_pct,
        order_items.gross_amount,
        order_items.discount_amount,
        order_items.line_amount,

        -- share of the order's value carried by this line; a ratio, so float is
        -- fine. Summing whole cents keeps the float sum exact in any order,
        -- so the share is identical on every run.
        ratio_to_report((order_items.line_amount * 100)::float)
            over (partition by order_items.order_id)            as share_of_order

    from order_items
    inner join orders
        on orders.order_id = order_items.order_id
    left join products
        on products.product_id = order_items.product_id

)

select * from joined
