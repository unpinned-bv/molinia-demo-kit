with source as (

    select * from {{ source('raw', 'products') }}

),

renamed as (

    select
        product_id,
        upper(trim(sku))                                    as sku,
        trim(product_name)                                  as product_name,
        upper(category_code)                                as category_code,
        decode(upper(category_code),
            'EL', 'Electronics',
            'HO', 'Home & Living',
            'SP', 'Sports',
            'BO', 'Books',
            'TO', 'Toys',
            'Other')                                        as category_name,
        -- a handful of products lost their list price in the ERP migration
        zeroifnull(unit_price)::number(10, 2)               as list_price,
        unit_price is null                                  as is_price_missing,
        is_active

    from source

)

select * from renamed
