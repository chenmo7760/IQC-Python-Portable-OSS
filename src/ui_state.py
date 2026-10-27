"""Pure WO-09 desktop state machine, independent from Tk and external I/O."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Iterable, Mapping, Sequence

from .import_orchestrator import ImportRequest, ImportStatus, OrderImportResult


class UiOrderStatus(str, Enum):
    WAITING = "待处理"
    READ_SUCCESS = "读取成功"
    QUERY_SUCCESS = "查询成功"
    WINDCHILL_SUCCESS = "Windchill成功"
    GENERATED_CONFIRM = "已生成待确认"
    IMPORTING = "导入中"
    COMMITTED = "已提交"
    ROLLED_BACK_RETRYABLE = "已回滚/可重导"
    ERROR_RETRYABLE = "异常/可重试"
    CANCELLED = "已取消"
    ERROR_SKIPPED = "异常/跳过"
    BLOCKED = "阻断"
    PAPER_ONLY = "纸质记录/不导入"


@dataclass
class UiOrder:
    order_number: str
    sql_path: Path
    row_count: int = 0
    result_path: Path | None = None
    status: UiOrderStatus = UiOrderStatus.WAITING
    confirmed: bool = False
    retry_failed: bool = False
    cancel_requested: bool = False
    attempt: int = 0
    detail: str = ""
    # Kept at the end to preserve the existing positional UiOrder API.
    duplicate_confirmed: bool = False


class UiStateError(ValueError):
    """Invalid UI state transition or duplicate order."""


class UiBatchState:
    """Thread-safe, deterministic state transitions for the single-window UI."""

    def __init__(self) -> None:
        self._orders: dict[str, UiOrder] = {}
        self._lock = RLock()

    def stage(
        self,
        order_number: str,
        sql_path: Path,
        *,
        row_count: int = 0,
        result_path: Path | None = None,
        status: UiOrderStatus = UiOrderStatus.READ_SUCCESS,
        detail: str = "",
    ) -> UiOrder:
        order = str(order_number).strip()
        if not order:
            raise UiStateError("送检单号不能为空")
        if row_count < 0:
            raise UiStateError("行数不能为负数")
        with self._lock:
            if order in self._orders:
                raise UiStateError(f"送检单号重复：{order}")
            item = UiOrder(
                order,
                Path(sql_path),
                int(row_count),
                Path(result_path) if result_path is not None else None,
                status=status,
                detail=detail,
            )
            self._orders[order] = item
            return self._copy(item)

    def clear(self) -> None:
        with self._lock:
            if any(item.status is UiOrderStatus.IMPORTING for item in self._orders.values()):
                raise UiStateError("导入进行中不能清空单号")
            self._orders.clear()

    def get(self, order_number: str) -> UiOrder:
        with self._lock:
            try:
                return self._copy(self._orders[order_number])
            except KeyError as exc:
                raise UiStateError(f"未知送检单号：{order_number}") from exc

    def snapshot(self) -> tuple[UiOrder, ...]:
        with self._lock:
            return tuple(self._copy(item) for item in self._orders.values())

    def mark_generated(self, order_number: str, sql_path: Path | None = None) -> UiOrder:
        with self._lock:
            item = self._require(order_number)
            if item.status in {UiOrderStatus.COMMITTED, UiOrderStatus.IMPORTING}:
                raise UiStateError(f"当前状态不能标记为待确认：{item.status.value}")
            if sql_path is not None:
                item.sql_path = Path(sql_path)
            item.status = UiOrderStatus.GENERATED_CONFIRM
            item.confirmed = False
            item.retry_failed = False
            item.detail = "等待人工确认"
            return self._copy(item)

    def confirm(self, order_number: str, *, duplicate_confirmed: bool = False) -> UiOrder:
        with self._lock:
            item = self._require(order_number)
            if type(duplicate_confirmed) is not bool:
                raise UiStateError("重复导入确认标志必须是布尔值")
            if item.status is UiOrderStatus.COMMITTED:
                if not duplicate_confirmed:
                    raise UiStateError("检测到本次为重复导入，必须先确认后才能继续")
                item.confirmed = True
                item.duplicate_confirmed = True
                item.detail = "已确认本次为重复导入，等待逐单导入"
                return self._copy(item)
            if item.status is UiOrderStatus.IMPORTING:
                raise UiStateError(f"当前状态不能确认导入：{item.status.value}")
            if item.status is UiOrderStatus.BLOCKED:
                raise UiStateError("阻断状态必须先解决原因，不能直接确认")
            if item.status is UiOrderStatus.PAPER_ONLY:
                raise UiStateError("纸质记录不生成电子草稿，不能确认导入")
            item.confirmed = True
            # The duplicate may be known only from the persisted
            # ImportOrchestrator history.  After regenerating an order the UI
            # row is ``GENERATED_CONFIRM`` rather than ``COMMITTED``; retain
            # the explicit dialog decision so build_import_requests forwards
            # ``duplicate_confirmed=True`` to the orchestrator.  The caller
            # sets this flag only after the duplicate warning was accepted.
            item.duplicate_confirmed = duplicate_confirmed
            if item.status in {
                UiOrderStatus.WAITING,
                UiOrderStatus.READ_SUCCESS,
                UiOrderStatus.QUERY_SUCCESS,
                UiOrderStatus.WINDCHILL_SUCCESS,
                UiOrderStatus.ROLLED_BACK_RETRYABLE,
                UiOrderStatus.CANCELLED,
            }:
                item.status = UiOrderStatus.GENERATED_CONFIRM
            item.detail = "已人工确认，等待逐单导入"
            return self._copy(item)

    def request_retry(self, order_number: str) -> UiOrder:
        with self._lock:
            item = self._require(order_number)
            if item.status not in {UiOrderStatus.ROLLED_BACK_RETRYABLE, UiOrderStatus.ERROR_RETRYABLE}:
                raise UiStateError("只有已回滚/可重导或未启动异常单号可以申请重试")
            item.confirmed = False
            item.retry_failed = True
            item.duplicate_confirmed = False
            item.status = UiOrderStatus.GENERATED_CONFIRM
            item.detail = "已申请重导，等待再次人工确认"
            return self._copy(item)

    def cancel(self, order_number: str) -> UiOrder:
        with self._lock:
            item = self._require(order_number)
            if item.status is UiOrderStatus.COMMITTED:
                raise UiStateError("已提交单号不能取消")
            if item.status is UiOrderStatus.IMPORTING:
                item.cancel_requested = True
                item.detail = "已请求取消，等待执行器结束"
            else:
                item.status = UiOrderStatus.CANCELLED
                item.confirmed = False
                item.duplicate_confirmed = False
                item.detail = "用户取消，未调用 BAT"
            return self._copy(item)

    def build_import_requests(self, order_numbers: Sequence[str]) -> tuple[ImportRequest, ...]:
        with self._lock:
            requests: list[ImportRequest] = []
            for order_number in order_numbers:
                item = self._require(order_number)
                if not item.confirmed:
                    raise UiStateError(f"单号 {order_number} 尚未人工确认")
                if item.status is UiOrderStatus.COMMITTED and not item.duplicate_confirmed:
                    raise UiStateError(f"单号 {order_number} 为重复导入，必须先确认后才能继续")
                if item.status is UiOrderStatus.BLOCKED:
                    raise UiStateError(f"单号 {order_number} 处于阻断状态")
                if item.status is UiOrderStatus.PAPER_ONLY:
                    raise UiStateError(f"单号 {order_number} 为纸质记录/不导入")
                item.status = UiOrderStatus.IMPORTING
                item.cancel_requested = False
                item.detail = "导入中"
                requests.append(
                    ImportRequest(
                        order_number=item.order_number,
                        sql_path=item.sql_path,
                        confirmed=True,
                        retry_failed=item.retry_failed,
                        duplicate_confirmed=item.duplicate_confirmed,
                    )
                )
            return tuple(requests)

    def begin_entry_submission(self, order_numbers: Sequence[str]) -> tuple[UiOrder, ...]:
        """将已确认单号置为统一“提交中”状态，供 Entry API 复用。

        该状态转换与 ``build_import_requests`` 使用同一确认/重复导入门，
        但不构造 BAT 请求；调用方随后可把聚合后的 SqlRecord 交给注入式
        EntryDraftApiClient。失败时由 ``apply_entry_submission_results`` 标记
        可重试，不会伪造已提交或删除服务端旧草稿。
        """

        with self._lock:
            for order_number in order_numbers:
                item = self._require(order_number)
                if not item.confirmed:
                    raise UiStateError(f"单号 {order_number} 尚未人工确认")
                if item.status is UiOrderStatus.COMMITTED and not item.duplicate_confirmed:
                    raise UiStateError(f"单号 {order_number} 为重复提交，必须先确认后才能继续")
                if item.status is UiOrderStatus.BLOCKED:
                    raise UiStateError(f"单号 {order_number} 处于阻断状态")
                if item.status is UiOrderStatus.PAPER_ONLY:
                    raise UiStateError(f"单号 {order_number} 为纸质记录/不导入")
            for order_number in order_numbers:
                item = self._require(order_number)
                item.status = UiOrderStatus.IMPORTING
                item.cancel_requested = False
                item.detail = "Entry 草稿保存中"
            return self.snapshot()

    def apply_entry_submission_results(
        self,
        results: Mapping[str, tuple[bool, str]],
    ) -> tuple[UiOrder, ...]:
        """应用 Entry API 逐单结果；失败保留可重试状态和旧审计链。"""

        with self._lock:
            for order_number, value in results.items():
                item = self._require(order_number)
                try:
                    accepted, detail = value
                except (TypeError, ValueError) as exc:
                    raise UiStateError(f"Entry 结果格式无效：{order_number}") from exc
                item.detail = str(detail)
                item.cancel_requested = False
                if bool(accepted):
                    item.status = UiOrderStatus.COMMITTED
                    item.confirmed = True
                    item.retry_failed = False
                    item.duplicate_confirmed = False
                else:
                    item.status = UiOrderStatus.ERROR_RETRYABLE
                    item.confirmed = False
                    item.retry_failed = True
                    item.duplicate_confirmed = False
            return self.snapshot()

    def apply_import_results(self, results: Iterable[OrderImportResult]) -> tuple[UiOrder, ...]:
        with self._lock:
            for result in results:
                item = self._require(result.order_number)
                item.attempt = result.attempt
                item.detail = result.diagnostic
                item.cancel_requested = False
                if result.status is ImportStatus.COMMITTED:
                    item.status = UiOrderStatus.COMMITTED
                    item.confirmed = True
                    item.retry_failed = False
                    item.duplicate_confirmed = False
                elif result.status in {
                    ImportStatus.ROLLED_BACK_RETRYABLE,
                    ImportStatus.TIMED_OUT_ROLLED_BACK_RETRYABLE,
                    ImportStatus.CANCELLED_ROLLED_BACK_RETRYABLE,
                }:
                    item.status = UiOrderStatus.ROLLED_BACK_RETRYABLE
                    item.confirmed = False
                    item.retry_failed = True
                    item.duplicate_confirmed = False
                elif result.status is ImportStatus.START_FAILED_RETRYABLE:
                    item.status = UiOrderStatus.ERROR_RETRYABLE
                    item.confirmed = False
                    item.retry_failed = True
                    item.duplicate_confirmed = False
                elif result.status in {
                    ImportStatus.NOT_CONFIRMED,
                    ImportStatus.RETRY_CONFIRMATION_REQUIRED,
                }:
                    item.status = UiOrderStatus.GENERATED_CONFIRM
                    item.confirmed = False
                    item.duplicate_confirmed = False
                elif result.status is ImportStatus.CANCELLED_BEFORE_START:
                    item.status = UiOrderStatus.CANCELLED
                    item.confirmed = False
                    item.duplicate_confirmed = False
                elif result.status is ImportStatus.DUPLICATE_BLOCKED:
                    # A duplicate warning is a user decision, not an
                    # irreversible failure.  Keep the committed state so the
                    # UI can ask again without fabricating a blocked result.
                    item.status = UiOrderStatus.COMMITTED
                    item.confirmed = False
                    item.duplicate_confirmed = False
                elif result.status in {
                    ImportStatus.PREVIOUS_RESULT_UNCERTAIN_BLOCKED,
                    ImportStatus.COMMIT_UNVERIFIED_BLOCKED,
                    ImportStatus.ROLLBACK_UNVERIFIED_BLOCKED,
                    ImportStatus.TIMED_OUT_ROLLBACK_UNVERIFIED_BLOCKED,
                    ImportStatus.CANCELLED_ROLLBACK_UNVERIFIED_BLOCKED,
                }:
                    item.status = UiOrderStatus.BLOCKED
                    item.confirmed = False
                    item.duplicate_confirmed = False
                else:
                    item.status = UiOrderStatus.ERROR_SKIPPED
                    item.confirmed = False
                    item.duplicate_confirmed = False
            return self.snapshot()

    def mark_error(self, order_numbers: Sequence[str], detail: str) -> tuple[UiOrder, ...]:
        """Leave an exceptional worker failure visible without claiming rollback."""

        with self._lock:
            for order_number in order_numbers:
                item = self._require(order_number)
                if item.status is UiOrderStatus.COMMITTED:
                    continue
                item.status = UiOrderStatus.ERROR_SKIPPED
                item.confirmed = False
                item.duplicate_confirmed = False
                item.cancel_requested = False
                item.detail = detail
            return self.snapshot()

    def _require(self, order_number: str) -> UiOrder:
        try:
            return self._orders[order_number]
        except KeyError as exc:
            raise UiStateError(f"未知送检单号：{order_number}") from exc

    @staticmethod
    def _copy(item: UiOrder) -> UiOrder:
        return UiOrder(
            order_number=item.order_number,
            sql_path=item.sql_path,
            row_count=item.row_count,
            result_path=item.result_path,
            status=item.status,
            confirmed=item.confirmed,
            retry_failed=item.retry_failed,
            duplicate_confirmed=item.duplicate_confirmed,
            cancel_requested=item.cancel_requested,
            attempt=item.attempt,
            detail=item.detail,
        )
