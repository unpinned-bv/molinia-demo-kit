-- Monthly revenue by customer country, for the finance dashboard.

with order_facts as (

    select * from {{ ref('fct_orders') }}

),

monthly as (

    select
        order_month,
        country_code,
        count(*)                                                        as orders,
        count_if(status not in ('cancelled', 'returned'))               as revenue_orders,
        count(distinct customer_id)                                     as customers,
        sum(net_revenue)                                                as revenue,
        sum(discount_amount)                                            as discounts,
        listagg(distinct channel, ',') within group (order by channel)  as channels
    from order_facts
    group by order_month, country_code

)

select
    order_month,
    country_code,
    orders,
    revenue_orders,
    customers,
    revenue::number(14, 2)                                              as revenue,
    discounts::number(14, 2)                                            as discounts,
    -- average order value over orders that count as revenue, to the cent
    round(div0(revenue, revenue_orders), 2)                             as avg_order_value,
    -- the country's share of the month's revenue, in percent (float over
    -- whole cents, so the sum is exact and the result stable)
    round(100 * ratio_to_report((revenue * 100)::float)
                    over (partition by order_month), 2)                 as revenue_share_pct,
    channels

from monthly
