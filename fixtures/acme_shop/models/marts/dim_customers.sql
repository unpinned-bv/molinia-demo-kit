-- One row per person. Accounts sharing an e-mail address are the same person:
-- the earliest signup is kept as the customer, and the orders and web activity
-- of the later accounts are credited to it.

with customers as (

    select * from {{ ref('stg_customers') }}

),

account_map as (

    select
        customer_id,
        first_value(customer_id) over (
            partition by email
            order by signup_ts, customer_id
        )                                                   as canonical_customer_id,
        count(*) over (partition by email)                  as accounts
    from customers

),

deduplicated as (

    select *
    from customers
    qualify row_number() over (partition by email order by signup_ts, customer_id) = 1

),

order_stats as (

    select
        account_map.canonical_customer_id                               as customer_id,
        count(*)                                                        as lifetime_orders,
        count_if(orders.status = 'delivered')                           as delivered_orders,
        count_if(orders.status in ('cancelled', 'returned'))            as cancelled_or_returned_orders,
        count_if(orders.has_coupon)                                     as orders_with_coupon,
        sum(orders.net_revenue)                                         as lifetime_revenue,
        min(orders.order_date)                                          as first_order_date,
        max(orders.order_date)                                          as last_order_date
    from {{ ref('fct_orders') }} as orders
    inner join account_map
        on account_map.customer_id = orders.customer_id
    group by account_map.canonical_customer_id

),

web_stats as (

    select
        account_map.canonical_customer_id                               as customer_id,
        count_if(item_views.event_type = 'product_view')                as product_views,
        count_if(item_views.event_type = 'add_to_cart')                 as cart_adds
    from {{ ref('int_web_item_views') }} as item_views
    inner join account_map
        on account_map.customer_id = item_views.customer_id
    group by account_map.canonical_customer_id

)

select
    deduplicated.customer_id,
    deduplicated.first_name,
    deduplicated.last_name,
    deduplicated.email,
    deduplicated.country_code,
    deduplicated.region,
    deduplicated.signup_date,
    deduplicated.signup_cohort,
    deduplicated.marketing_opt_in,
    account_map.accounts - 1                                            as merged_accounts,
    zeroifnull(order_stats.lifetime_orders)                             as lifetime_orders,
    zeroifnull(order_stats.delivered_orders)                            as delivered_orders,
    zeroifnull(order_stats.cancelled_or_returned_orders)                as cancelled_or_returned_orders,
    zeroifnull(order_stats.orders_with_coupon)                          as orders_with_coupon,
    zeroifnull(order_stats.lifetime_revenue)::number(12, 2)             as lifetime_revenue,
    order_stats.first_order_date,
    order_stats.last_order_date,
    datediff(day, order_stats.last_order_date, '{{ var("as_of_date") }}'::date) as days_since_last_order,
    zeroifnull(web_stats.product_views)                                 as product_views,
    zeroifnull(web_stats.cart_adds)                                     as cart_adds,
    case
        when zeroifnull(order_stats.lifetime_orders) = 0 then 'prospect'
        when order_stats.lifetime_orders >= 15 then 'loyal'
        when datediff(day, order_stats.last_order_date, '{{ var("as_of_date") }}'::date) > 180 then 'lapsed'
        else 'active'
    end                                                                 as customer_segment

from deduplicated
inner join account_map
    on account_map.customer_id = deduplicated.customer_id
left join order_stats
    on order_stats.customer_id = deduplicated.customer_id
left join web_stats
    on web_stats.customer_id = deduplicated.customer_id
