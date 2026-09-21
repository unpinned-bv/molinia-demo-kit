with source as (

    select * from {{ source('raw', 'orders') }}

),

renamed as (

    select
        order_id,
        customer_id,
        order_ts::timestamp_ntz                                         as order_ts,
        order_ts::date                                                  as order_date,
        date_trunc('month', order_ts)::date                             as order_month,
        -- the checkout service is not consistent about casing
        lower(trim(status))                                             as status,
        lower(channel)                                                  as channel,

        -- ORDER_META is written by the checkout service; every key is optional
        order_meta:coupon::string                                       as coupon_code,
        order_meta:device::string                                       as device,
        order_meta:shipping.method::string                              as shipping_method,
        try_to_number(order_meta:shipping.cost::string, 10, 2)          as shipping_cost,
        nvl(order_meta:gift::boolean, false)                            as is_gift,

        -- 30-day return window, counted from the order date
        dateadd(day, 30, order_ts::date)                                as return_deadline

    from source

)

select * from renamed
