"""Tk-independent PQC UI state with frozen reset and isolation semantics."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from threading import RLock
from typing import Iterable

from .pqc_models import (
    PQC_UI_SLOT_CODES,
    PqcCandidateState,
    PqcCreationBatch,
    PqcDeviceCandidate,
    PqcDraftSelection,
    PqcProject,
    PqcTool,
    PqcToolValidity,
)
from .pqc_rules import classify_source, with_selection


class PqcUiStateError(ValueError):
    pass


class PqcUiState:
    def __init__(self) -> None:
        self._lock = RLock()
        self.source_path = ""
        self.engineer_name = ""
        self.projects: tuple[PqcProject, ...] = ()
        self.project_number = ""
        self.candidates: tuple[PqcDeviceCandidate, ...] = ()
        # 完整复合键：项目 + 设备 + template_key + procedure_code。
        self.tool_selections: dict[tuple[str, str, str, str], tuple[PqcTool, ...]] = {}
        self.confirmed = True
        self.creation = PqcCreationBatch()

    def set_projects(
        self,
        source_path: object,
        engineer_name: object,
        projects: Iterable[PqcProject],
    ) -> tuple[PqcProject, ...]:
        with self._lock:
            next_projects = tuple(projects)
            project_numbers = [item.project_number for item in next_projects]
            if len(set(project_numbers)) != len(project_numbers):
                raise PqcUiStateError("PQC 项目列表存在重复项目编号")
            self.source_path = str(source_path).strip()
            self.engineer_name = str(engineer_name).strip()
            self.projects = next_projects
            self.project_number = self.projects[0].project_number if len(self.projects) == 1 else ""
            self._clear_transient()
            return self.projects

    def select_project(self, project_number: object) -> PqcProject:
        value = str(project_number).strip()
        with self._lock:
            for project in self.projects:
                if project.project_number == value:
                    if value != self.project_number:
                        self._clear_transient()
                    self.project_number = value
                    return project
        raise PqcUiStateError(f"未知 PQC 项目：{value}")

    def current_project(self) -> PqcProject:
        with self._lock:
            for project in self.projects:
                if project.project_number == self.project_number:
                    return project
        raise PqcUiStateError("请先选择 PQC 项目")

    def load_candidates(self, candidates: Iterable[PqcDeviceCandidate]) -> tuple[PqcDeviceCandidate, ...]:
        with self._lock:
            if not self.project_number:
                raise PqcUiStateError("未选择项目，不能加载设备候选")
            next_candidates = tuple(candidates)
            item_codes: set[str] = set()
            expected_types = PQC_UI_SLOT_CODES
            for device in next_candidates:
                if device.source.project_number != self.project_number:
                    raise PqcUiStateError("设备候选与当前 PQC 项目不一致")
                if device.source.item_code in item_codes:
                    raise PqcUiStateError(f"设备候选重复：{device.source.item_code}")
                item_codes.add(device.source.item_code)
                if tuple(item.code for item in device.types) != expected_types:
                    raise PqcUiStateError("PQC 设备候选必须严格包含 01、02、03、04、05、CATEGORY（类别模板）")
            self.candidates = next_candidates
            self.tool_selections.clear()
            self.creation = PqcCreationBatch()
            self.confirmed = True
            return self.candidates

    def _candidate_index(self, item_code: str) -> int:
        for index, device in enumerate(self.candidates):
            if device.source.item_code == item_code:
                return index
        raise PqcUiStateError(f"未知 PQC 设备：{item_code}")

    @staticmethod
    def _require_template_identity(candidate) -> tuple[str, str]:
        """Require the resolved composite identity; never fall back to legacy slot codes."""

        template_key = candidate.template_key
        procedure_code = candidate.procedure_code
        if not template_key or not procedure_code:
            raise PqcUiStateError("PQC 类型缺少复合模板身份，禁止继续配置或创建")
        return template_key, procedure_code

    def _tool_key(self, item_code: str, type_code: str) -> tuple[str, str, str, str]:
        device = self.candidates[self._candidate_index(item_code)]
        candidate = device.type_candidate(type_code)
        template_key, procedure_code = self._require_template_identity(candidate)
        return (
            self.project_number,
            device.source.item_code,
            template_key,
            procedure_code,
        )

    def tools_for_type(self, item_code: str, type_code: str) -> tuple[PqcTool, ...]:
        """Return saved tools for an applicable candidate only.

        ``EXCLUDED``/``BLOCKED`` placeholders deliberately carry no template
        identity (and are not configurable).  They must not be turned into a
        synthetic empty composite key merely because a table row is rendered.
        Applicable candidates still flow through ``_tool_key`` so a missing
        ``template_key`` or ``procedure_code`` remains fail-closed.
        """

        device = self.candidates[self._candidate_index(item_code)]
        candidate = device.type_candidate(type_code)
        if candidate.state in {PqcCandidateState.EXCLUDED, PqcCandidateState.BLOCKED}:
            return ()

        return self.tool_selections.get(self._tool_key(item_code, type_code), ())

    def toggle_type(self, item_code: str, type_code: str, selected: bool | None = None) -> PqcDeviceCandidate:
        with self._lock:
            index = self._candidate_index(item_code)
            device = self.candidates[index]
            updated_types = []
            found = False
            for candidate in device.types:
                if candidate.code != type_code:
                    updated_types.append(candidate)
                    continue
                found = True
                next_value = not candidate.selected if selected is None else bool(selected)
                try:
                    updated_types.append(with_selection(candidate, next_value))
                except ValueError as exc:
                    raise PqcUiStateError(str(exc)) from exc
            if not found:
                raise PqcUiStateError(f"未知 PQC 类型：{type_code}")
            updated = replace(device, types=tuple(updated_types))
            mutable = list(self.candidates)
            mutable[index] = updated
            self.candidates = tuple(mutable)
            return updated

    def set_device_selected(self, item_code: str, selected: bool) -> PqcDeviceCandidate:
        with self._lock:
            index = self._candidate_index(item_code)
            device = self.candidates[index]
            updated_types = tuple(
                with_selection(candidate, selected) if candidate.selectable else candidate
                for candidate in device.types
            )
            updated = replace(device, types=updated_types)
            mutable = list(self.candidates)
            mutable[index] = updated
            self.candidates = tuple(mutable)
            return updated

    def set_tools(
        self,
        item_code: str,
        type_code: str,
        tools: Iterable[PqcTool],
        *,
        completion_day: date,
    ) -> tuple[PqcTool, ...]:
        with self._lock:
            device = self.candidates[self._candidate_index(item_code)]
            candidate = device.type_candidate(type_code)
            if not candidate.selectable:
                raise PqcUiStateError(f"当前设备类型不可配置工器具：{candidate.detail}")
            selected_list: list[PqcTool] = []
            seen: set[tuple[str, str]] = set()
            for tool in tools:
                identity = (tool.tool_id, tool.display_text())
                if identity in seen:
                    continue
                seen.add(identity)
                selected_list.append(tool)
            selected = tuple(selected_list)
            expired = [tool.display_text() for tool in selected if tool.validity_on(completion_day) is PqcToolValidity.EXPIRED]
            if expired:
                raise PqcUiStateError("过期工器具不可选：" + ", ".join(expired))
            self.tool_selections[self._tool_key(item_code, type_code)] = selected
            return selected

    def copy_tools_to_selected_type(
        self,
        source_item_code: str,
        type_code: str,
        *,
        completion_day: date,
    ) -> int:
        with self._lock:
            source_tools = self.tools_for_type(source_item_code, type_code)
            copied = 0
            for device in self.candidates:
                candidate = device.type_candidate(type_code)
                if not candidate.selected or not candidate.selectable:
                    continue
                self.set_tools(
                    device.source.item_code,
                    type_code,
                    source_tools,
                    completion_day=completion_day,
                )
                copied += 1
            return copied

    def selected_count(self) -> int:
        with self._lock:
            return sum(candidate.selected for device in self.candidates for candidate in device.types)

    def selected_drafts(self) -> tuple[PqcDraftSelection, ...]:
        with self._lock:
            result: list[PqcDraftSelection] = []
            for device in self.candidates:
                for candidate in device.types:
                    if not candidate.selected:
                        continue
                    template_key, procedure_code = self._require_template_identity(candidate)
                    result.append(
                        PqcDraftSelection(
                            source=device.source,
                            type_code=candidate.code,
                            tools=self.tools_for_type(device.source.item_code, candidate.code),
                            allow_existing_draft=candidate.state is PqcCandidateState.DRAFT,
                            preview_trigger_basis=candidate.trigger_basis,
                            template_key=template_key,
                            procedure_code=procedure_code,
                            model_id=candidate.model_id,
                            procedure_name=candidate.inspection_type.procedure_name,
                            form_suffix=(candidate.trigger_basis.rsplit("-", 1)[-1] if candidate.trigger_basis else ""),
                            classification=classify_source(device.source) or "",
                        )
                    )
            return tuple(result)

    def apply_creation(self, creation: PqcCreationBatch) -> None:
        with self._lock:
            self.creation = creation

    def on_show(self) -> None:
        with self._lock:
            self.confirmed = True

    def reset_transient(self) -> None:
        """Preserve source/engineer/projects/current project and clear runtime state."""

        with self._lock:
            self._clear_transient()

    def _clear_transient(self) -> None:
        self.candidates = ()
        self.tool_selections.clear()
        self.creation = PqcCreationBatch()
        self.confirmed = True


__all__ = ["PqcUiState", "PqcUiStateError"]
