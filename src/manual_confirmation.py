"""人工确认所需的模板候选与确认草稿。

本模块只做内存中的候选排序、确认决策校验和 JSON 草稿持久化；它不会
修改全局别名配置、访问 Windchill/MySQL 或生成任何导入产物。UI 可以把
``ManualConfirmationRow`` 渲染成表格，后台 provider 在收到确认后再继续
解析。未勾选的编码以 ``None`` 保存，表示操作员明确放弃该项。
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .models import normalize_procedure_name


@dataclass(frozen=True)
class FuzzyTemplateCandidate:
    """一个仅供人工参考的当前模板候选。"""

    model_id: str
    procedure_name: str
    score: float
    active: bool
    block_reason: str = ""
    basis: str = "sequence_ratio"

    @property
    def display_name(self) -> str:
        return f"ID{self.model_id} · {self.procedure_name} · {self.score:.1%}"


@dataclass(frozen=True)
class ManualConfirmationRow:
    """一个无法自动匹配的物项，供 UI 逐行勾选。"""

    order_number: str
    item_code: str
    raw_name: str
    normalized_name: str
    candidates: tuple[FuzzyTemplateCandidate, ...] = ()
    blocking_reason: str = "自动正则/精确匹配未得到唯一有效模板"

    @property
    def confirmation_key(self) -> str:
        """当前批次稳定复合键，避免不同送检单同物项互相覆盖。"""

        return f"{str(self.order_number).strip()}::{str(self.item_code).strip()}"


class ManualConfirmationRequired(RuntimeError):
    """模型匹配失败，需要操作员先确认候选。"""

    def __init__(self, rows: Sequence[ManualConfirmationRow]) -> None:
        self.rows = tuple(rows)
        codes = ", ".join(row.item_code for row in self.rows[:8])
        suffix = "…" if len(self.rows) > 8 else ""
        reason = self.rows[0].blocking_reason if self.rows else ""
        prefix = f"{reason}；" if reason else ""
        super().__init__(f"{prefix}有 {len(self.rows)} 个物项未能自动匹配，需人工确认：{codes}{suffix}")


@dataclass(frozen=True)
class ManualConfirmationDecision:
    """一条人工决策；名称为空时使用候选模板的规范名称。"""

    model_id: str
    manual_name: str = ""


def _is_active(model: object) -> bool:
    return (
        str(getattr(model, "is_lasted_version", "")).strip() == "1"
        and str(getattr(model, "is_frozen", "")).strip() == "0"
        and str(getattr(model, "processstate", "")).strip() == "2"
    )


def _similarity(left: str, right: str) -> float:
    """保守、可解释的名称相似度；不用于自动选中。"""

    a = normalize_procedure_name(left).casefold()
    b = normalize_procedure_name(right).casefold()
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if a == b:
        return 1.0
    if a in b or b in a:
        # 子串候选只提高展示排序，不足以绕过人工确认。
        ratio = max(ratio, 0.82 + 0.15 * min(len(a), len(b)) / max(len(a), len(b)))
    return min(1.0, ratio)


def _similarity_with_basis(left: str, right: str) -> tuple[float, str]:
    score = _similarity(left, right)
    normalized_left = normalize_procedure_name(left).casefold()
    normalized_right = normalize_procedure_name(right).casefold()
    if normalized_left == normalized_right:
        return score, "normalized_exact"
    if normalized_left and normalized_right and (
        normalized_left in normalized_right or normalized_right in normalized_left
    ):
        return score, "reverse_substring"
    return score, "sequence_ratio"


def rank_template_candidates(
    models: Iterable[object],
    expected_name: str,
    *,
    limit: int = 8,
) -> tuple[FuzzyTemplateCandidate, ...]:
    """为人工表格生成稳定排序的候选；绝不返回自动决策。"""

    if limit <= 0:
        return ()
    candidates: list[FuzzyTemplateCandidate] = []
    for model in models:
        model_id = str(getattr(model, "id", "")).strip()
        procedure_name = str(getattr(model, "procedure_name", "")).strip()
        if not model_id or not procedure_name:
            continue
        active = _is_active(model)
        # 非当前有效版本不能成为人工确认选项；否则历史模板会挤掉
        # 可实际导入的 ACTIVE 候选。阻断原因仍由上游错误/审计保留。
        if not active:
            continue
        score, basis = _similarity_with_basis(expected_name, procedure_name)
        candidates.append(
            FuzzyTemplateCandidate(
                model_id=model_id,
                procedure_name=procedure_name,
                score=score,
                active=active,
                basis=basis,
            )
        )
    # 分数相同按 ID、名称稳定排序，避免数据库行序变化导致候选跳动。
    candidates.sort(key=lambda item: (-item.score, not item.active, item.model_id, item.procedure_name))
    return tuple(candidates[:limit])


def build_confirmation_row(
    *,
    order_number: str,
    item_code: str,
    raw_name: str,
    normalized_name: str,
    models: Iterable[object],
    blocking_reason: str,
) -> ManualConfirmationRow:
    return ManualConfirmationRow(
        order_number=str(order_number).strip(),
        item_code=str(item_code).strip(),
        raw_name=str(raw_name),
        normalized_name=str(normalized_name),
        candidates=rank_template_candidates(models, normalized_name),
        blocking_reason=str(blocking_reason),
    )


def validate_confirmation_choices(
    rows: Sequence[ManualConfirmationRow],
    choices: Mapping[str, object],
) -> dict[str, ManualConfirmationDecision | None]:
    """校验 UI 的逐行选择，未勾选项明确转成 ``None``。"""

    by_code: dict[str, ManualConfirmationRow] = {}
    for row in rows:
        key = row.confirmation_key
        if key in by_code:
            raise ValueError(f"人工确认复合键重复：{key}")
        by_code[key] = row
    result: dict[str, ManualConfirmationDecision | None] = {}
    legacy_counts: dict[str, int] = {}
    for row in rows:
        legacy_counts[row.item_code] = legacy_counts.get(row.item_code, 0) + 1
    for key, row in by_code.items():
        # 复合键是 UI/新调用方的正式契约；单物项旧调用方仍可用 raw
        # item_code 兼容，但同批重复编码时不允许退回含糊旧键。
        raw = choices.get(key)
        result_key = key
        if raw is None and legacy_counts[row.item_code] == 1 and row.item_code in choices:
            raw = choices.get(row.item_code)
            # 单行旧调用方继续得到 raw key；一旦使用正式复合 key，结果
            # 始终保持复合键，避免同批重复编码互相覆盖。
            result_key = row.item_code
        if raw is None or raw is False:
            result[result_key] = None
            continue
        manual_name = ""
        if isinstance(raw, Mapping):
            model_id = str(raw.get("model_id", raw.get("id", ""))).strip()
            manual_name = str(raw.get("manual_name", raw.get("name", ""))).strip()
        else:
            model_id = str(raw).strip()
        if not model_id and not manual_name:
            raise ValueError(f"物项 {key} 已勾选但未选择模板")
        if model_id:
            candidate = next((item for item in row.candidates if item.model_id == model_id), None)
            if candidate is None:
                raise ValueError(f"物项 {key} 的人工确认候选 ID 无效")
            if not candidate.active:
                raise ValueError(f"物项 {key} 选择了非当前有效模板 ID{model_id}")
        result[result_key] = ManualConfirmationDecision(model_id, manual_name)
    return result


def write_confirmation_draft(
    path: Path,
    rows: Sequence[ManualConfirmationRow],
    *,
    # UI 传入的值可能是 ``None``/``False``（明确放弃）、model_id 字符串，
    # 或带 ``model_id``/``manual_name`` 的 ``ManualConfirmationDecision``。
    # 这里不应把参数错误地收窄为 ``str``，否则类型检查会把合法的逐行
    # 确认结果误报为不兼容；序列化函数会统一转为审计 JSON。
    decisions: Mapping[str, object] | None = None,
    status: str = "PENDING",
) -> Path:
    """写独立人工确认 JSON；不覆盖 procedure_aliases.json。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    decision_map = decisions or {}
    payload = {
        "schema_version": "manual_template_confirmation.v1",
        "status": str(status),
        "created_at": datetime.now().astimezone().isoformat(),
        "rows": [
            {
                "order_number": row.order_number,
                "item_code": row.item_code,
                "raw_windchill_name": row.raw_name,
                "normalized_name": row.normalized_name,
                "blocking_reason": row.blocking_reason,
                "candidates": [
                    {
                        "model_id": item.model_id,
                        "procedure_name": item.procedure_name,
                        "score": round(item.score, 6),
                        "active": item.active,
                        "basis": item.basis,
                        "block_reason": item.block_reason,
                    }
                    for item in row.candidates
                ],
                "decision": _serialize_decision(
                    decision_map.get(row.confirmation_key, decision_map.get(row.item_code))
                ),
                "decision_state": (
                    "confirmed"
                    if decision_map.get(row.confirmation_key, decision_map.get(row.item_code))
                    else (
                        "declined"
                        if row.confirmation_key in decision_map or row.item_code in decision_map
                        else "pending"
                    )
                ),
            }
            for row in rows
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def confirmed_model_ids(decisions: Mapping[str, object]) -> dict[str, str]:
    """提取非空候选 ID；名称-only 决策继续保留在原决策映射中解析。"""

    result: dict[str, str] = {}
    for code, decision in decisions.items():
        if decision is None or decision is False:
            continue
        if isinstance(decision, ManualConfirmationDecision):
            model_id = decision.model_id.strip()
        else:
            model_id = str(decision).strip()
        if model_id:
            result[str(code)] = model_id
    return result


def _serialize_decision(value: object) -> object:
    if value is None or value is False:
        return None
    if isinstance(value, ManualConfirmationDecision):
        return {"model_id": value.model_id, "manual_name": value.manual_name}
    return str(value)
