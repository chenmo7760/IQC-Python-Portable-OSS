"""ProcessEnd draft payload, one-shot API client and serial creation workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Mapping, Protocol, Sequence

from .entry_api import EntryApiTransport
from .pqc_models import (
    PqcCandidateState,
    PqcCreationBatch,
    PqcCreationResult,
    PqcCreationStatus,
    PqcDraftRecord,
    PqcDraftSelection,
    PqcModelContext,
    PqcModelContextRow,
    PqcProject,
    PqcQuerySnapshot,
    PqcSourceRow,
    PqcTool,
    PqcToolValidity,
)
from .pqc_rules import build_device_candidates, classify_source, form_suffix_for_type, parse_trigger_basis


PROCESSEND_MODEL_CONTEXT_PATH = "/api/api/qcauto/procedureModel/processEnd/getModelContextById/"
PROCESSEND_SAVE_DRAFT_PATH = "/api/api/qcauto/procedureDraft/processEnd/saveDraftContextById/"
# The configured base URL points at the web reverse proxy.  Its /api/ prefix is
# stripped once, so the backend's logical /api/system/... route uses /api/api/
# here, consistent with the existing Entry API transport paths.
PROCESSEND_IDENTITY_PATH = "/api/api/system/user/user_info/"


class ProcessEndApiError(RuntimeError):
    pass


class ProcessEndIdentityError(ProcessEndApiError):
    """The authenticated API user could not be safely proven."""


class ProcessEndPostUncertain(ProcessEndApiError):
    """A POST was attempted but its durable outcome is not trustworthy."""


class ProcessEndPostRejected(ProcessEndApiError):
    """The server returned a readable response that did not confirm success."""


@dataclass(frozen=True)
class ProcessEndApiIdentity:
    """Minimal authenticated identity proven by the backend itself.

    IDs are held only in memory for validation.  They are never copied into a
    request header/payload and should not be included in log messages.
    """

    user_id: str
    name: str
    dept_id: str

    @property
    def current_dept_id(self) -> str:
        """The department used for the current authenticated request.

        The current processEnd user-info endpoint exposes the active
        department as ``dept`` (or ``dept_info``).  Keep the explicit alias so
        callers cannot accidentally compare a missing/other department field.
        """

        return self.dept_id

    @property
    def current_department_id(self) -> str:
        """Readable alias for the active department ID."""

        return self.current_dept_id


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProcessEndApiError(f"{label}不是对象")
    return value


def parse_processend_model_context(
    response: Mapping[str, object], *, model_id: str, expected_template_code: str,
    expected_template_name: str = "", expected_template_key: str = "",
    expected_form_suffix: str = "",
) -> PqcModelContext:
    data = response.get("data")
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)) or len(data) < 2:
        raise ProcessEndApiError("processEnd 模板 GET 响应缺少表头或检查项")
    raw_head = data[0]
    if isinstance(raw_head, Sequence) and not isinstance(raw_head, (str, bytes)):
        if not raw_head:
            raise ProcessEndApiError("processEnd 模板 GET 表头为空")
        raw_head = raw_head[0]
    head = _mapping(raw_head, "processEnd 模板表头")
    procedure_code = _text(head.get("procedure_code", head.get("procedureCode")))
    procedure_name = _text(head.get("procedure_name", head.get("procedureName")))
    if not procedure_name:
        raise ProcessEndApiError("processEnd 模板表头缺少模板名称")
    if procedure_code != expected_template_code:
        raise ProcessEndApiError(
            f"processEnd 模板编码不匹配：期望 {expected_template_code}，实际 {procedure_code or '空'}"
        )
    if expected_template_name and procedure_name != _text(expected_template_name):
        raise ProcessEndApiError(
            f"processEnd 模板名称不匹配：期望 {_text(expected_template_name)}，实际 {procedure_name or '空'}"
        )
    raw_rows = data[1]
    if isinstance(raw_rows, Mapping):
        raw_rows = raw_rows.get("data", raw_rows.get("results", raw_rows.get("rows")))
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)) or not raw_rows:
        raise ProcessEndApiError("processEnd 模板检查项为空")
    rows: list[PqcModelContextRow] = []
    for index, raw in enumerate(raw_rows, start=1):
        item = _mapping(raw, f"processEnd 模板检查项[{index}]")
        context_number = _text(item.get("context_number", item.get("contextNumber")))
        inspection_items = _text(item.get("inspection_items", item.get("inspectionItems")))
        inspection_standards = _text(item.get("inspection_standards", item.get("inspectionStandards")))
        if not context_number or not inspection_items:
            raise ProcessEndApiError(f"processEnd 模板检查项[{index}]缺少序号或检查项目")
        rows.append(PqcModelContextRow(context_number, inspection_items, inspection_standards))
    return PqcModelContext(
        model_id=_text(model_id),
        procedure_code=procedure_code,
        procedure_name=procedure_name,
        procedure_version=_text(head.get("procedure_version", head.get("procedureVersion"))),
        rows=tuple(rows),
        template_key=_text(expected_template_key),
        form_suffix=_text(expected_form_suffix),
    )


def parse_processend_api_identity(response: Mapping[str, object]) -> ProcessEndApiIdentity:
    """Parse the authenticated ``system/user/user_info`` response fail-closed."""

    raw = response.get("data")
    try:
        data = _mapping(raw, "API 当前用户响应")
    except ProcessEndApiError as exc:
        raise ProcessEndIdentityError(str(exc)) from exc
    user_id = _text(data.get("id", data.get("user_id", data.get("userId"))))
    name = _text(data.get("name"))
    dept_value = data.get("dept", data.get("dept_id", data.get("deptId")))
    if isinstance(dept_value, Mapping):
        dept_id = _text(
            dept_value.get("id", dept_value.get("dept_id", dept_value.get("deptId")))
        )
    else:
        dept_id = _text(dept_value)
    if not dept_id:
        dept_info = data.get("dept_info", data.get("deptInfo"))
        if isinstance(dept_info, Mapping):
            dept_id = _text(
                dept_info.get("id", dept_info.get("dept_id", dept_info.get("deptId")))
            )
    # Some deployments expose the active department separately from the
    # account's department field.  If present, it is authoritative and must
    # agree with ``dept``; an empty or conflicting current department is not a
    # usable identity and therefore blocks POST.
    current_keys = (
        "current_dept_id",
        "currentDeptId",
        "current_department_id",
        "currentDepartmentId",
        "current_dept",
        "currentDepartment",
    )
    current_present = any(key in data for key in current_keys)
    current_value = data.get(
        "current_dept_id",
        data.get(
            "currentDeptId",
            data.get(
                "current_department_id",
                data.get("currentDepartmentId", data.get("current_dept", data.get("currentDepartment"))),
            ),
        ),
    )
    if current_present:
        if isinstance(current_value, Mapping):
            current_dept_id = _text(
                current_value.get("id", current_value.get("dept_id", current_value.get("deptId")))
            )
        else:
            current_dept_id = _text(current_value)
        if not current_dept_id or (dept_id and current_dept_id != dept_id):
            raise ProcessEndIdentityError("API 当前部门未通过已认证身份校验")
        dept_id = current_dept_id
    if not user_id or not name or name.casefold() in {"anonymous", "anonymoususer"} or not dept_id:
        raise ProcessEndIdentityError("API 当前用户未通过已认证身份校验")
    return ProcessEndApiIdentity(user_id, name, dept_id)


def _selected_engineer_identity(value: object) -> ProcessEndApiIdentity:
    """Extract the configured Engineer identity for explicit diagnostics only."""

    user_id = _text(getattr(value, "user_id", ""))
    name = _text(getattr(value, "name", ""))
    dept_id = _text(getattr(value, "dept_id", ""))
    if not user_id or not name or name.casefold() in {"anonymous", "anonymoususer"} or not dept_id:
        raise ProcessEndIdentityError("所选工程师缺少有效 user_id/name/dept_id，禁止 POST")
    return ProcessEndApiIdentity(user_id, name, dept_id)


def _selected_engineer_name(value: object) -> str:
    """Return the configured engineer name without requiring local user/dept IDs."""

    if isinstance(value, str):
        name = _text(value)
    else:
        name = _text(getattr(value, "name", "")) or _text(getattr(value, "display_name", ""))
    if not name or name.casefold() in {"anonymous", "anonymoususer"}:
        raise ProcessEndApiError("所选工程师姓名无效，禁止 POST")
    return name


def _required_source_text(value: object, field_name: str) -> str:
    text = _text(value)
    if not text:
        raise ProcessEndApiError(f"{field_name}不能为空")
    return text


def build_processend_draft_payload(
    source: PqcSourceRow,
    *,
    type_code: str,
    model_id: object,
    trigger_basis: object,
    qc_engineer: object,
    model_context: PqcModelContext,
    completion_day: date,
    tools: Iterable[PqcTool] = (),
    template_key: str = "",
    procedure_code: str = "",
    procedure_name: str = "",
    form_suffix: str = "",
) -> dict[str, object]:
    """Build the frozen FormData fields without issuing a request."""

    # type_code 仅是 UI 槽位；模板编码、名称、键和 SS 必须由活动候选显式提供。
    template_key_text = _required_source_text(template_key, "模板键")
    expected_code = _required_source_text(procedure_code, "模板编码")
    expected_name = _required_source_text(procedure_name, "模板名称")
    # SS is generated from the inspection type.  Keep the argument for
    # compatibility with older callers, but reject a conflicting value.
    expected_suffix = form_suffix_for_type(type_code)
    supplied_suffix = _text(form_suffix)
    if supplied_suffix and supplied_suffix != expected_suffix:
        raise ProcessEndApiError("表单编号后缀与检验类型不一致")
    if source.errors:
        raise ProcessEndApiError("Excel 设备行存在冲突：" + "；".join(source.errors))
    if not isinstance(completion_day, date):
        raise ProcessEndApiError("完成日期必须是 date")
    model_text = _required_source_text(model_id, "modelId")
    if model_context.model_id != model_text:
        raise ProcessEndApiError("GET 模板上下文 modelId 与候选不一致")
    if model_context.procedure_code != expected_code:
        raise ProcessEndApiError("GET 模板上下文编码与检验类型不一致")
    if model_context.procedure_name != expected_name:
        raise ProcessEndApiError("GET 模板上下文名称与候选不一致")
    if model_context.template_key != template_key_text:
        raise ProcessEndApiError("GET 模板上下文键与候选不一致")
    if model_context.form_suffix and model_context.form_suffix != expected_suffix:
        raise ProcessEndApiError("GET 模板上下文 SS 与候选不一致")
    if not model_context.rows:
        raise ProcessEndApiError("GET 模板上下文没有检查项")

    trigger_text = _required_source_text(trigger_basis, "表单编号")
    parsed_trigger = parse_trigger_basis(_text(source.project_number), trigger_text)
    if parsed_trigger is None or parsed_trigger[1] != expected_suffix:
        raise ProcessEndApiError("表单编号与项目或检验类型不一致")
    engineer_text = _required_source_text(qc_engineer, "质检工程师")
    if engineer_text != _text(source.qc_engineer):
        raise ProcessEndApiError("所选工程师与 Excel 设备行不一致")

    selected_tools = tuple(tools)
    expired_tools = [
        item.display_text()
        for item in selected_tools
        if item.validity_on(completion_day) is PqcToolValidity.EXPIRED
    ]
    if expired_tools:
        raise ProcessEndApiError("过期工器具不可发送：" + ", ".join(expired_tools))
    fields: dict[str, object] = {
        "modelId": model_text,
        "procedureTriggerBasis": trigger_text,
        "procedureDraftWorkorderNumber": _required_source_text(source.workorder_number, "派工单"),
        "procedureDraftContractNumber": "NA",
        "procedureDraftProjectName": _required_source_text(source.project_name, "项目名称"),
        "procedureDraftProjectNumber": _required_source_text(source.project_number, "项目编号"),
        "procedureDraftItemCode": _required_source_text(source.item_code, "设备编号"),
        "procedureDraftSequenceCode": _required_source_text(source.sequence_code, "序列码/流转码"),
        "procedureDraftInspectionConclusion": "",
        "procedureDraftQcEngineer": engineer_text,
        "procedureDraftCompletionTime": completion_day.isoformat(),
    }
    if selected_tools:
        fields["procedureDraftTestingTools"] = ",".join(item.display_text().strip() for item in selected_tools)
    if _text(source.ncr_code):
        fields["procedureDraftNcrCode"] = _text(source.ncr_code)
    # The frozen rule deliberately omits procedureDraftNotes: H controls type
    # exclusion only and must never leak into notes.
    for index, context in enumerate(model_context.rows):
        prefix = f"procedureDraftContext[{index}]"
        fields[f"{prefix}[context_number]"] = _text(context.context_number)
        fields[f"{prefix}[inspection_items]"] = _text(context.inspection_items)
        fields[f"{prefix}[inspection_standards]"] = _text(context.inspection_standards)
        fields[f"{prefix}[inspect_records]"] = ""
        fields[f"{prefix}[inspection_conclusion]"] = ""

    # Defensive final trim for every source-derived string.  Internal spaces
    # remain intact; only accidental leading/trailing whitespace is removed.
    return {key: (_text(value) if isinstance(value, str) else value) for key, value in fields.items()}


@dataclass
class ProcessEndDraftApiClient:
    transport: EntryApiTransport

    def get_model_context(
        self, model_id: object, expected_template_code: str, *,
        expected_template_name: str = "", expected_template_key: str = "",
        expected_form_suffix: str = "",
    ) -> PqcModelContext:
        model_text = _required_source_text(model_id, "modelId")
        _required_source_text(expected_template_code, "模板编码")
        _required_source_text(expected_template_name, "模板名称")
        _required_source_text(expected_template_key, "模板键")
        try:
            response = self.transport.get_json(PROCESSEND_MODEL_CONTEXT_PATH, {"id": model_text})
        except Exception as exc:
            raise ProcessEndApiError(f"GET processEnd 模板上下文失败：{type(exc).__name__}") from exc
        return parse_processend_model_context(
            response, model_id=model_text, expected_template_code=expected_template_code,
            expected_template_name=expected_template_name,
            expected_template_key=expected_template_key,
            expected_form_suffix=expected_form_suffix,
        )

    def verify_identity(self, selected_engineer: object) -> ProcessEndApiIdentity:
        """Explicitly prove that the API credential resolves to the selected engineer.

        PQC draft creation does not call this method. Exceptions deliberately
        expose only the exception class, never request headers.
        """

        expected = _selected_engineer_identity(selected_engineer)
        try:
            response = self.transport.get_json(PROCESSEND_IDENTITY_PATH, {})
        except Exception as exc:
            # Do not retain the transport exception as a chained cause: HTTP
            # libraries may include request/response headers in its repr.
            raise ProcessEndIdentityError(f"API 身份预检失败：{type(exc).__name__}") from None
        identity = parse_processend_api_identity(response)
        if (
            identity.user_id != expected.user_id
            or identity.name != expected.name
            or identity.current_dept_id != expected.dept_id
        ):
            raise ProcessEndIdentityError("API 当前登录用户、所选工程师或当前部门不一致")
        return identity

    def save_draft(
        self, fields: Mapping[str, object], *, selected_engineer: object
    ) -> Mapping[str, object]:
        # PQC follows the IQC Entry path: the local payload carries only the
        # selected engineer name, while creator/dept come from backend request.user.
        expected_name = _selected_engineer_name(selected_engineer)
        payload_engineer = _required_source_text(fields.get("procedureDraftQcEngineer"), "质检工程师")
        if payload_engineer != expected_name:
            raise ProcessEndApiError("POST 草稿工程师字段与所选工程师不一致")
        try:
            response = self.transport.post_form(PROCESSEND_SAVE_DRAFT_PATH, fields)
        except Exception as exc:
            raise ProcessEndPostUncertain(f"POST processEnd 草稿结果不确定：{type(exc).__name__}") from None
        data = response.get("data") if isinstance(response, Mapping) else None
        accepted = isinstance(data, list) and any(
            _text(item).lower() == "draft context saved successfully." for item in data
        )
        if not accepted:
            raise ProcessEndPostRejected("POST processEnd 草稿响应未确认保存成功")
        return dict(response)


class PqcQueryRepository(Protocol):
    def load_project_snapshot(self, project_number: str, *, include_tools: bool = True) -> PqcQuerySnapshot:
        ...

    def matching_drafts(
        self,
        project_number: str,
        item_code: str,
        template_code: str,
        trigger_basis: str,
        qc_engineer: str,
        *,
        template_key: str = "",
        created_not_before: datetime | None = None,
    ) -> tuple[PqcDraftRecord, ...]:
        ...


PqcCreateEventCallback = Callable[[str, str, str], None]


@dataclass
class PqcDraftCreationService:
    query: PqcQueryRepository
    api: ProcessEndDraftApiClient
    today: Callable[[], date] = date.today
    now: Callable[[], datetime] = datetime.now
    event_callback: PqcCreateEventCallback | None = None

    def _event(self, name: str, status: str, detail: str) -> None:
        if self.event_callback is None:
            return
        try:
            self.event_callback(name, status, detail)
        except Exception:
            return

    @staticmethod
    def _history_serials(snapshot: PqcQuerySnapshot, project_number: str) -> tuple[dict[str, set[int]], int]:
        by_item: dict[str, set[int]] = {}
        maximum = 0
        history = [
            (item.item_code, item.trigger_basis) for item in snapshot.formal_records
        ] + [
            (item.item_code, item.trigger_basis) for item in snapshot.drafts
        ]
        for item_code, trigger_basis in history:
            parsed = parse_trigger_basis(project_number, trigger_basis)
            if parsed is None:
                continue
            serial, _type_code = parsed
            by_item.setdefault(item_code, set()).add(serial)
            maximum = max(maximum, serial)
        return by_item, maximum

    def _readback(
        self,
        selection: PqcDraftSelection,
        trigger_basis: str,
        qc_engineer: str,
        *,
        created_not_before: datetime | None = None,
    ) -> tuple[PqcDraftRecord, ...]:
        template_code = _text(selection.procedure_code)
        template_key = _required_source_text(selection.template_key, "模板键")
        if not template_code:
            raise ProcessEndApiError("草稿回查缺少复合模板编码")
        kwargs: dict[str, object] = {"created_not_before": created_not_before}
        kwargs["template_key"] = template_key
        return self.query.matching_drafts(
            selection.source.project_number,
            selection.source.item_code,
            template_code,
            trigger_basis,
            qc_engineer,
            **kwargs,
        )

    @staticmethod
    def _result(
        selection: PqcDraftSelection,
        trigger_basis: str,
        status: PqcCreationStatus,
        detail: str,
    ) -> PqcCreationResult:
        return PqcCreationResult(
            source_rows=selection.source.source_rows,
            project_number=selection.source.project_number,
            item_code=selection.source.item_code,
            type_code=selection.type_code,
            trigger_basis=trigger_basis,
            status=status,
            detail=detail,
            tools=tuple(item.display_text() for item in selection.tools),
            template_key=selection.template_key,
            procedure_code=selection.procedure_code,
        )

    def create_serially(
        self, selections: Iterable[PqcDraftSelection], *, qc_engineer: object
    ) -> PqcCreationBatch:
        # Only the selected engineer name is a local payload field.  The backend
        # authenticated request decides creator and department for the draft.
        engineer_name = _selected_engineer_name(qc_engineer)
        ordered = sorted(
            selections,
            key=lambda item: (item.source.project_number, item.source.first_source_row, item.type_code),
        )
        results: list[PqcCreationResult] = []
        batch_serials: dict[tuple[str, str], int] = {}
        reserved_max: dict[str, int] = {}

        for selection in ordered:
            source = selection.source
            expected_code = _text(selection.procedure_code)
            expected_key = _text(selection.template_key)
            if not expected_key or not expected_code or not _text(selection.model_id) or not _text(selection.procedure_name) or not _text(selection.classification):
                results.append(self._result(selection, "", PqcCreationStatus.FAILED, "候选复合模板身份不完整，禁止创建"))
                continue
            preview = parse_trigger_basis(
                selection.source.project_number,
                selection.preview_trigger_basis,
            )
            if preview is None:
                results.append(
                    self._result(
                        selection,
                        selection.preview_trigger_basis,
                        PqcCreationStatus.FAILED,
                        "预览表单编号无效，请刷新候选",
                    )
                )
                continue
            preview_serial = preview[0]
            self._event("pqc_create_recheck", "STARTED", f"保存前实时重查 {source.item_code} {selection.type_code}")
            try:
                snapshot = self.query.load_project_snapshot(source.project_number, include_tools=False)
                fresh_device = build_device_candidates(
                    PqcProject(source.project_number, source.project_name, (source,)), snapshot
                )[0]
                fresh_type = fresh_device.type_candidate(selection.type_code)
                serials_by_item, live_max = self._history_serials(snapshot, source.project_number)
            except Exception as exc:
                results.append(
                    self._result(selection, "", PqcCreationStatus.FAILED, f"保存前只读重查失败：{type(exc).__name__}")
                )
                continue

            if fresh_type.state in {PqcCandidateState.FORMAL, PqcCandidateState.EXCLUDED, PqcCandidateState.BLOCKED}:
                results.append(self._result(selection, fresh_type.trigger_basis, PqcCreationStatus.SKIPPED, fresh_type.detail))
                continue
            fresh_suffix = fresh_type.trigger_basis.rsplit("-", 1)[-1] if fresh_type.trigger_basis else ""
            if (
                fresh_type.template_key != expected_key
                or fresh_type.procedure_code != expected_code
                or fresh_type.model_id != selection.model_id
                or fresh_type.inspection_type.procedure_name != selection.procedure_name
                or (selection.form_suffix and fresh_suffix != selection.form_suffix)
                or (selection.classification and classify_source(source) != selection.classification)
            ):
                results.append(self._result(selection, fresh_type.trigger_basis, PqcCreationStatus.FAILED, "实时模板身份或 SS 已变化，请刷新候选"))
                continue
            if fresh_type.state is PqcCandidateState.DRAFT and not selection.allow_existing_draft:
                results.append(
                    self._result(selection, fresh_type.trigger_basis, PqcCreationStatus.SKIPPED, "实时重查发现已有同类型草稿")
                )
                continue

            key = (source.project_number, source.item_code)
            historical_serials = serials_by_item.get(source.item_code, set())
            if len(historical_serials) > 1:
                results.append(self._result(selection, "", PqcCreationStatus.FAILED, "同一设备存在多个历史流水号"))
                continue
            cached = batch_serials.get(key)
            if historical_serials:
                serial = next(iter(historical_serials))
                claimed_by = {
                    item for item, values in serials_by_item.items() if serial in values and item != source.item_code
                }
                if claimed_by:
                    results.append(self._result(selection, "", PqcCreationStatus.FAILED, "项目流水号已被其他设备使用，请刷新"))
                    continue
                if cached is not None and cached != serial:
                    results.append(self._result(selection, "", PqcCreationStatus.FAILED, "实时编号与本批已分配编号冲突"))
                    continue
            elif cached is not None:
                serial = cached
                # A different device claiming the locally reserved number is
                # a cross-computer collision.  Do not auto-renumber/retry.
                claimed_by = {
                    item for item, values in serials_by_item.items() if serial in values and item != source.item_code
                }
                if claimed_by:
                    results.append(self._result(selection, "", PqcCreationStatus.FAILED, "另一设备已占用本批编号，请刷新"))
                    continue
            else:
                serial = max(live_max, reserved_max.get(source.project_number, 0)) + 1
            if serial != preview_serial:
                results.append(
                    self._result(
                        selection,
                        selection.preview_trigger_basis,
                        PqcCreationStatus.FAILED,
                        "实时编号已与预览不一致，可能有其他导入端占号，请刷新",
                    )
                )
                continue
            if serial > 999:
                results.append(self._result(selection, "", PqcCreationStatus.FAILED, "项目流水号超过 999"))
                continue
            batch_serials[key] = serial
            reserved_max[source.project_number] = max(reserved_max.get(source.project_number, 0), serial)
            # SS is deterministic from the selected inspection type.
            suffix = form_suffix_for_type(selection.type_code)
            trigger_basis = f"{source.project_number}-{serial:03d}-{suffix}"

            try:
                before = self._readback(selection, trigger_basis, engineer_name)
            except Exception as exc:
                results.append(
                    self._result(selection, trigger_basis, PqcCreationStatus.FAILED, f"POST 前草稿回查失败：{type(exc).__name__}")
                )
                continue
            before_ids = {item.draft_id for item in before}

            try:
                context_code = fresh_type.procedure_code
                context = self.api.get_model_context(
                    fresh_type.model_id, context_code,
                    expected_template_name=fresh_type.inspection_type.procedure_name,
                    expected_template_key=fresh_type.template_key,
                    expected_form_suffix=suffix,
                )
                payload = build_processend_draft_payload(
                    source,
                    type_code=selection.type_code,
                    model_id=fresh_type.model_id,
                    trigger_basis=trigger_basis,
                    qc_engineer=engineer_name,
                    model_context=context,
                    completion_day=self.today(),
                    tools=selection.tools,
                    template_key=selection.template_key or fresh_type.template_key,
                    procedure_code=context_code,
                    procedure_name=fresh_type.inspection_type.procedure_name,
                    form_suffix=suffix,
                )
            except Exception as exc:
                results.append(
                    self._result(selection, trigger_basis, PqcCreationStatus.FAILED, f"模板或 payload 校验失败：{type(exc).__name__}")
                )
                continue

            # The readback window starts just before the one and only POST.
            # Round down and include a small clock/DB precision allowance.
            readback_start = self.now().replace(microsecond=0) - timedelta(minutes=5)
            post_status = PqcCreationStatus.SUCCESS
            post_detail = "接口已确认保存"
            try:
                self.api.save_draft(payload, selected_engineer=qc_engineer)
            except ProcessEndIdentityError as exc:
                post_status = PqcCreationStatus.FAILED
                post_detail = str(exc)
            except ProcessEndPostRejected as exc:
                post_status = PqcCreationStatus.FAILED
                post_detail = str(exc)
            except Exception as exc:
                post_status = PqcCreationStatus.UNKNOWN
                post_detail = f"POST 结果不确定且未自动重试：{type(exc).__name__}"

            try:
                after = self._readback(
                    selection,
                    trigger_basis,
                    engineer_name,
                    created_not_before=readback_start,
                )
                new_ids = {item.draft_id for item in after} - before_ids
            except Exception as exc:
                results.append(
                    self._result(
                        selection,
                        trigger_basis,
                        PqcCreationStatus.UNKNOWN,
                        f"{post_detail}；POST 后回查失败：{type(exc).__name__}",
                    )
                )
                continue

            if post_status is PqcCreationStatus.SUCCESS and len(new_ids) == 1:
                detail = "草稿保存成功并完成唯一回查"
                final_status = PqcCreationStatus.SUCCESS
            elif post_status is PqcCreationStatus.FAILED and not new_ids:
                detail = post_detail + "；回查未发现新增草稿"
                final_status = PqcCreationStatus.FAILED
            else:
                detail = post_detail + f"；回查新增匹配数={len(new_ids)}，未自动重试"
                final_status = PqcCreationStatus.UNKNOWN
            results.append(self._result(selection, trigger_basis, final_status, detail))
            self._event("pqc_create_result", final_status.value, detail)

        return PqcCreationBatch(tuple(results))


__all__ = [
    "PROCESSEND_IDENTITY_PATH",
    "PROCESSEND_MODEL_CONTEXT_PATH",
    "PROCESSEND_SAVE_DRAFT_PATH",
    "ProcessEndApiError",
    "ProcessEndApiIdentity",
    "ProcessEndIdentityError",
    "ProcessEndDraftApiClient",
    "ProcessEndPostRejected",
    "ProcessEndPostUncertain",
    "PqcDraftCreationService",
    "build_processend_draft_payload",
    "parse_processend_api_identity",
    "parse_processend_model_context",
]
