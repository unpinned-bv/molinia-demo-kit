with source as (

    select * from {{ source('raw', 'customers') }}

),

renamed as (

    select
        customer_id,
        trim(first_name)                                            as first_name,
        trim(last_name)                                             as last_name,
        -- accounts are matched on e-mail, so normalise it once here
        lower(trim(email))                                          as email,
        upper(trim(country_code))                                   as country_code,
        iff(upper(trim(country_code)) in ('NL', 'BE', 'LU'),
            'Benelux', 'Rest of EU')                                as region,
        signup_ts::timestamp_ntz                                    as signup_ts,
        signup_ts::date                                             as signup_date,
        to_varchar(signup_ts, 'YYYY-MM')                            as signup_cohort,
        datediff(day, signup_ts::date, '{{ var("as_of_date") }}'::date) as account_age_days,
        nvl(marketing_opt_in, false)                                as marketing_opt_in

    from source

)

select * from renamed
