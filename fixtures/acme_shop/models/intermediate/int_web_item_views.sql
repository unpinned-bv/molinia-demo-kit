-- One row per item shown in a web event (product views, cart adds, checkouts).
-- Events without items (page views) produce no rows.

with events as (

    select * from {{ source('raw', 'web_events') }}

),

products as (

    select product_id, sku from {{ ref('stg_products') }}

),

flattened as (

    select
        events.event_id,
        f.index                                             as item_position,   -- 0-based
        events.customer_id,
        events.event_ts::timestamp_ntz                      as event_ts,
        lower(events.event_type)                            as event_type,
        events.payload:session_id::string                   as session_id,
        upper(f.value:sku::string)                          as sku,
        f.value:qty::number                                 as quantity,
        events.payload:utm.source::string                   as utm_source,
        events.payload:utm.campaign::string                 as utm_campaign

    from events,
        lateral flatten(input => events.payload:items) f

)

select
    to_varchar(flattened.event_id) || '-' || to_varchar(flattened.item_position) as web_item_view_id,
    flattened.event_id,
    flattened.item_position,
    flattened.customer_id,
    flattened.event_ts,
    flattened.event_type,
    flattened.session_id,
    flattened.sku,
    products.product_id,
    flattened.quantity,
    flattened.utm_source,
    flattened.utm_campaign

from flattened
left join products
    on products.sku = flattened.sku
