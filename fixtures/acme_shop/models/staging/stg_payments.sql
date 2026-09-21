with source as (

    select * from {{ source('raw', 'payments') }}

),

renamed as (

    select
        payment_id,
        order_id,
        lower(payment_method)                                       as payment_method,
        lower(status)                                               as payment_status,
        amount_cents,
        {{ cents_to_euro('amount_cents') }}                         as amount,

        -- what the payment contributes to the order balance:
        -- a success adds, a refund subtracts, a failed attempt adds nothing
        iff(lower(status) = 'success', 1,
            iff(lower(status) = 'refunded', -1, 0))
            * {{ cents_to_euro('amount_cents') }}                   as net_amount,

        paid_ts::timestamp_ntz                                      as paid_ts,
        paid_ts::date                                               as paid_date

    from source

)

select * from renamed
