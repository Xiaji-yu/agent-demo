"""双存储实现的**结构一致性**护栏（H3）。

为什么需要：``tests/test_store_contract.py`` 用同一批断言打两个实现，但 PG 参数在
没有 ``TEST_DATABASE_URL`` 时跳过；而 Python 的 ABC 只保证"方法存在"，参数改名或
默认值漂移仍能实例化成功，同一调用在两个后端却可能悄悄分叉（历史事故见
REVIEW-a604023..679c9b3）。这里用 AST 做静态签名比对——不连库，默认套件永远生效。

同时锁住同源 chunk 去重只保留**一份**实现（``_unique_chunk_indices``）：两侧原先
各写一遍，内存侧曾整段漏掉去重。
"""

import ast
from pathlib import Path

import pytest

import agentcore.memory.store as store_module
from agentcore.memory.store import _unique_chunk_indices

_TREE = ast.parse(Path(store_module.__file__).read_text(encoding="utf-8"))
_CLASSES = {node.name: node for node in _TREE.body if isinstance(node, ast.ClassDef)}


def _methods(class_name: str) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in _CLASSES[class_name].body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _signature(node) -> dict:
    args = node.args
    return {
        "positional": [arg.arg for arg in args.posonlyargs + args.args],
        "kwonly": [arg.arg for arg in args.kwonlyargs],
        "defaults": [ast.unparse(default) for default in args.defaults],
        "kwdefaults": {
            arg.arg: ast.unparse(default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
            if default is not None
        },
        "vararg": args.vararg.arg if args.vararg else None,
        "kwarg": args.kwarg.arg if args.kwarg else None,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }


def _is_abstract(node) -> bool:
    return any(
        (isinstance(d, ast.Name) and d.id == "abstractmethod")
        or (isinstance(d, ast.Attribute) and d.attr == "abstractmethod")
        for d in node.decorator_list
    )


ABSTRACT = {
    name: _signature(node)
    for name, node in _methods("BaseMemoryStore").items()
    if _is_abstract(node)
}

IMPLEMENTATIONS = ["InMemoryMemoryStore", "PgMemoryStore"]


def test_every_base_method_is_abstract_and_discovered():
    """L6（REVIEW-ee39c8e..e55cab5）：`>=40` 的下限挡不住「某个抽象方法因装饰器
    写法变化被漏识别」——那会让它从下面的覆盖/签名断言里静默消失。BaseMemoryStore
    目前是纯 ABC（44 个方法全是 abstractmethod），所以要求两者**集合相等**；
    将来若真的加了具体方法，请同时明确它是否需要双实现覆盖，再更新本守卫。"""
    declared = set(_methods("BaseMemoryStore"))
    assert set(ABSTRACT) == declared, (
        f"BaseMemoryStore 方法未被识别为抽象：{sorted(declared - set(ABSTRACT))}"
    )


@pytest.mark.parametrize("impl", IMPLEMENTATIONS)
def test_implementation_covers_every_abstract_method(impl):
    missing = sorted(set(ABSTRACT) - set(_methods(impl)))
    assert not missing, f"{impl} 未实现这些抽象方法：{missing}"


@pytest.mark.parametrize("impl", IMPLEMENTATIONS)
def test_signatures_match_base_contract(impl):
    methods = _methods(impl)
    mismatched = {
        name: {"base": expected, "impl": _signature(methods[name])}
        for name, expected in ABSTRACT.items()
        if _signature(methods[name]) != expected
    }
    assert not mismatched, f"{impl} 与 BaseMemoryStore 的签名分叉：{mismatched}"


def _calls(node, func_name: str) -> bool:
    return any(
        isinstance(call.func, ast.Name) and call.func.id == func_name
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    )


@pytest.mark.parametrize("impl", IMPLEMENTATIONS)
def test_kb_add_chunks_uses_shared_dedupe(impl):
    method = _methods(impl)["kb_add_chunks"]
    assert _calls(method, "_unique_chunk_indices"), (
        f"{impl}.kb_add_chunks 又写了一份去重逻辑；"
        "请改用 _unique_chunk_indices 保持两侧口径一致"
    )


@pytest.mark.asyncio
async def test_inmemory_dedupe_runs_through_shared_helper(monkeypatch):
    """L6：AST 只认「出现了一个叫 _unique_chunk_indices 的调用」，把去重逻辑
    **内联复制**进来仍能骗过它。这里用 spy 证明运行时真的经过共享实现。"""
    calls = []
    real = store_module._unique_chunk_indices

    def spy(chunks, existing):
        calls.append((list(chunks), set(existing)))
        return real(chunks, existing)

    monkeypatch.setattr(store_module, "_unique_chunk_indices", spy)
    store = store_module.InMemoryMemoryStore()
    added = await store.kb_add_chunks("s1", ["A", "A", "B"], [[1.0], [2.0], [3.0]])
    assert added == 2
    assert calls and calls[0][0] == ["A", "A", "B"], (
        "kb_add_chunks 没走共享去重函数（可能内联复制了一份逻辑）"
    )


def test_unique_chunk_indices_dedupes_existing_and_batch():
    assert _unique_chunk_indices(["A", "B", "A", "C"], {"B"}) == [(0, "A"), (3, "C")]
    assert _unique_chunk_indices(["A", "A"], set()) == [(0, "A")]
    assert _unique_chunk_indices([], {"A"}) == []
