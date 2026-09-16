"""渲染层：把 Markdown 结构转成 QQ 可读的形式（表格 → PNG）。"""

from agentcore.render.table import render_table_png, split_tables

__all__ = ["render_table_png", "split_tables"]
