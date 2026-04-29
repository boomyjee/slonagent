"""AST-based introspection of Python files in tools/ — превращает классы Skill
с @tool-декораторами в JSON-schema tool-объявления для LLM, не импортируя их.

Используется SandboxSkill для автоматической регистрации скриптов из
<workspace_dir>/tools/ как функций, доступных модели.
"""
import ast


_AST_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


def introspect(path: str) -> list[dict]:
    """Парсит Python-файл, возвращает список tool-dicts для всех @tool-методов
    у классов Skill. Имя tool'а — `<class-prefix>_<method>`, где prefix — имя
    класса без суффиксов Skill/Memory/Provider, в lower-case."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read())
    except SyntaxError:
        return []

    tools = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(
            (isinstance(b, ast.Name) and b.id == "Skill") or
            (isinstance(b, ast.Attribute) and b.attr == "Skill")
            for b in node.bases
        ):
            continue
        prefix = node.name.removesuffix("Skill").removesuffix("Memory").removesuffix("Provider").lower()
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            desc = _tool_desc(item)
            if desc is None:
                continue
            props, required = _parse_params(item)
            tools.append({
                "type": "function",
                "function": {
                    "name": f"{prefix}_{item.name}",
                    "description": desc,
                    "parameters": {"type": "object", "properties": props, "required": required},
                },
            })
    return tools


def _tool_desc(func: ast.FunctionDef) -> str | None:
    """Достаёт первый строковый аргумент @tool(...). Возвращает None если декоратора нет."""
    for dec in func.decorator_list:
        if isinstance(dec, ast.Call):
            fn = dec.func
            if (isinstance(fn, ast.Name) and fn.id == "tool") or \
               (isinstance(fn, ast.Attribute) and fn.attr == "tool"):
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    return dec.args[0].value
    return None


def _parse_params(func: ast.FunctionDef) -> tuple[dict, list]:
    props: dict = {}
    required: list = []
    args = func.args
    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        if arg.arg == "self":
            continue
        props[arg.arg] = _annotation_to_schema(arg.annotation)
        if i < defaults_offset:
            required.append(arg.arg)
    return props, required


def _annotation_to_schema(node) -> dict:
    if node is None:
        return {"type": "string"}
    # Annotated[type, "desc"]
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Annotated":
        elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
        base = elts[0]
        desc = elts[1].value if len(elts) > 1 and isinstance(elts[1], ast.Constant) else None
        schema = _base_type_schema(base)
        if desc:
            schema["description"] = desc
        return schema
    return _base_type_schema(node)


def _base_type_schema(node) -> dict:
    if isinstance(node, ast.Name):
        return {"type": _AST_TYPES.get(node.id, "string")}
    # list[str] и подобные
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "list":
        item_type = "string"
        if isinstance(node.slice, ast.Name):
            item_type = _AST_TYPES.get(node.slice.id, "string")
        return {"type": "array", "items": {"type": item_type}}
    return {"type": "string"}
