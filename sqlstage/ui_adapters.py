def rule_rows(rows):
    return [
        {"when": {"field": row[0], "op": row[1], "value": row[2]},
         "then": [{"field": row[3], "op": row[4], "value": row[5]}]}
        for row in rows if row[0] and row[3]
    ]


def mapping_rows(rows):
    result = {}
    for field, source, target in rows:
        if field and source:
            result.setdefault(field, {})[source] = target
    return result


def unit_rows(rows):
    result = {}
    for field, unit, factor in rows:
        if field and unit:
            result.setdefault(field, {})[unit] = float(factor)
    return result


def dependency_rows(rows):
    result = {}
    for field, required in rows:
        if field and required:
            result.setdefault(field, []).append(required)
    return result


def mutex_rows(rows):
    return [[field for field in row if field] for row in rows if sum(bool(field) for field in row) > 1]


def named_expression_rows(rows):
    return {name: expression for name, expression in rows if name and expression}


def join_rows(rows):
    return [
        {"trigger_field": row[0], "join": {"table": row[1], "on": row[2]}}
        for row in rows if row[0] and row[1] and row[2]
    ]


def search_space(table_methods, table_ks, column_methods, candidate_counts, fewshot_ks):
    return {
        "tricks.table_recall": table_methods,
        "tricks.table_top_k": table_ks,
        "tricks.column_recall": column_methods,
        "tricks.candidate_count": candidate_counts,
        "tricks.fewshot_top_k": fewshot_ks,
    }
