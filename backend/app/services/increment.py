"""增量采集游标业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "increment"
REQUIRED_FIELDS = ["游标编号", "来源标识", "增量字段"]
STATUS_ORDER = ["草稿", "采集中", "已对齐", "已停用"]

# 动作 → 目标状态：对齐成功进入已对齐，重置回到草稿，停用进入已停用。
ACTION_RULES = {"标记对齐": "已对齐", "重置游标": "草稿", "停用游标": "已停用"}
NEGATIVE_ACTIONS = ["停用游标"]

# 每个动作允许从哪些状态发起：只能按 草稿 → 采集中 → 已对齐 → 已停用 推进，
# 重置游标是唯一的回退手段（回到草稿），已停用是终态。
ACTION_SOURCES = {
    "标记对齐": {"采集中", "已对齐"},
    "重置游标": {"采集中", "已对齐"},
    "停用游标": {"草稿", "采集中", "已对齐"},
}

# 标记对齐时页面必须带回的核对字段，缺字段只记尝试标记、不动水位。
ALIGN_REQUIRED = ["来源标识", "增量字段", "当前水位", "增量条数", "采集时间"]

# 提交窗口按（来源标识, 采集时间）归窗：重试请求即使跨日到达，仍归入原提交窗口。
_commit_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _window_key(source: str, collected_at: str) -> str:
    return f"{source}::{collected_at}"


def _ensure_runtime_fields(entry: dict[str, Any]) -> None:
    """兼容既有游标数据：老数据没有台账与窗口记录，首次动作时补齐。"""
    entry.setdefault("已提交窗口", [])
    entry.setdefault("水位台账", {})
    entry.setdefault("尝试次数", 0)
    entry.setdefault("最近尝试", None)


class IncrementService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        source: str | None = None,
        field: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("游标编号", ""))]
        if source:
            rows = [row for row in rows if source in str(row.get("来源标识", ""))]
        if field:
            rows = [row for row in rows if field in str(row.get("增量字段", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_watermarks(self) -> list[dict[str, Any]]:
        """各来源的已提交水位台账：采集任务按来源标识读取，决定从哪个水位续作。"""
        merged: dict[str, dict[str, Any]] = {}
        for row in store.rows(MODULE):
            ledger = row.get("水位台账") or {}
            for source, watermark in ledger.items():
                merged[source] = {"来源标识": source, "游标编号": row.get("游标编号"), **watermark}
            if not ledger and row.get("来源标识"):
                # 兼容既有数据：还没提交过窗口的游标，按行内水位先登记一份
                source = str(row["来源标识"])
                merged.setdefault(source, {
                    "来源标识": source,
                    "游标编号": row.get("游标编号"),
                    "当前水位": row.get("当前水位"),
                    "上次水位": row.get("上次水位"),
                    "增量条数": row.get("增量条数"),
                    "采集时间": row.get("采集时间"),
                })
        return list(merged.values())

    def watermark_for(self, source: str) -> dict[str, Any] | None:
        for item in self.list_watermarks():
            if item.get("来源标识") == source:
                return item
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        _ensure_runtime_fields(entry)
        rows.append(entry)
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        values: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"增量游标 {entry_id} 不存在或已归档", False
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于增量采集游标可执行范围", False
        current = str(entry.get("status") or STATUS_ORDER[0])
        if current not in ACTION_SOURCES[action]:
            return (
                None,
                f"当前状态「{current}」不允许{action}：增量游标只能按 草稿→采集中→已对齐→已停用 推进",
                False,
            )
        if action == "标记对齐":
            return self._align(entry, values or {})
        if action == "重置游标":
            return self._reset(entry)
        return self._disable(entry)

    def _align(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any], str, bool]:
        missing = [field for field in ALIGN_REQUIRED if not str(values.get(field) or "").strip()]
        if missing:
            reason = f"对齐提交缺少字段：{'、'.join(missing)}"
            self._mark_attempt(entry, reason, values)
            return entry, f"{reason}，本次只留下尝试标记，水位未变", False
        source = str(values["来源标识"]).strip()
        incr_field = str(values["增量字段"]).strip()
        registered_field = str(entry.get("增量字段") or "").strip()
        if incr_field != registered_field:
            reason = f"增量字段「{incr_field}」与登记的「{registered_field}」不一致"
            self._mark_attempt(entry, reason, values)
            return entry, f"{reason}，本次只留下尝试标记，水位未变", False
        current_level = str(values["当前水位"]).strip()
        count = str(values["增量条数"]).strip()
        collected_at = str(values["采集时间"]).strip()
        client_previous = str(values.get("上次水位") or "").strip()
        key = _window_key(source, collected_at)
        with _commit_lock:
            _ensure_runtime_fields(entry)
            committed: list[str] = entry["已提交窗口"]
            if key in committed:
                # 幂等：同一提交窗口（含跨日到达的重试）只落一次结果
                return (
                    entry,
                    f"提交窗口 {collected_at} 已落过结果，重复请求已忽略，"
                    f"水位保持 当前 {entry.get('当前水位')} / 上次 {entry.get('上次水位')}",
                    True,
                )
            ledger: dict[str, dict[str, Any]] = entry["水位台账"]
            stored_source = str(entry.get("来源标识") or "").strip()
            notes: list[str] = []
            if source != stored_source:
                # 换来源另起窗口：新来源不继承旧来源的上次水位
                previous = str(ledger.get(source, {}).get("当前水位") or "")
                notes.append(
                    f"来源标识由 {stored_source or '空'} 切换为 {source}，新来源另起窗口，不继承旧来源水位"
                )
            elif committed:
                # 已有提交记录：上次水位按服务端提交顺序平移
                previous = str(ledger.get(source, {}).get("当前水位") or "")
                if client_previous and client_previous != previous:
                    notes.append(
                        f"页面上次水位 {client_previous} 与服务端 {previous or '空'} 冲突，按提交顺序采用服务端水位"
                    )
            else:
                # 兼容既有数据：首次提交采用页面带回的上次水位作为基线
                previous = client_previous or str(entry.get("上次水位") or "").strip()
            entry["来源标识"] = source
            entry["当前水位"] = current_level
            entry["上次水位"] = previous
            entry["增量条数"] = count
            entry["采集时间"] = collected_at
            entry["是否完成"] = "是"
            ledger[source] = {
                "当前水位": current_level,
                "上次水位": previous,
                "增量条数": count,
                "采集时间": collected_at,
            }
            committed.append(key)
            entry["最近提交"] = {"提交窗口": key, "提交时间": _now_iso()}
            entry["status"] = "已对齐"
            entry["pending"] = False
            entry["abnormal"] = False
        message = f"增量游标已标记对齐：当前水位 {current_level}，上次水位 {previous or '空'}，采集时间 {collected_at}"
        if notes:
            message += "；" + "；".join(notes)
        return entry, message, True

    def _reset(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
        with _commit_lock:
            _ensure_runtime_fields(entry)
            # 重置只回状态到草稿：历史水位、采集时间与提交窗口都保留
            entry["status"] = "草稿"
            entry["pending"] = True
            entry["abnormal"] = False
            entry["是否完成"] = "否"
            entry["重置时间"] = _now_iso()
        return entry, "增量游标已重置游标：回到草稿，历史水位与采集时间保留", True

    def _disable(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str, bool]:
        with _commit_lock:
            _ensure_runtime_fields(entry)
            entry["status"] = "已停用"
            entry["pending"] = False
            entry["abnormal"] = True
        return entry, "增量游标已停用游标", True

    def _mark_attempt(self, entry: dict[str, Any], reason: str, values: dict[str, Any]) -> None:
        """中断的对齐尝试：只留尝试标记，不动水位与状态。"""
        with _commit_lock:
            _ensure_runtime_fields(entry)
            entry["尝试次数"] = int(entry.get("尝试次数") or 0) + 1
            entry["最近尝试"] = {
                "动作": "标记对齐",
                "结果": "中断",
                "原因": reason,
                "采集时间": str(values.get("采集时间") or ""),
                "尝试时间": _now_iso(),
            }
