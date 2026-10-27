from __future__ import annotations

from datetime import date

from src.pqc_models import (
    PQC_TEMPLATE_SPECS,
    PqcProject,
    PqcQuerySnapshot,
    PqcSourceRow,
    PqcTemplate,
)


def source_row(
    item_code: str = "DEV-JD-01",
    *,
    row_number: int = 2,
    project_number: str = "P",
    project_name: str = "Project",
    engineer: str = "Engineer",
    special_case: str = "",
    ncr_code: str = "",
    category: str | None = None,
):
    # 旧测试的默认设备保持 -JD- 机柜优先路径；新类别测试显式提供 J 值。
    normalized_category = category if category is not None else ("" if "-JD-" in item_code else "盘台")
    return PqcSourceRow(
        source_rows=(row_number,),
        workorder_number="WO-001",
        project_name=project_name,
        project_number=project_number,
        item_code=item_code,
        sequence_code="000123",
        qc_engineer=engineer,
        special_case=special_case,
        ncr_code=ncr_code,
        category=normalized_category,
    )


def project(*rows: PqcSourceRow, project_number: str = "P", project_name: str = "Project"):
    return PqcProject(project_number, project_name, tuple(rows))


def active_templates():
    suffixes = {
        "COMPONENT": "11",
        "NON_NUCLEAR_CABINET": "12",
        "DEMO_LOGIC_GLM": "13",
        "CHASSIS": "14",
        "FAN_TRAY": "15",
    }
    return tuple(
        PqcTemplate(
            model_id=str(index),
            procedure_code=spec.legacy_template_code or f"PQC-{spec.template_key}",
            procedure_name=spec.procedure_name,
            procedure_version="1",
            is_lasted_version="1",
            processstate="2",
            is_frozen="0",
            template_key=spec.template_key,
            form_suffix=suffixes.get(spec.template_key, spec.slot_code),
        )
        for index, spec in enumerate(PQC_TEMPLATE_SPECS, start=1)
    )


def snapshot(*, formal=(), drafts=(), tools=(), templates=None):
    return PqcQuerySnapshot(
        templates=active_templates() if templates is None else tuple(templates),
        formal_records=tuple(formal),
        drafts=tuple(drafts),
        tools=tuple(tools),
    )


TODAY = date(2026, 9, 6)
