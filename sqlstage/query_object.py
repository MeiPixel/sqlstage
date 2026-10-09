"""不可变查询对象：复用现有 ORM 编译器，安全解释增量操作与参数模板。"""
import ast
import re
from copy import deepcopy
from dataclasses import dataclass
import sqlglot
from sqlglot import exp

from .orm import apply_orm_rules, compile_orm
from .config import OrmConfig


@dataclass(frozen=True)
class QueryObject:
    _plan: dict

    def __post_init__(self):
        object.__setattr__(self, "_plan", deepcopy(self._plan))

    @property
    def plan(self):
        return deepcopy(self._plan)

    def filter(self, **conditions):
        operators = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=", "ne": "!=", "contains": "LIKE", "startswith": "LIKE"}
        plan = self.plan
        for lookup, value in conditions.items():
            parts = lookup.rsplit("__", 1)
            field, operator = (parts[0], operators[parts[1]]) if len(parts) == 2 else (lookup, "=")
            if lookup.endswith("__contains"):
                value = f"%{value}%"
            elif lookup.endswith("__startswith"):
                value = f"{value}%"
            plan.setdefault("filters", []).append({"field": field, "op": operator, "value": value})
        return QueryObject(plan)

    def select(self, *fields):
        plan = self.plan
        plan["select"] = list(fields)
        return QueryObject(plan)

    def where(self, field, op, value):
        if op.upper() not in {"=", "<", "<=", ">", ">=", "!=", "<>", "LIKE", "IS", "IS NOT"}:
            raise ValueError(f"不支持的条件操作：{op}")
        plan = self.plan
        plan.setdefault("filters", []).append({"field": field, "op": op.upper(), "value": value})
        return QueryObject(plan)

    def order_by(self, *fields):
        plan = self.plan
        plan["order_by"] = [{"field": field.lstrip("-"), "direction": "DESC" if field.startswith("-") else "ASC"} for field in fields]
        return QueryObject(plan)

    def group_by(self, *fields):
        plan = self.plan
        plan["group_by"] = list(fields)
        return QueryObject(plan)

    def limit(self, count):
        if not isinstance(count, int) or count < 0:
            raise ValueError("limit 必须为非负整数")
        plan = self.plan
        plan["limit"] = count
        return QueryObject(plan)

    def join(self, table, on):
        plan = self.plan
        plan.setdefault("joins", []).append({"table": table, "on": on})
        return QueryObject(plan)

    def sql(self, config=None):
        config = config or OrmConfig()
        return compile_orm(apply_orm_rules(self.plan, config), config.table_prefix, config.quote_order_by)


def apply_object_code(query, code):
    """只解释 query 的白名单方法与字面量，不执行生成的 Python。"""
    match = re.fullmatch(r"\s*```(?:python)?\s*(.*?)\s*```\s*", code, re.DOTALL)
    statements = ast.parse(match.group(1) if match else code).body
    if len(statements) != 1:
        raise ValueError("增量只允许一条 query 赋值")
    statement = statements[0]
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name) or statement.targets[0].id != "query":
        raise ValueError("必须输出 query = query.<操作>(...)")

    def interpret(node):
        if isinstance(node, ast.Name) and node.id == "query":
            return query
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr not in {"filter", "where", "select", "order_by", "group_by", "limit", "join"}:
            raise ValueError("对象操作不在白名单")
        if any(keyword.arg is None for keyword in node.keywords):
            raise ValueError("不允许展开关键字参数")
        owner = interpret(node.func.value)
        return getattr(owner, node.func.attr)(*[ast.literal_eval(arg) for arg in node.args],
                                             **{keyword.arg: ast.literal_eval(keyword.value) for keyword in node.keywords})
    return interpret(statement.value)


def parameterize(plan, filter_index, parameter):
    if not parameter.isidentifier():
        raise ValueError("参数名必须是有效标识符")
    result = deepcopy(plan)
    result["filters"][filter_index]["value"] = {"parameter": parameter}
    return result


def table(name):
    return QueryObject({"from": name, "select": ["*"]})


def bind_template(plan, values):
    result = deepcopy(plan)
    for item in result.get("filters", []):
        if isinstance(item["value"], dict):
            item["value"] = values[item["value"]["parameter"]]
    return QueryObject(result)


