import json
import re
from copy import deepcopy


def apply_link_rules(filters, rules):
    result = list(filters)
    for rule in rules:
        trigger = rule["when"]
        if trigger in result:
            for item in rule["then"]:
                if item not in result:
                    result.append(item)
    return result


def convert_unit_value(field, value, unit_rules):
    if not isinstance(value, str) or field not in unit_rules:
        return value
    match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z\u4e00-\u9fff]+)\s*", value)
    if not match:
        return value
    number, unit = float(match.group(1)), match.group(2)
    converted = number * unit_rules[field][unit]
    return int(converted) if converted.is_integer() else converted


def expand_named_expressions(select, named):
    return [
        f"{named[item]} AS {item}" if item in named else item
        for item in select
    ]


def _lookup(mapping, table, field):
    exact = mapping.get(f"{table}.{field}")
    if exact is not None:
        return exact
    if field in mapping:
        return mapping[field]
    hits = [value for key, value in mapping.items() if key.endswith("." + field)]
    return hits[0] if len(hits) == 1 else None


def _resolve_alias(field_alias, table, field):
    for key, aliases in field_alias.items():
        if field in aliases:
            return key.split(".", 1)[-1]
    return field


def _in_field_list(fields, table, field):
    return f"{table}.{field}" in fields or field in fields


def apply_orm_rules(plan, config):
    result = deepcopy(plan)
    table = result["from"].split()[0]
    filters = apply_link_rules(result.get("filters", []), config.link_rules)
    result["filters"] = filters
    for item in filters:
        item["field"] = _resolve_alias(config.field_alias, table, item["field"])
        mapping = _lookup(config.value_maps, table, item["field"]) or {}
        if item["value"] in mapping:
            item["value"] = mapping[item["value"]]
        item["value"] = convert_unit_value(item["field"], item["value"], config.unit_rules)
        if item["op"] == "=":
            match_mode = _lookup(config.field_match, table, item["field"])
            if match_mode == "contains":
                item["op"], item["value"] = "LIKE", f"%{item['value']}%"
            elif match_mode == "startswith":
                item["op"], item["value"] = "LIKE", f"{item['value']}%"
    used = set(result.get("select", [])) | {item["field"] for item in filters}
    for field in list(used):
        for required in config.required_fields.get(field, []):
            if required not in result["select"]:
                result["select"].append(required)
    for pair in config.mutex_fields:
        assert len(set(pair) & {item["field"] for item in filters}) < 2, f"互斥字段同时出现: {pair}"
    for rule in config.join_rules:
        if rule["trigger_field"] in used and rule["join"] not in result.get("joins", []):
            result.setdefault("joins", []).append(rule["join"])
    for item in filters:
        if _in_field_list(config.numeric_cast, table, item["field"]):
            item["field"] = f"CAST({item['field']} AS REAL)"
    named = {**config.derived_fields, **config.metrics}
    result["select"] = expand_named_expressions(result.get("select", []), named)
    return result


def sql_value(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    if value in {"CURRENT_DATE", "CURRENT_TIMESTAMP"}:
        return value
    return "'" + str(value).replace("'", "''") + "'"


def qualify_table(table, prefix):
    if not prefix:
        return table
    parts = table.split(maxsplit=1)
    return prefix + parts[0] + (" " + parts[1] if len(parts) > 1 else "")


def quote_order_col(column):
    if column.startswith("`"):
        return column
    if "." in column:
        return ".".join(f"`{part}`" for part in column.split("."))
    return f"`{column}`"


def split_order(item):
    if isinstance(item, dict):
        direction = item.get("direction") or item.get("order")
        if item.get("desc"):
            direction = "DESC"
        if not direction:
            raise ValueError(f"排序项既没有 direction / order，也没有 desc：{item}")
        return item["field"], str(direction).upper()
    parts = item.split()
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1].upper()


def compile_orm(plan, table_prefix="", quote_order_by=False):
    select = ", ".join(plan["select"]) or "*"
    sql = f"SELECT {select} FROM {qualify_table(plan['from'], table_prefix)}"
    for join in plan.get("joins", []):
        sql += f" JOIN {qualify_table(join['table'], table_prefix)} ON {join['on']}"
    filters = plan.get("filters", [])
    if filters:
        sql += " WHERE " + " AND ".join(
            f"{item['field']} {item['op']} {sql_value(item['value'])}" for item in filters
        )
    if plan.get("group_by"):
        sql += " GROUP BY " + ", ".join(plan["group_by"])
    if plan.get("order_by"):
        parts = []
        for item in plan["order_by"]:
            column, direction = split_order(item)
            if quote_order_by:
                column = quote_order_col(column)
            parts.append(f"{column} {direction}" if direction else column)
        sql += " ORDER BY " + ", ".join(parts)
    if plan.get("limit") is not None:
        sql += f" LIMIT {int(plan['limit'])}"
    return sql


def parse_orm_plan(text):
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return json.loads(match.group(1) if match else text)
