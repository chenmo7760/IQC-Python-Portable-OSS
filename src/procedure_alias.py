"""Explicit, operator-maintained Windchill to IQC procedure aliases.

Aliases are intentionally exact only.  They are not a fuzzy-matching layer:
an item-code alias wins over a source/normalized-name alias, and duplicate
keys or disabled aliases fail closed so a typo cannot silently select a
different inspection model.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import normalize_procedure_name


# 合成示例：不可拆分的严格别名组；默认空配置不启用该组。
SYNTHETIC_GROUP_ITEM_CODES = frozenset((
    "DEMO-MAT-017", "DEMO-MAT-002", "DEMO-MAT-013", "DEMO-MAT-015",
    "DEMO-MAT-022", "DEMO-MAT-019", "DEMO-MAT-024", "DEMO-MAT-035",
))
SYNTHETIC_GROUP_SOURCE_NAME = "DemoLogic定型优化PCM进货检验规范"
SYNTHETIC_GROUP_EXCLUDE_KEY = "DEMO-DEMO18-DEMOSYS-A-DEMOFORM-DOC101-002"
SYNTHETIC_GROUP_TARGET_NAME = "附录 A DemoLogic定型优化PCM进货检验记录表"


class ProcedureAliasError(ValueError):
    """Configuration is malformed or an explicit alias cannot be applied."""


@dataclass(frozen=True)
class ProcedureAlias:
    enabled: bool
    item_code: str
    source_name: str
    normalized_name: str
    target_procedure_name: str
    note: str
    version: str
    effective_date: str
    rule_id: str = ""
    strict_target_match: bool = False
    config_sha256: str = ""

    @property
    def has_item_key(self) -> bool:
        return bool(self.item_code)

    @property
    def has_name_key(self) -> bool:
        return bool(self.source_name or self.normalized_name)


@dataclass(frozen=True)
class AliasResolution:
    """The exact alias selected for one Windchill result, if any."""

    alias: ProcedureAlias
    matched_by: str

    @property
    def target_procedure_name(self) -> str:
        return self.alias.target_procedure_name


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _bool(value: Any, *, field: str, index: int) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
    raise ProcedureAliasError(f"第 {index} 条映射字段 {field} 必须是布尔值")


def _row_to_alias(row: Mapping[str, Any], index: int) -> ProcedureAlias:
    if not isinstance(row, Mapping):
        raise ProcedureAliasError(f"第 {index} 条映射必须是对象")
    enabled = _bool(row.get("enabled", True), field="enabled", index=index)
    strict_target_match = row.get("strict_target_match", False)
    if not isinstance(strict_target_match, bool):
        raise ProcedureAliasError(f"第 {index} 条映射字段 strict_target_match 必须是布尔值")
    item_code = _text(row.get("item_code"))
    source_name = _text(row.get("source_name", row.get("source")))
    normalized_name = _text(row.get("normalized_name"))
    target = _text(row.get("target_procedure_name"))
    if not item_code and not source_name and not normalized_name:
        raise ProcedureAliasError(f"第 {index} 条映射必须提供 item_code/source_name/normalized_name")
    if enabled and not target:
        raise ProcedureAliasError(f"第 {index} 条启用映射缺少 target_procedure_name")
    if normalized_name:
        normalized_name = normalize_procedure_name(normalized_name)
    return ProcedureAlias(
        enabled=enabled,
        item_code=item_code,
        source_name=source_name,
        normalized_name=normalized_name,
        target_procedure_name=target,
        note=_text(row.get("note")),
        version=_text(row.get("version")),
        effective_date=_text(row.get("effective_date")),
        rule_id=_text(row.get("rule_id")),
        strict_target_match=strict_target_match,
    )


def load_procedure_aliases(path: Path | None) -> "ProcedureAliasResolver":
    """Load UTF-8 JSON alias configuration; missing file means no aliases.

    The source CSV supplied by operators is deliberately not read or rewritten
    at runtime.  Its confirmed values are copied into the JSON configuration,
    which is easier to review, package, and hand-maintain beside the app.
    """

    if path is None or not Path(path).is_file():
        return ProcedureAliasResolver(())
    payload: Any = None
    last_error: BaseException | None = None
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ProcedureAliasError(f"别名配置读取失败：{Path(path)}：{type(exc).__name__}") from exc
    # UTF-8 is the delivered format; GB18030 fallback keeps a hand-maintained
    # file readable when an older Windows editor saves it in the local codepage.
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            payload = json.loads(raw.decode(encoding))
            break
        except (UnicodeError, json.JSONDecodeError) as exc:
            last_error = exc
    if payload is None:
        error_name = type(last_error).__name__ if last_error is not None else "UnknownError"
        raise ProcedureAliasError(f"别名配置读取失败：{Path(path)}：{error_name}") from last_error
    if isinstance(payload, Mapping):
        rows = payload.get("aliases")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ProcedureAliasError("别名配置必须是 aliases 数组或数组根对象")
    config_sha256 = hashlib.sha256(raw).hexdigest()
    aliases = tuple(
        replace(_row_to_alias(row, index), config_sha256=config_sha256)
        for index, row in enumerate(rows, start=1)
    )
    controlled = tuple(alias for alias in aliases if alias.item_code in SYNTHETIC_GROUP_ITEM_CODES)
    if controlled and (
        len(controlled) != len(SYNTHETIC_GROUP_ITEM_CODES)
        or {alias.item_code for alias in controlled} != SYNTHETIC_GROUP_ITEM_CODES
        or any(
            not alias.enabled or not alias.strict_target_match
            or alias.rule_id != f"full_confirmed_50299821_{alias.item_code}"
            or alias.source_name != SYNTHETIC_GROUP_SOURCE_NAME
            or alias.normalized_name != SYNTHETIC_GROUP_SOURCE_NAME
            or alias.target_procedure_name != SYNTHETIC_GROUP_TARGET_NAME
            for alias in controlled
        )
    ):
        raise ProcedureAliasError("DEMO-GROUP 优化 PCM 的八条配置必须完整且一致指向已确认严格目标，拒绝旧配置或部分迁移")
    return ProcedureAliasResolver(aliases)


class ProcedureAliasResolver:
    """Resolve explicit aliases by exact item code, then exact name keys."""

    def __init__(self, aliases: Iterable[ProcedureAlias]) -> None:
        self.aliases = tuple(aliases)
        self._item: dict[str, ProcedureAlias] = {}
        self._source: dict[str, ProcedureAlias] = {}
        self._normalized: dict[str, ProcedureAlias] = {}
        self._disabled_item: set[str] = set()
        self._disabled_source: set[str] = set()
        self._disabled_normalized: set[str] = set()
        # Item-specific aliases may coexist with a generic name alias because
        # item_code has higher precedence.  Retain their targets separately so
        # two different item-specific targets on one name still fail closed.
        self._specific_source_targets: dict[str, set[str]] = {}
        self._specific_normalized_targets: dict[str, set[str]] = {}
        for alias in self.aliases:
            self._register(alias)

    @property
    def enabled_count(self) -> int:
        return sum(1 for alias in self.aliases if alias.enabled)

    def _register(self, alias: ProcedureAlias) -> None:
        if alias.normalized_name:
            alias = replace(alias, normalized_name=normalize_procedure_name(alias.normalized_name))
        # Validate the same constraints for programmatic fixtures as for JSON.
        if alias.enabled and not alias.target_procedure_name.strip():
            raise ProcedureAliasError("启用映射缺少 target_procedure_name")
        self._register_key(self._item, self._disabled_item, alias.item_code, alias, "item_code")
        if alias.item_code:
            self._register_specific_name(
                self._specific_source_targets,
                alias.source_name,
                alias,
                "source_name",
            )
            self._register_specific_name(
                self._specific_normalized_targets,
                alias.normalized_name,
                alias,
                "normalized_name",
            )
        else:
            self._register_key(self._source, self._disabled_source, alias.source_name, alias, "source_name")
            self._register_key(
                self._normalized,
                self._disabled_normalized,
                alias.normalized_name,
                alias,
                "normalized_name",
            )

    @staticmethod
    def _register_specific_name(
        targets_by_key: dict[str, set[str]],
        key: str,
        alias: ProcedureAlias,
        label: str,
    ) -> None:
        if not key or not alias.enabled:
            return
        targets = targets_by_key.setdefault(key, set())
        targets.add(alias.target_procedure_name)
        if len(targets) > 1:
            raise ProcedureAliasError(f"别名 {label}={key!r} 存在多个目标，拒绝启动")

    @staticmethod
    def _register_key(
        index: dict[str, ProcedureAlias],
        disabled: set[str],
        key: str,
        alias: ProcedureAlias,
        label: str,
    ) -> None:
        if not key:
            return
        if not alias.enabled:
            disabled.add(key)
            return
        previous = index.get(key)
        if previous is not None and (
            previous.target_procedure_name != alias.target_procedure_name
            or previous.item_code != alias.item_code
        ):
            raise ProcedureAliasError(f"别名 {label}={key!r} 存在多个目标，拒绝启动")
        if key in disabled:
            raise ProcedureAliasError(f"别名 {label}={key!r} 同时存在启用和禁用配置")
        index[key] = alias

    def resolve(
        self,
        *,
        item_code: object,
        source_name: object,
        normalized_name: object | None = None,
    ) -> AliasResolution | None:
        """Resolve without fuzzy matching; disabled exact keys fail closed."""

        code = _text(item_code)
        source = _text(source_name)
        normalized = _text(normalized_name) or normalize_procedure_name(source)
        if code in self._disabled_item:
            raise ProcedureAliasError(f"物项编码 {code} 的别名已禁用，拒绝回退匹配")
        alias = self._item.get(code)
        if alias is not None:
            return AliasResolution(alias, "item_code")
        if source in self._disabled_source:
            raise ProcedureAliasError(f"原始名称 {source!r} 的别名已禁用，拒绝回退匹配")
        alias = self._source.get(source)
        if alias is not None:
            return AliasResolution(alias, "source_name")
        if normalized in self._disabled_normalized:
            raise ProcedureAliasError(f"规范化名称 {normalized!r} 的别名已禁用，拒绝回退匹配")
        alias = self._normalized.get(normalized)
        if alias is not None:
            return AliasResolution(alias, "normalized_name")
        return None