def edit_distance(left, right):
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        next_row = [i]
        for j, b in enumerate(right, 1):
            next_row.append(min(row[j] + 1, next_row[-1] + 1, row[j - 1] + (a != b)))
        row = next_row
    return row[-1]


def nearest_value(value, candidates, max_distance=2):
    """字符 n-gram 向量候选排序，再用编辑距离限制自动改写范围。"""
    def vector(text):
        text = text.casefold()
        return {text[i:i + n] for n in (1, 2) for i in range(len(text) - n + 1)}
    source = vector(value)
    def score(candidate):
        target = vector(candidate)
        return len(source & target) / (len(source) * len(target)) ** 0.5 if source and target else 0
    ranked = sorted(candidates, key=lambda candidate: (-score(candidate), candidate))
    eligible = [candidate for candidate in ranked if edit_distance(value.casefold(), candidate.casefold()) <= max_distance]
    return eligible[0] if eligible else value

def correct_vector_values(plan, fields, database):
    result, corrections = deepcopy(plan), []
    table = result["from"].split()[0]
    for item in result.get("filters", []):
        field = item["field"]
        distance = fields.get(f"{table}.{field}", fields.get(field))
        if distance is None or item["op"] != "=" or not isinstance(item["value"], str):
            continue
        candidates = [str(value) for value in database.distinct_values(table, field, 1000) if value is not None]
        original = item["value"]
        item["value"] = nearest_value(original, candidates, distance)
        if item["value"] != original:
            corrections.append({"field": field, "before": original, "after": item["value"]})
    return result, corrections


def describe_query(sql, schema, dialect):
    tree = sqlglot.parse_one(sql, read=dialect)
    tables = list(dict.fromkeys(table.name for table in tree.find_all(exp.Table) if table.name in schema))
    names = {column.name for column in tree.find_all(exp.Column)}
    star = tree.find(exp.Star) is not None
    columns = {table: [column["name"] for column in schema[table]["columns"] if star or column["name"] in names] for table in tables}
    return tables, columns


def follow_object(query, question, model, schema_text, request_started):
    model.begin_timing(request_started)
    # 既有查询只传一次；模型输出短操作，而非重写整个 SQL。
    messages = [{"role": "system", "content":
                 "你操作一个现有查询对象。只输出一行 query = query.<方法>(...)。"
                 "支持 filter(字段__lt/lte/gt/gte/ne/contains/startswith=字面量)、select(字段)、order_by(字段，负号降序)、group_by、limit、join(表,连接条件)。"
                 "保留已有条件；不输出SQL，不导入或执行其他代码。\n结构：" + schema_text},
                {"role": "user", "content": f"现有查询：{query.sql()}\n追加要求：{question}"}]
    raw = model.complete(messages)
    return apply_object_code(query, raw), raw, model.timing()

def render_object_code(plan):
    code = f"query = table({plan['from']!r})"
    for join in plan.get("joins", []):
        code += f".join({join['table']!r}, {join['on']!r})"
    operators = {"=": "", "<": "__lt", "<=": "__lte", ">": "__gt", ">=": "__gte", "!=": "__ne"}
    for item in plan.get("filters", []):
        field = item["field"] + operators.get(item["op"], "")
        if item["op"] not in operators:
            code += f".where({item['field']!r}, {item['op']!r}, {item['value']!r})"
        elif field.isidentifier():
            code += f".filter({field}={item['value']!r})"
        else:
            code += f".filter(**{{{field!r}: {item['value']!r}}})"
    code += ".select(" + ", ".join(repr(field) for field in plan["select"]) + ")"
    if plan.get("group_by"):
        code += ".group_by(" + ", ".join(repr(field) for field in plan["group_by"]) + ")"
    if plan.get("order_by"):
        from sqlstage.orm import split_order
        orders = [split_order(item) for item in plan["order_by"]]
        code += ".order_by(" + ", ".join(repr(("-" if direction == "DESC" else "") + field) for field, direction in orders) + ")"
    if plan.get("limit") is not None:
        code += f".limit({plan['limit']})"
    return code



