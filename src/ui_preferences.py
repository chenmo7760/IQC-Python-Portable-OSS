"""Per-Windows-user UI preferences for IQC/PQC/送检信息调整页面。"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


VALID_MODULES = frozenset({"IQC", "PQC", "INSPECTION_ADJUSTMENT"})
# PQC 表格列键是稳定的 UI 配置键，不携带 SS 或模板身份含义。
PQC_COLUMN_WIDTH_KEYS: tuple[str, ...] = (
    "select", "excel", "item", "sequence", "01", "02", "03", "04", "05",
    "special", "ncr", "category", "status",
)
PQC_COLUMN_WIDTH_DEFAULTS: dict[str, int] = {
    # These widths are based on the legacy PQC layout.  They keep every
    # business heading readable at first launch before a user resizes a
    # column, while the tree's horizontal scrollbar handles narrow panes.
    "select": 69, "excel": 74, "item": 191, "sequence": 191,
    "01": 84, "02": 84, "03": 84, "04": 84, "05": 84,
    "special": 130, "ncr": 130, "category": 130, "status": 100,
}
PQC_STATUS_COLUMN_WIDTH = 100
PQC_COLUMN_WIDTH_MIN = 40
PQC_COLUMN_WIDTH_MAX = 600
PQC_TOOL_COLUMN_WIDTH_KEYS: tuple[str, ...] = ("selected", "name", "validity")
PQC_TOOL_COLUMN_WIDTH_DEFAULTS: dict[str, int] = {"selected": 40, "name": 245, "validity": 90}
PQC_LAYOUT_WIDTH_KEYS: tuple[str, ...] = ("scope", "devices", "tools")
# PQC 左侧范围栏保持紧凑的 200px，剩余窗口宽度交给右侧设备表。
PQC_LAYOUT_WIDTH_DEFAULTS: dict[str, int] = {"scope": 200, "devices": 1030, "tools": 404}
PQC_LAYOUT_WIDTH_MIN = 180
PQC_LAYOUT_WIDTH_MAX = 2000
PQC_SCOPE_WIDTH_MAX = 200


def default_preferences_path() -> Path:
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "IQC-Python-Portable" / "ui_preferences.json"


@dataclass(frozen=True)
class UiPreferenceLoad:
    module: str
    warning: str = ""
    pqc_column_widths: Mapping[str, int] = field(default_factory=dict)
    pqc_tool_column_widths: Mapping[str, int] = field(default_factory=dict)
    pqc_layout_widths: Mapping[str, int] = field(default_factory=dict)
    last_iqc_source_path: str = ""
    last_pqc_source_path: str = ""
    last_engineer_name: str = ""
    last_paper_standard_catalog_path: str = ""
    last_inspection_adjustment_a_path: str = ""
    last_inspection_adjustment_b_path: str = ""


@dataclass
class UiPreferenceStore:
    path: Path

    @classmethod
    def for_current_user(cls) -> "UiPreferenceStore":
        return cls(default_preferences_path())

    def _load_payload(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, Mapping):
            raise json.JSONDecodeError("配置格式无效", "", 0)
        return dict(payload)

    def _load_valid_payload(self) -> dict[str, object]:
        try:
            payload = self._load_payload()
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload

    @staticmethod
    def _text(value: object) -> str:
        return "" if value is None else str(value).strip()

    def _save_payload(self, payload: Mapping[str, object]) -> tuple[bool, str]:
        temp_path = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(self.path)
        except OSError as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False, f"页面偏好无法保存：{type(exc).__name__}"
        return True, ""

    def _payload_with_valid_module(self) -> dict[str, object]:
        payload = self._load_valid_payload()
        module = str(payload.get("last_active_module", "IQC")).strip().upper()
        payload["last_active_module"] = module if module in VALID_MODULES else "IQC"
        return payload

    def load(self) -> UiPreferenceLoad:
        try:
            if not self.path.exists():
                return UiPreferenceLoad("IQC", "页面偏好配置不存在，已默认进入 IQC")
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return UiPreferenceLoad("IQC", "页面偏好配置损坏或无法读取，已默认进入 IQC")
        if not isinstance(payload, Mapping):
            return UiPreferenceLoad("IQC", "页面偏好配置格式无效，已默认进入 IQC")
        module = str(payload.get("last_active_module", "")).strip().upper()
        if module not in VALID_MODULES:
            return UiPreferenceLoad("IQC", "页面偏好配置值无效，已默认进入 IQC")
        def read_widths(raw: object, keys: tuple[str, ...], *, minimum: int = PQC_COLUMN_WIDTH_MIN, maximum: int = PQC_COLUMN_WIDTH_MAX) -> tuple[dict[str, int], bool]:
            widths: dict[str, int] = {}
            invalid = raw is not None and not isinstance(raw, Mapping)
            if isinstance(raw, Mapping):
                for key, value in raw.items():
                    if key not in keys:
                        invalid = True
                        continue
                    # bool 是 int 的子类，但不是合法的用户列宽。
                    if isinstance(value, bool) or not isinstance(value, int) or not (minimum <= value <= maximum):
                        invalid = True
                        continue
                    widths[str(key)] = int(value)
            return widths, invalid

        widths, invalid_width = read_widths(payload.get("pqc_column_widths", {}), PQC_COLUMN_WIDTH_KEYS)
        tool_widths, invalid_tool_width = read_widths(payload.get("pqc_tool_column_widths", {}), PQC_TOOL_COLUMN_WIDTH_KEYS)
        layout_widths, invalid_layout_width = read_widths(
            payload.get("pqc_layout_widths", {}),
            PQC_LAYOUT_WIDTH_KEYS,
            minimum=PQC_LAYOUT_WIDTH_MIN,
            maximum=PQC_LAYOUT_WIDTH_MAX,
        )
        if "scope" in layout_widths and layout_widths["scope"] > PQC_SCOPE_WIDTH_MAX:
            layout_widths["scope"] = PQC_SCOPE_WIDTH_MAX
            invalid_layout_width = True
        last_iqc_source_path = self._text(payload.get("last_iqc_source_path", ""))
        last_pqc_source_path = self._text(payload.get("last_pqc_source_path", ""))
        last_engineer_name = self._text(payload.get("last_engineer_name", ""))
        last_paper_standard_catalog_path = self._text(payload.get("last_paper_standard_catalog_path", ""))
        last_inspection_adjustment_a_path = self._text(
            payload.get("last_inspection_adjustment_a_path", "")
        )
        last_inspection_adjustment_b_path = self._text(
            payload.get("last_inspection_adjustment_b_path", "")
        )
        warning = "PQC 表格列宽配置无效，已对异常项使用默认宽度" if invalid_width or invalid_tool_width else ""
        if invalid_layout_width:
            warning = warning or "PQC 布局宽度配置无效，已对异常项使用默认宽度"
        return UiPreferenceLoad(
            module,
            warning,
            widths,
            tool_widths,
            layout_widths,
            last_iqc_source_path,
            last_pqc_source_path,
            last_engineer_name,
            last_paper_standard_catalog_path,
            last_inspection_adjustment_a_path,
            last_inspection_adjustment_b_path,
        )

    def save(self, module: object) -> tuple[bool, str]:
        value = str(module).strip().upper()
        if value not in VALID_MODULES:
            return False, "页面偏好值无效"
        payload = self._payload_with_valid_module()
        payload["last_active_module"] = value
        return self._save_payload(payload)

    def save_iqc_source_path(self, path: object) -> tuple[bool, str]:
        payload = self._payload_with_valid_module()
        payload["last_iqc_source_path"] = self._text(path)
        return self._save_payload(payload)

    def save_pqc_source_path(self, path: object) -> tuple[bool, str]:
        payload = self._payload_with_valid_module()
        payload["last_pqc_source_path"] = self._text(path)
        return self._save_payload(payload)

    def save_engineer_name(self, name: object) -> tuple[bool, str]:
        payload = self._payload_with_valid_module()
        payload["last_engineer_name"] = self._text(name)
        return self._save_payload(payload)

    def save_paper_standard_catalog_path(self, path: object) -> tuple[bool, str]:
        payload = self._payload_with_valid_module()
        payload["last_paper_standard_catalog_path"] = self._text(path)
        return self._save_payload(payload)

    def save_inspection_adjustment_a_path(self, path: object) -> tuple[bool, str]:
        payload = self._payload_with_valid_module()
        payload["last_inspection_adjustment_a_path"] = self._text(path)
        return self._save_payload(payload)

    def save_inspection_adjustment_b_path(self, path: object) -> tuple[bool, str]:
        payload = self._payload_with_valid_module()
        payload["last_inspection_adjustment_b_path"] = self._text(path)
        return self._save_payload(payload)

    def save_pqc_column_widths(self, widths: Mapping[str, object]) -> tuple[bool, str]:
        """原子保存 PQC 列宽，模块偏好及其他未知字段不被覆盖。"""

        normalized: dict[str, int] = {}
        for key, value in widths.items():
            if key not in PQC_COLUMN_WIDTH_KEYS:
                return False, "PQC 列宽键无效"
            if isinstance(value, bool) or not isinstance(value, int) or not (PQC_COLUMN_WIDTH_MIN <= value <= PQC_COLUMN_WIDTH_MAX):
                return False, "PQC 列宽值无效"
            normalized[key] = int(value)
        payload = self._payload_with_valid_module()
        payload["pqc_column_widths"] = normalized
        return self._save_payload(payload)

    def save_pqc_tool_column_widths(self, widths: Mapping[str, object]) -> tuple[bool, str]:
        """原子保存右侧工器具表列宽，与中间设备表分开存储。"""

        normalized: dict[str, int] = {}
        for key, value in widths.items():
            if key not in PQC_TOOL_COLUMN_WIDTH_KEYS:
                return False, "PQC 工器具列宽键无效"
            if isinstance(value, bool) or not isinstance(value, int) or not (PQC_COLUMN_WIDTH_MIN <= value <= PQC_COLUMN_WIDTH_MAX):
                return False, "PQC 工器具列宽值无效"
            normalized[key] = int(value)
        payload = self._payload_with_valid_module()
        payload["pqc_tool_column_widths"] = normalized
        return self._save_payload(payload)

    def save_pqc_layout_widths(self, widths: Mapping[str, object]) -> tuple[bool, str]:
        """原子保存 PQC 布局宽度，重启后恢复拖动后的布局。"""

        normalized: dict[str, int] = {}
        for key, value in widths.items():
            if key not in PQC_LAYOUT_WIDTH_KEYS:
                return False, "PQC 布局宽度键无效"
            if isinstance(value, bool) or not isinstance(value, int) or not (PQC_LAYOUT_WIDTH_MIN <= value <= PQC_LAYOUT_WIDTH_MAX):
                return False, "PQC 布局宽度值无效"
            if key == "scope" and value > PQC_SCOPE_WIDTH_MAX:
                return False, "PQC 左栏宽度不能超过 200px"
            normalized[key] = int(value)
        payload = self._payload_with_valid_module()
        payload["pqc_layout_widths"] = normalized
        return self._save_payload(payload)


__all__ = [
    "UiPreferenceLoad",
    "UiPreferenceStore",
    "VALID_MODULES",
    "PQC_COLUMN_WIDTH_KEYS",
    "PQC_COLUMN_WIDTH_DEFAULTS",
    "PQC_COLUMN_WIDTH_MIN",
    "PQC_COLUMN_WIDTH_MAX",
    "PQC_STATUS_COLUMN_WIDTH",
    "PQC_TOOL_COLUMN_WIDTH_KEYS",
    "PQC_TOOL_COLUMN_WIDTH_DEFAULTS",
    "PQC_LAYOUT_WIDTH_KEYS",
    "PQC_LAYOUT_WIDTH_DEFAULTS",
    "PQC_LAYOUT_WIDTH_MIN",
    "PQC_LAYOUT_WIDTH_MAX",
    "PQC_SCOPE_WIDTH_MAX",
    "default_preferences_path",
]
