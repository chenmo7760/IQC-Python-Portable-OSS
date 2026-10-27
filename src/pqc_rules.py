"""PQC 分类、模板候选、编号和工器具规则。"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import replace
from datetime import date
from typing import Iterable, Mapping

from .pqc_models import (
    PQC_CATEGORY_TO_TEMPLATE_KEYS,
    PQC_TEMPLATE_SPEC_BY_KEY,
    PQC_TEMPLATE_SPECS,
    PQC_UI_SLOT_CODES,
    PqcCandidateState,
    PqcDeviceCandidate,
    PqcDraftRecord,
    PqcFormalRecord,
    PqcInspectionType,
    PqcProject,
    PqcQuerySnapshot,
    PqcTemplate,
    PqcTool,
    PqcToolValidity,
    PqcTypeCandidate,
)


_SPECIAL_RULES: Mapping[str, frozenset[str]] = {
    "无装配": frozenset({"01"}),
    "无接线": frozenset({"02"}),
    "无上电前": frozenset({"03"}),
    "无上电后": frozenset({"04"}),
    "无上电": frozenset({"03", "04"}),
    "无扭矩": frozenset({"05"}),
    "无扭矩上电": frozenset({"03", "04", "05"}),
}
_SPECIAL_PUNCTUATION_RE = re.compile(r"[\s,，、;；。.!！:：/\\|()（）\[\]【】]+")
_VALID_CATEGORIES = frozenset(PQC_CATEGORY_TO_TEMPLATE_KEYS)


class PqcRuleError(ValueError):
    pass


def normalize_special_case(value: object) -> str:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()
    return _SPECIAL_PUNCTUATION_RE.sub("", text)


def special_case_exclusions(value: object) -> tuple[frozenset[str], bool]:
    normalized = normalize_special_case(value)
    if not normalized:
        return frozenset(), True
    excluded = _SPECIAL_RULES.get(normalized)
    return (excluded, True) if excluded is not None else (frozenset(), False)


def parse_trigger_basis(project_number: str, value: object) -> tuple[int, str] | None:
    """Parse project-NNN-SS; SS is a real form suffix, never a UI column number."""

    text = "" if value is None else str(value).strip()
    # SS/流水号只接受 ASCII 数字；``\d`` 还会接受全角等 Unicode 数字。
    match = re.fullmatch(re.escape(project_number) + r"-([0-9]{3})-([0-9]{2})", text)
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def _is_valid_form_suffix(value: object) -> bool:
    """Return whether a template proof-map SS is exactly two ASCII digits."""

    return isinstance(value, str) and re.fullmatch(r"[0-9]{2}", value) is not None


def form_suffix_for_type(type_code: object) -> str:
    """Generate SS from inspection type; CATEGORY and unknown types use 01."""
    code = "" if type_code is None else str(type_code).strip()
    return code if code in {"01", "02", "03", "04", "05"} else "01"


def classify_source(source: object) -> str | None:
    """Return CABINET or an exact J category; never guess from names."""

    item_code = str(getattr(source, "item_code", "")).strip()
    if "-JD-" in item_code:  # 区分大小写，-jd- 不命中。
        return "CABINET"
    category = str(getattr(source, "category", "")).strip()
    return category if category in _VALID_CATEGORIES else None


def template_specs_for_source(source: object) -> tuple[object, ...]:
    profile = classify_source(source)
    if profile == "CABINET":
        cabinet_keys = {"CABINET_ASSEMBLY", "CABINET_WIRING", "CABINET_PRE_POWER", "CABINET_POST_POWER", "TORQUE"}
        return tuple(item for item in PQC_TEMPLATE_SPECS if item.template_key in cabinet_keys)
    return tuple(PQC_TEMPLATE_SPEC_BY_KEY[key] for key in PQC_CATEGORY_TO_TEMPLATE_KEYS.get(profile or "", ()))


def _history_by_device(
    project_number: str,
    formal_records: Iterable[PqcFormalRecord],
    drafts: Iterable[PqcDraftRecord],
) -> tuple[dict[str, set[int]], int, tuple[str, ...]]:
    serials: dict[str, set[int]] = defaultdict(set)
    maximum = 0
    invalid: list[str] = []
    history = [(item.item_code, item.trigger_basis) for item in formal_records if item.project_number == project_number] + [(item.item_code, item.trigger_basis) for item in drafts if item.project_number == project_number]
    for item_code, trigger_basis in history:
        parsed = parse_trigger_basis(project_number, trigger_basis)
        if parsed is None:
            if trigger_basis:
                invalid.append(trigger_basis)
            continue
        serial, _suffix = parsed
        serials[item_code].add(serial)
        maximum = max(maximum, serial)
    return dict(serials), maximum, tuple(dict.fromkeys(invalid))


def _cross_device_serial_conflicts(serials_by_item: Mapping[str, set[int]]) -> dict[str, set[int]]:
    owners: dict[int, set[str]] = defaultdict(set)
    for item_code, serials in serials_by_item.items():
        for serial in serials:
            owners[serial].add(item_code)
    conflicts: dict[str, set[int]] = defaultdict(set)
    for serial, item_codes in owners.items():
        if len(item_codes) < 2:
            continue
        for item_code in item_codes:
            conflicts[item_code].add(serial)
    return dict(conflicts)


def _template_map(templates: Iterable[PqcTemplate]) -> tuple[dict[str, tuple[PqcTemplate, ...]], dict[str, tuple[PqcTemplate, ...]]]:
    by_name: dict[str, list[PqcTemplate]] = defaultdict(list)
    by_code: dict[str, list[PqcTemplate]] = defaultdict(list)
    for template in templates:
        by_name[template.procedure_name].append(template)
        by_code[template.procedure_code].append(template)
    return ({key: tuple(value) for key, value in by_name.items()}, {key: tuple(value) for key, value in by_code.items()})


def _template_for_spec(spec: object, by_name: Mapping[str, tuple[PqcTemplate, ...]], by_code: Mapping[str, tuple[PqcTemplate, ...]]) -> tuple[PqcTemplate | None, str]:
    # 名称是配置矩阵的唯一查询条件，禁止名称缺失时回退旧通用模板。
    candidates = by_name.get(spec.procedure_name, ())
    if len(candidates) != 1:
        return None, f"模板 {spec.procedure_name} 最新已审核未冻结版本数量为 {len(candidates)}，要求唯一"
    template = candidates[0]
    if template.is_lasted_version != "1" or template.processstate != "2" or template.is_frozen != "0":
        return None, f"模板 {spec.procedure_name} 不是最新、已审核、未冻结版本"
    if not template.model_id or not template.procedure_code:
        return None, f"模板 {spec.procedure_name} 缺少 model_id 或 procedure_code，禁止创建"
    same_code = by_code.get(template.procedure_code, ())
    if len({item.procedure_name for item in same_code}) > 1:
        return None, f"模板编码 {template.procedure_code} 对应多个不同模板名称，身份不唯一"
    return template, ""


def _slot_type(spec: object) -> PqcInspectionType:
    return PqcInspectionType(spec.slot_code, spec.label, "", spec.template_key, spec.procedure_name)


def _slot_placeholder(slot: str, source_category: str) -> PqcTypeCandidate:
    label = {"01": "装配", "02": "接线", "03": "上电前", "04": "上电后", "05": "扭矩", "CATEGORY": "类别模板"}[slot]
    return PqcTypeCandidate(PqcInspectionType(slot, label, "", "", ""), PqcCandidateState.EXCLUDED, False, False, f"不适用：{source_category or '类别无法识别'}")


def build_device_candidates(project: PqcProject, snapshot: PqcQuerySnapshot) -> tuple[PqcDeviceCandidate, ...]:
    """Generate six UI slots while retaining each real template identity."""

    template_by_name, template_by_code = _template_map(snapshot.templates)
    # 任意两个受控模板名称共享 procedure_code 都会让复合身份失真，项目级 fail-closed。
    ambiguous_template_codes = frozenset(
        code for code, candidates in template_by_code.items()
        if len({item.procedure_name for item in candidates}) > 1
    )
    serials_by_item, maximum, invalid_history = _history_by_device(project.project_number, snapshot.formal_records, snapshot.drafts)
    cross_device_conflicts = _cross_device_serial_conflicts(serials_by_item)
    next_serial = maximum + 1
    assigned: dict[str, int | None] = {}
    serial_errors: dict[str, str] = {}
    for row in sorted(project.rows, key=lambda item: item.first_source_row):
        values = serials_by_item.get(row.item_code, set())
        if row.item_code in cross_device_conflicts:
            assigned[row.item_code] = None
            serial_errors[row.item_code] = "项目流水号被多台设备共用：" + ", ".join(f"{value:03d}" for value in sorted(cross_device_conflicts[row.item_code]))
        elif len(values) > 1:
            assigned[row.item_code] = None
            serial_errors[row.item_code] = "同一设备存在多个历史流水号：" + ", ".join(f"{value:03d}" for value in sorted(values))
        elif len(values) == 1:
            assigned[row.item_code] = next(iter(values))
        elif row.errors:
            assigned[row.item_code] = None
        elif next_serial > 999:
            assigned[row.item_code] = None
            serial_errors[row.item_code] = "项目流水号已超过 999，不能分配新编号"
        else:
            assigned[row.item_code] = next_serial
            next_serial += 1

    result: list[PqcDeviceCandidate] = []
    for source in sorted(project.rows, key=lambda item: item.first_source_row):
        profile = classify_source(source)
        serial = assigned[source.item_code]
        applicable = {item.slot_code: item for item in template_specs_for_source(source)}
        resolved_templates = {
            slot: _template_for_spec(spec, template_by_name, template_by_code)
            for slot, spec in applicable.items()
        }
        # SS 和完整 trigger_basis 都是同一设备内的唯一身份约束；只阻断
        # 受影响的候选，并在设备 warnings 中保留可审计的阻断原因。
        suffix_slots: dict[str, list[str]] = defaultdict(list)
        trigger_slots: dict[str, list[str]] = defaultdict(list)
        for slot, (resolved, _error) in resolved_templates.items():
            if resolved is None:
                continue
            suffix = form_suffix_for_type(slot)
            suffix_slots[suffix].append(slot)
            if serial is not None:
                trigger_slots[f"{source.project_number}-{serial:03d}-{suffix}"].append(slot)
        duplicate_suffix_slots = frozenset(
            slot for slots in suffix_slots.values() if len(slots) > 1 for slot in slots
        )
        duplicate_trigger_slots = frozenset(
            slot for slots in trigger_slots.values() if len(slots) > 1 for slot in slots
        )
        excluded, special_recognized = special_case_exclusions(source.special_case)
        type_candidates: list[PqcTypeCandidate] = []
        for slot in PQC_UI_SLOT_CODES:
            spec = applicable.get(slot)
            if spec is None:
                if profile is None:
                    label = {"01": "装配", "02": "接线", "03": "上电前", "04": "上电后", "05": "扭矩", "CATEGORY": "类别模板"}[slot]
                    type_candidates.append(PqcTypeCandidate(PqcInspectionType(slot, label, "", "", ""), PqcCandidateState.BLOCKED, False, False, "类别无法识别：请修正 J 列"))
                else:
                    type_candidates.append(_slot_placeholder(slot, source.category if profile != "CABINET" else "机柜类"))
                continue
            template, template_error = resolved_templates[slot]
            # SS is generated from the inspection type; CATEGORY defaults to 01.
            suffix = form_suffix_for_type(slot) if template else ""
            trigger = f"{source.project_number}-{serial:03d}-{suffix}" if serial is not None and suffix else ""
            # 正式/草稿查缺必须同时匹配 template_key 和 procedure_code；空键历史记录不再按 code 猜测。
            formal = tuple(item for item in snapshot.formal_records if item.project_number == source.project_number and item.item_code == source.item_code and item.template_key == spec.template_key and item.template_name == spec.procedure_name and template is not None and item.template_code == template.procedure_code)
            drafts = tuple(item for item in snapshot.drafts if item.project_number == source.project_number and item.item_code == source.item_code and item.template_key == spec.template_key and item.template_name == spec.procedure_name and template is not None and item.template_code == template.procedure_code)
            identity_anomalies = tuple(
                item for item in (*snapshot.formal_records, *snapshot.drafts)
                if item.project_number == source.project_number
                and item.item_code == source.item_code
                and template is not None
                and item.template_code == template.procedure_code
                and (item.template_key != spec.template_key or item.template_name != spec.procedure_name)
            )
            inspection_type = _slot_type(spec)
            common = dict(template_key=spec.template_key, procedure_code=template.procedure_code if template else "", trigger_basis=trigger)
            # Existing formal records need no new SS/serial allocation. Resolve
            # their verified composite identity before validating creation inputs.
            if any(item.processstate in {"1", "2"} for item in formal):
                statuses = sorted({item.processstate for item in formal if item.processstate in {"1", "2"}})
                labels = {"1": "审核中", "2": "已完成"}
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.FORMAL, False, False, "已有正式记录：" + "/".join(labels[item] for item in statuses), model_id=template.model_id, **common)
            elif profile is None:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.BLOCKED, False, False, "类别无法识别：请修正 J 列", **common)
            elif ambiguous_template_codes:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.BLOCKED, False, False, "模板编码对应多个不同模板名称，模板身份不唯一，禁止创建", **common)
            elif source.errors:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.BLOCKED, False, False, "；".join(source.errors), **common)
            elif serial is None:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.BLOCKED, False, False, serial_errors.get(source.item_code, "无法分配项目流水号"), **common)
            elif template is None:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.BLOCKED, False, False, template_error, **common)
            elif identity_anomalies:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.BLOCKED, False, False, "历史记录模板复合身份缺失或不一致，禁止按 procedure_code 回退", model_id=template.model_id, **common)
            elif slot in duplicate_suffix_slots or slot in duplicate_trigger_slots:
                reason = []
                if slot in duplicate_suffix_slots:
                    reason.append("SS 重复")
                if slot in duplicate_trigger_slots:
                    reason.append("完整 trigger_basis 重复")
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.BLOCKED, False, False, "同一设备适用模板" + "、".join(reason) + "，禁止创建", model_id=template.model_id, **common)
            elif spec.slot_code in excluded and len(applicable) > 1:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.EXCLUDED, False, False, f"特殊情况排除：{source.special_case}", model_id=template.model_id, **common)
            elif formal:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.ANOMALY, False, True, "正式记录状态异常：" + "/".join(sorted({item.processstate or "空" for item in formal})), model_id=template.model_id, **common)
            elif drafts:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.DRAFT, False, True, f"已有草稿 {len(drafts)} 份，可人工选择重复创建", model_id=template.model_id, existing_draft_ids=tuple(item.draft_id for item in drafts), **common)
            elif not special_recognized:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.ANOMALY, False, True, f"特殊情况未识别：{source.special_case}", model_id=template.model_id, **common)
            else:
                candidate = PqcTypeCandidate(inspection_type, PqcCandidateState.CANDIDATE, True, True, "可创建", model_id=template.model_id, **common)
            type_candidates.append(candidate)
        single_role_warning = ""
        if len(applicable) == 1 and excluded and special_recognized:
            only_spec = next(iter(applicable.values()))
            if only_spec.slot_code not in excluded:
                single_role_warning = f"特殊情况提示：{source.special_case} 对 {only_spec.procedure_name} 无对应角色，未删除唯一模板"
        duplicate_warnings = []
        if duplicate_suffix_slots:
            duplicate_warnings.append("同一设备适用模板 SS 重复，相关候选已阻断：" + ", ".join(sorted(duplicate_suffix_slots)))
        if duplicate_trigger_slots:
            duplicate_warnings.append("同一设备完整 trigger_basis 重复，相关候选已阻断：" + ", ".join(sorted(duplicate_trigger_slots)))
        warnings = tuple(filter(None, (serial_errors.get(source.item_code, ""), f"发现 {len(invalid_history)} 个非法历史表单编号，已忽略" if invalid_history else "", f"特殊情况未识别：{source.special_case}" if source.special_case and not special_recognized else "", single_role_warning, *duplicate_warnings)))
        result.append(PqcDeviceCandidate(source, tuple(type_candidates), serial, warnings))
    return tuple(result)


def with_selection(candidate: PqcTypeCandidate, selected: bool) -> PqcTypeCandidate:
    if selected and not candidate.selectable:
        raise PqcRuleError(f"{candidate.inspection_type.label} 当前状态不可选：{candidate.detail}")
    if selected and (not candidate.template_key or not candidate.procedure_code):
        raise PqcRuleError(f"{candidate.inspection_type.label} 模板身份不完整，禁止选择")
    return replace(candidate, selected=bool(selected))


def tool_is_recommended(tool: PqcTool, type_code: str) -> bool:
    name = unicodedata.normalize("NFKC", tool.name).lower()
    if type_code in {"02", "03", "04"}:
        return any(token in name for token in ("万用表", "多用表"))
    if type_code == "05":
        return any(token in name for token in ("扭矩", "扭力"))
    return False


def sort_tools_for_type(tools: Iterable[PqcTool], type_code: str, completion_day: date) -> tuple[PqcTool, ...]:
    validity_order = {PqcToolValidity.VALID: 0, PqcToolValidity.NO_DATE: 1, PqcToolValidity.EXPIRED: 2}
    return tuple(sorted(tools, key=lambda item: (0 if tool_is_recommended(item, type_code) else 1, validity_order[item.validity_on(completion_day)], item.name, item.item_id, item.tool_id)))


__all__ = ["PqcRuleError", "build_device_candidates", "classify_source", "form_suffix_for_type", "normalize_special_case", "parse_trigger_basis", "sort_tools_for_type", "special_case_exclusions", "template_specs_for_source", "tool_is_recommended", "with_selection"]
