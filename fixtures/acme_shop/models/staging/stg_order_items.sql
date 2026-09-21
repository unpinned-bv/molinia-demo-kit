with source as (

    select * from {{ source('raw', 'order_items') }}

),

renamed as (

    select
        order_item_id,
        order_id,
        product_id,
        quantity,
        unit_price,
        discount_pct,

        -- Money is exact NUMBER arithmetic; a line is rounded to the cent once,
        -- half away from zero, exactly as the invoice does it.
        quantity * unit_price                                                   as gross_amount,
        round(quantity * unit_price * (1 - discount_pct / 100), 2)              as line_amount,
        quantity * unit_price
            - round(quantity * unit_price * (1 - discount_pct / 100), 2)        as discount_amount

    from source

)

select * from renamed
