-- rpt_monthly_revenue has exactly one row per (order_month, country_code).

select
    order_month,
    country_code,
    count(*) as row_count
from {{ ref('rpt_monthly_revenue') }}
group by order_month, country_code
having count(*) > 1
