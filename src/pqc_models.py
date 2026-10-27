"""Pure data contracts for the PQC process-end draft workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


@dataclass(frozen=True)
class PqcInspectionType:
    code: str
    name: str
    template_code: str
    # 业务模板键与精确过程名称独立于 UI 槽位；旧调用仍可只传前三个字段。
    template_key: str = ""
    procedure_name: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}{self.code}"


PQC_INSPECTION_TYPES: tuple[PqcInspectionType, ...] = (
    PqcInspectionType("01", "装配", "DEMO-ICS-DEMOFORM-001"),
    PqcInspectionType("02", "接线", "DEMO-ICS-DEMOFORM-002"),
    PqcInspectionType("03", "上电前", "DEMO-ICS-DEMOFORM-003"),
    PqcInspectionType("04", "上电后", "DEMO-ICS-DEMOFORM-004"),
    PqcInspectionType("05", "扭矩", "DEMO-ICS-DEMOFORM-005"),
)
PQC_TYPE_BY_CODE = {item.code: item for item in PQC_INSPECTION_TYPES}
PQC_TYPE_BY_TEMPLATE_CODE = {item.template_code: item for item in PQC_INSPECTION_TYPES}


@dataclass(frozen=True)
class PqcTemplateSpec:
    """分类矩阵中的一个业务模板，不把 UI 列号当成数据库身份。"""

    template_key: str
    slot_code: str
    label: str
    procedure_name: str
    # 仅测试/历史审计使用；运行时 procedure_code 必须来自活动模板查询。
    legacy_template_code: str = ""


# 01--05 是既有固定槽位；CATEGORY 只是新增 UI 槽位名称，不代表 SS=06。
PQC_UI_SLOT_CODES: tuple[str, ...] = ("01", "02", "03", "04", "05", "CATEGORY")
PQC_TEMPLATE_SPECS: tuple[PqcTemplateSpec, ...] = (
    PqcTemplateSpec("CABINET_ASSEMBLY", "01", "装配", "机柜装配质量检查记录单", "DEMO-ICS-DEMOFORM-001"),
    PqcTemplateSpec("CABINET_WIRING", "02", "接线", "机柜接线质量检查记录单", "DEMO-ICS-DEMOFORM-002"),
    PqcTemplateSpec("CABINET_PRE_POWER", "03", "上电前", "机柜上电前质量检查记录单", "DEMO-ICS-DEMOFORM-003"),
    PqcTemplateSpec("CABINET_POST_POWER", "04", "上电后", "机柜上电后质量检查记录单", "DEMO-ICS-DEMOFORM-004"),
    PqcTemplateSpec("TORQUE", "05", "扭矩", "扭矩质量检查记录单", "DEMO-ICS-DEMOFORM-005"),
    PqcTemplateSpec("PANEL_ASSEMBLY", "01", "装配", "盘台装配质量检查记录单"),
    PqcTemplateSpec("PANEL_WIRING", "02", "接线", "盘台接线质量检查记录单"),
    PqcTemplateSpec("PANEL_PRE_POWER", "03", "上电前", "盘台上电前质量检查记录单"),
    PqcTemplateSpec("COMPONENT", "CATEGORY", "类别模板", "组件类产品质量检查记录单"),
    PqcTemplateSpec("NON_NUCLEAR_CABINET", "CATEGORY", "类别模板", "非核盘柜质量检查记录单"),
    PqcTemplateSpec("DEMO_LOGIC_GLM", "CATEGORY", "类别模板", "DemoLogic通用逻辑模块（GLM）作业质量检查记录单"),
    PqcTemplateSpec("CHASSIS", "CATEGORY", "类别模板", "机箱类产品集成制造质量检查记录单"),
    PqcTemplateSpec("FAN_TRAY", "CATEGORY", "类别模板", "风扇盘集成质量检查记录单"),
)
PQC_TEMPLATE_SPEC_BY_KEY = {item.template_key: item for item in PQC_TEMPLATE_SPECS}
PQC_TEMPLATE_SPECS_BY_NAME = {item.procedure_name: item for item in PQC_TEMPLATE_SPECS}
PQC_CATEGORY_TO_TEMPLATE_KEYS: dict[str, tuple[str, ...]] = {
    # 扭矩模板由机柜/盘台共享同一真实模板身份。
    "盘台": ("PANEL_ASSEMBLY", "PANEL_WIRING", "PANEL_PRE_POWER", "TORQUE"),
    "组件类": ("COMPONENT",),
    "非核盘柜": ("NON_NUCLEAR_CABINET",),
    "DemoLogic_GLM": ("DEMO_LOGIC_GLM",),
    "机箱类": ("CHASSIS",),
    "风扇盘": ("FAN_TRAY",),
}


@dataclass(frozen=True)
class PqcSourceRow:
    """One normalized device from the source workbook.

    ``source_rows`` contains every physical row merged into this device.  A
    conflicting duplicate remains visible but carries errors and is never a
    creatable candidate.
    """

    source_rows: tuple[int, ...]
    workorder_number: str
    project_name: str
    project_number: str
    item_code: str
    sequence_code: str
    qc_engineer: str
    special_case: str = ""
    ncr_code: str = ""
    errors: tuple[str, ...] = ()
    category: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.project_number, self.item_code

    @property
    def first_source_row(self) -> int:
        return min(self.source_rows) if self.source_rows else 0


@dataclass(frozen=True)
class PqcProject:
    project_number: str
    project_name: str
    rows: tuple[PqcSourceRow, ...]
    errors: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.project_number}  {self.project_name}".strip()


@dataclass(frozen=True)
class PqcWorkbook:
    path: str
    rows: tuple[PqcSourceRow, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class PqcTemplate:
    model_id: str
    procedure_code: str
    procedure_name: str
    procedure_version: str
    is_lasted_version: str
    processstate: str
    is_frozen: str
    template_key: str = ""
    # 仅来自只读 proof-map；空值表示未核验，候选必须 BLOCKED。
    form_suffix: str = ""


@dataclass(frozen=True)
class PqcFormalRecord:
    record_id: str
    project_number: str
    item_code: str
    template_code: str
    processstate: str
    trigger_basis: str
    template_key: str = ""
    template_name: str = ""


@dataclass(frozen=True)
class PqcDraftRecord:
    draft_id: str
    project_number: str
    item_code: str
    template_code: str
    trigger_basis: str
    qc_engineer: str = ""
    create_datetime: str = ""
    template_key: str = ""
    template_name: str = ""


class PqcToolValidity(str, Enum):
    VALID = "有效"
    NO_DATE = "无有效期"
    EXPIRED = "已过期"


@dataclass(frozen=True)
class PqcTool:
    tool_id: str
    name: str
    item_id: str
    valid_until: str = ""

    def validity_on(self, completion_day: date) -> PqcToolValidity:
        if not self.valid_until:
            return PqcToolValidity.NO_DATE
        try:
            valid_day = date.fromisoformat(self.valid_until[:10])
        except ValueError:
            return PqcToolValidity.NO_DATE
        return PqcToolValidity.EXPIRED if valid_day < completion_day else PqcToolValidity.VALID

    def display_text(self) -> str:
        suffix = self.valid_until[:10] if self.valid_until else "无有效期"
        return f"{self.name}({self.item_id})({suffix})"


@dataclass(frozen=True)
class PqcQuerySnapshot:
    templates: tuple[PqcTemplate, ...]
    formal_records: tuple[PqcFormalRecord, ...]
    drafts: tuple[PqcDraftRecord, ...]
    tools: tuple[PqcTool, ...]


class PqcCandidateState(str, Enum):
    CANDIDATE = "待创建"
    FORMAL = "已有正式"
    DRAFT = "已有草稿"
    EXCLUDED = "特殊排除"
    ANOMALY = "异常"
    BLOCKED = "阻断"


@dataclass(frozen=True)
class PqcTypeCandidate:
    inspection_type: PqcInspectionType
    state: PqcCandidateState
    selected: bool
    selectable: bool
    detail: str
    model_id: str = ""
    trigger_basis: str = ""
    existing_draft_ids: tuple[str, ...] = ()
    template_key: str = ""
    procedure_code: str = ""

    @property
    def code(self) -> str:
        return self.inspection_type.code

    @property
    def identity(self) -> tuple[str, str]:
        """稳定业务身份；UI 槽位 code 不能替代它。"""
        return self.template_key, self.procedure_code


@dataclass(frozen=True)
class PqcDeviceCandidate:
    source: PqcSourceRow
    types: tuple[PqcTypeCandidate, ...]
    serial_number: int | None = None
    warnings: tuple[str, ...] = ()

    def type_candidate(self, code: str) -> PqcTypeCandidate:
        for candidate in self.types:
            if candidate.code == code:
                return candidate
        raise KeyError(code)


class PqcCreationStatus(str, Enum):
    PREVIEW = "PREVIEW"
    SKIPPED = "SKIPPED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PqcDraftSelection:
    source: PqcSourceRow
    type_code: str
    tools: tuple[PqcTool, ...] = ()
    allow_existing_draft: bool = False
    preview_trigger_basis: str = ""
    template_key: str = ""
    procedure_code: str = ""
    model_id: str = ""
    procedure_name: str = ""
    form_suffix: str = ""
    classification: str = ""


@dataclass(frozen=True)
class PqcCreationResult:
    source_rows: tuple[int, ...]
    project_number: str
    item_code: str
    type_code: str
    trigger_basis: str
    status: PqcCreationStatus
    detail: str
    tools: tuple[str, ...] = ()
    template_key: str = ""
    procedure_code: str = ""


@dataclass(frozen=True)
class PqcCreationBatch:
    results: tuple[PqcCreationResult, ...] = ()

    def count(self, status: PqcCreationStatus) -> int:
        return sum(1 for item in self.results if item.status is status)


@dataclass(frozen=True)
class PqcModelContextRow:
    context_number: str
    inspection_items: str
    inspection_standards: str


@dataclass(frozen=True)
class PqcModelContext:
    model_id: str
    procedure_code: str
    procedure_name: str
    procedure_version: str
    rows: tuple[PqcModelContextRow, ...] = field(default_factory=tuple)
    template_key: str = ""
    form_suffix: str = ""


__all__ = [
    "PQC_INSPECTION_TYPES",
    "PQC_UI_SLOT_CODES",
    "PQC_TEMPLATE_SPECS",
    "PQC_TEMPLATE_SPEC_BY_KEY",
    "PQC_TEMPLATE_SPECS_BY_NAME",
    "PQC_CATEGORY_TO_TEMPLATE_KEYS",
    "PQC_TYPE_BY_CODE",
    "PQC_TYPE_BY_TEMPLATE_CODE",
    "PqcCandidateState",
    "PqcCreationBatch",
    "PqcCreationResult",
    "PqcCreationStatus",
    "PqcDeviceCandidate",
    "PqcDraftRecord",
    "PqcDraftSelection",
    "PqcFormalRecord",
    "PqcInspectionType",
    "PqcModelContext",
    "PqcModelContextRow",
    "PqcProject",
    "PqcQuerySnapshot",
    "PqcSourceRow",
    "PqcTemplate",
    "PqcTemplateSpec",
    "PqcTool",
    "PqcToolValidity",
    "PqcTypeCandidate",
    "PqcWorkbook",
]
