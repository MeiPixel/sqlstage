DIALECT_NAMES = {"sqlite": "SQLite", "mysql": "MySQL", "postgres": "PostgreSQL"}


def direct_sql_messages(question, schema_text, guide_text="", fewshot_text="", variant_hint="", dialect="sqlite",
                        history=()):
    name = DIALECT_NAMES.get(dialect, dialect)
    return [
        {
            "role": "system",
            "content": (
                f"你是Text2SQL专家。根据数据库结构生成一条{name}只读查询。"
                "保留问题要求的全部条件与返回字段，不增加额外条件。只输出SQL。\n\n"
                + schema_text + guide_text + fewshot_text + "\n\n## 当前生成策略\n" + variant_hint
            ),
        },
        *history,
        {"role": "user", "content": question},
    ]


def orm_messages(question, schema_text, orm_config, history=(), guide_text="", fewshot_text=""):
    return [
        {
            "role": "system",
            "content": (
                "你是Text2SQL规划器。输出JSON查询计划，不要输出SQL或解释。"
                "格式必须包含select和from，可包含joins、filters、group_by、order_by、limit。"
                "filters元素格式为field/op/value，joins元素格式为table/on。\n\n"
                f"字段联动规则：{orm_config.link_rules}\n"
                f"值映射：{orm_config.value_maps}\n单位规则：{orm_config.unit_rules}\n"
                f"字段依赖：{orm_config.required_fields}\n互斥字段：{orm_config.mutex_fields}\n"
                f"派生字段：{orm_config.derived_fields}\n指标口径：{orm_config.metrics}\n"
                f"JOIN规则：{orm_config.join_rules}\n\n数据库结构：\n{schema_text}"
                + guide_text + fewshot_text
            ),
        },
        *history,
        {"role": "user", "content": question},
    ]
