"""Fersk Codex 公共接口；导入包本身不读取配置或启动客户端。"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fersk_codex.codex.codex_execution import FerskCodex, LiveTurn

__all__ = ["FerskCodex", "LiveTurn"]


def __getattr__(name: str):
    if name in __all__:
        from fersk_codex.codex import codex_execution as codex
        value = getattr(codex, name)
        globals()[name] = value
        return value
    raise AttributeError(f"模块 {__name__!r} 没有属性 {name!r}")
