{{ config(cluster_by=['order_date']) }}

with orders as (

    select * from {{ ref('stg_orders') }}

),

customers as (

    select customer_id, country_code from {{ ref('stg_customers') }}

),

order_lines as (

    select
        order_id,
        count(*)                    as item_count,
        sum(quantity)               as units,
        sum(gross_amount)           as gross_amount,
        sum(discount_amount)        as discount_amount,
        sum(line_amount)            as items_amount
    from {{ ref('int_order_lines') }}
    group by order_id

),

order_payments as (

    select
        order_id,
        count(*)                                                as payment_attempts,
        count_if(payment_status = 'failed')                     as failed_payments,
        sum(net_amount)                                         as paid_amount,
        max(iff(payment_status = 'success', paid_ts, null))     as paid_ts
    from {{ ref('stg_payments') }}
    group by order_id

),

joined as (

    select
        orders.order_id,
        orders.customer_id,
        customers.country_code,
        orders.order_ts,
        orders.order_date,
        orders.order_month,
        orders.status,
        orders.channel,
        orders.device,
        orders.shipping_method,
        orders.coupon_code,
        orders.is_gift,
        zeroifnull(order_lines.item_count)                                  as item_count,
        zeroifnull(order_lines.units)                                       as units,
        zeroifnull(order_lines.gross_amount)::number(12, 2)                 as gross_amount,
        zeroifnull(order_lines.discount_amount)::number(12, 2)              as discount_amount,
        zeroifnull(order_lines.items_amount)::number(12, 2)                 as items_amount,
        zeroifnull(orders.shipping_cost)::number(12, 2)                     as shipping_amount,
        (zeroifnull(order_lines.items_amount)
            + zeroifnull(orders.shipping_cost))::number(12, 2)              as order_total,
        zeroifnull(order_payments.paid_amount)::number(12, 2)               as paid_amount,
        zeroifnull(order_payments.payment_attempts)                         as payment_attempts,
        zeroifnull(order_payments.failed_payments)                          as failed_payments,
        order_payments.paid_ts

    from orders
    left join customers
        on customers.customer_id = orders.customer_id
    left join order_lines
        on order_lines.order_id = orders.order_id
    left join order_payments
        on order_payments.order_id = orders.order_id

)

select
    order_id,
    customer_id,
    country_code,
    order_ts,
    order_date,
    order_month,
    status,
    channel,
    device,
    shipping_method,
    coupon_code,
    iff(coupon_code is null, false, true)                                   as has_coupon,
    is_gift,
    item_count,
    units,
    gross_amount,
    discount_amount,
    items_amount,
    shipping_amount,
    order_total,
    paid_amount,
    payment_attempts,
    failed_payments,
    paid_ts,
    -- settled when the net payments cover the order (an empty order never is)
    iff(div0(paid_amount, order_total) >= 1, true, false)                   as is_fully_paid,
    -- cancelled and returned orders do not count as revenue
    iff(status in ('cancelled', 'returned'), 0, order_total)::number(12, 2) as net_revenue

from joined
