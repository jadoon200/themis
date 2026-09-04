{#
    Write semantics for partitioned tables.

    Hive-style partition overwrite is a session setting, and only an engine that has
    partitions understands it. The demo project builds on DuckDB and on Trino's memory
    connector, neither of which does, so the macro emits the setting only where it
    means something and a harmless no-op everywhere else.

    Wrapping it is also the realistic shape: a project of any size keeps write
    semantics in one macro rather than repeating the setting in forty configs. dbt
    records the *unrendered* hook, so what a reviewer's rules see is the call, not the
    setting -- which is exactly why hook inspection has to resolve macros.
#}
{% macro partition_overwrite_hook() %}
{%- if target.type == 'trino' and target.catalog == 'hive' -%}
set session hive.insert_existing_partitions_behavior = 'OVERWRITE'
{%- else -%}
select 1
{%- endif -%}
{% endmacro %}
