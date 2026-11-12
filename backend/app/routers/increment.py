"""增量采集游标接口：维护增量游标，覆盖标记对齐、重置游标、停用游标等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.increment import IncrementService

router = APIRouter(prefix="/api/increment", tags=["增量采集游标"])

service = IncrementService()

LIST_FIELDS = ["游标编号", "来源标识", "增量字段", "当前水位", "上次水位", "增量条数", "采集时间", "是否完成"]
STATUSES = ["草稿", "采集中", "已对齐", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按游标编号检索"),
    status: str | None = Query(default=None, description="草稿、采集中、已对齐、已停用"),
    游标编号: str | None = Query(default=None, description="按游标编号过滤"),
    来源标识: str | None = Query(default=None, description="按来源标识过滤"),
    增量字段: str | None = Query(default=None, description="按增量字段过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按游标编号、来源标识、增量字段与状态过滤增量采集游标列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword or 游标编号,
        status=status,
        source=来源标识,
        field=增量字段,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/watermarks")
def list_watermarks() -> dict[str, Any]:
    """各来源的已提交水位台账：采集任务按来源标识读取当前水位，决定续作起点。"""
    items = service.list_watermarks()
    return {"module": "increment", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出增量采集游标清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "increment", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条增量游标明细（含水位台账、提交窗口与最近尝试）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"增量游标 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条增量游标，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="增量游标已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条增量游标执行标记对齐、重置游标、停用游标。

    标记对齐必须随动作带回来源标识、增量字段、当前水位、增量条数、采集时间，
    提交成功才替换旧水位；重复提交同一采集时间的窗口只落一次结果。
    """
    values = dict(payload.values)
    action = str(values.pop("action", "") or "").strip()
    entry, message, ok = service.run_action(entry_id, action, values)
    return ActionResult(ok=ok, message=message, entry=entry)
