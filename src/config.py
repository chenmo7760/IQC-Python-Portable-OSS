from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Mapping


_IMPORT_AUTHORIZED_RE = re.compile(
    r'^\s*set\s+"IQC_IMPORT_AUTHORIZED\s*=\s*(?P<value>[^"]*)"\s*$',
    re.IGNORECASE,
)
_TRUE_CONFIG_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_PQC_SUFFIX_RE = re.compile(r"^[0-9]{2}$", re.ASCII)


def application_root() -> Path:
    """返回源码目录或免安装包中 IQC 可执行文件所在目录。"""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AppPaths:
    """集中保存路径，避免代码依赖当前工作目录。"""

    project_root: Path
    template_path: Path
    output_dir: Path
    log_dir: Path
    windchill_dir: Path
    windchill_exe: Path
    information_xls: Path
    readonly_query_bat: Path
    engineer_config: Path
    # 可由现场人工维护的检查单别名；缺失时不启用任何别名，仍按严格模型匹配。
    procedure_aliases: Path | None = None

    @classmethod
    def from_project_root(cls, project_root: Path) -> "AppPaths":
        root = project_root.resolve()
        runtime = root / "运行时"
        # Windchill v1.1 的完整运行目录固定放在 ``运行时/squery_item_name``。
        # 该路径是打包后和源码运行时的同一入口，避免误用旧版或根目录副本。
        windchill_dir = runtime / "squery_item_name"
        # 输出目录可由现场显式指定；相对路径相对应用目录解析，避免继承
        # 制作者机器的绝对路径（例如 <user-output-directory>）。
        env_output = os.environ.get("IQC_OUTPUT_DIR", "").strip()
        configured = env_output or _load_launcher_string(root / "config" / "launcher.json", "output_dir")
        output_dir = Path(configured).expanduser() if configured else root / "输出"
        # launcher.json 随包发布，禁止其中携带制作者机器的绝对路径；现场若需
        # 指定绝对输出目录必须通过 IQC_OUTPUT_DIR 显式注入。
        if output_dir.is_absolute() and not env_output:
            output_dir = root / "输出"
        if not output_dir.is_absolute():
            output_dir = root / output_dir
        output_dir = output_dir.resolve()
        return cls(
            project_root=root,
            template_path=root / "template" / "iqc_draft_template.xlsx",
            output_dir=output_dir,
            log_dir=root / "日志",
            windchill_dir=windchill_dir,
            windchill_exe=windchill_dir / "WindchillTechnicalSpecificationExporter.exe",
            information_xls=windchill_dir / "information.xls",
            readonly_query_bat=runtime / "db" / "run_readonly_query.bat",
            engineer_config=root / "config" / "engineers.json",
            procedure_aliases=root / "config" / "procedure_aliases.json",
        )


def load_import_authorization(config_path: Path) -> bool:
    """Read only the non-secret import authorization flag from the DB BAT.

    The connection BAT remains user-maintained and may contain credentials. We
    scan it line-by-line for the exact quoted flag and never return, log, or
    otherwise retain any other setting. Missing, malformed, or unreadable
    configuration fails closed.
    """

    try:
        with Path(config_path).open("r", encoding="utf-8-sig", errors="replace") as stream:
            for line in stream:
                match = _IMPORT_AUTHORIZED_RE.fullmatch(line.rstrip("\r\n"))
                if match is not None:
                    return match.group("value").strip().lower() in _TRUE_CONFIG_VALUES
    except OSError:
        return False
    return False


def _load_launcher_string(config_path: Path, key: str) -> str:
    try:
        raw = Path(config_path).read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp936")
        payload = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    value = payload.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def load_entry_api_base_url(config_path: Path) -> str:
    """读取可选的 Entry API 根地址；配置错误时安全回退为空字符串。

    ``config_path`` 通常是包内 ``config/launcher.json``。启动器已经对该
    文件做过合法性检查，这里仍独立按 UTF-8/UTF-8 BOM/cp936 读取，便于
    源码入口和打包后的 EXE 保持一致。环境变量覆盖逻辑由 ``src.main``
    负责；本函数不发起网络请求，也不记录地址内容。
    """

    return _load_launcher_string(config_path, "entry_api_base_url")


def load_paper_standard_catalog_path(config_path: Path) -> str:
    """读取默认线下规范清单路径；缺失、非法或非字符串时返回空字符串。

    该值只用于 UI 默认填充。真正读取前仍由 ``src.ui`` 检查文件是否存在
    和扩展名是否支持，因此空配置或现场文件缺失不会自动跳过线下清单门禁。
    """

    return _load_launcher_string(config_path, "paper_standard_catalog_path")


def load_pqc_template_suffix_map(
    config_path: Path,
    environ: object = None,
    *,
    template_keys: tuple[str, ...] = (),
) -> dict[str, str]:
    """Load the audited PQC template-key -> real SS proof map.

    The map is an explicit, read-only operator configuration.  It is never
    synthesized from a UI slot, procedure-code suffix, or template ordering.
    A missing/malformed file or environment value returns an empty map so the
    downstream candidate builder remains fail-closed for every unmapped
    template.  Partial maps are intentionally retained: mapped templates can
    be previewed while each missing template stays blocked.
    """

    source = os.environ if environ is None else environ
    getter = getattr(source, "get", None)
    raw: object = None
    if callable(getter):
        inline = getter("IQC_PQC_TEMPLATE_SUFFIX_MAP", "")
        map_file = getter("IQC_PQC_TEMPLATE_SUFFIX_MAP_FILE", "")
        if isinstance(inline, str) and inline.strip():
            raw = inline
        elif isinstance(map_file, str) and map_file.strip():
            path = Path(map_file.strip())
            if not path.is_absolute() or not path.is_file():
                return {}
            try:
                raw = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                return {}
    if raw is None:
        try:
            raw = Path(config_path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return {}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    expected = set(template_keys)
    result: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or (expected and key not in expected):
            return {}
        if not isinstance(value, str) or _PQC_SUFFIX_RE.fullmatch(value.strip()) is None:
            return {}
        result[key.strip()] = value.strip()
    if len(result) != len(payload):
        return {}
    return result


def resolve_readonly_mysql_config_path(
    project_root: Path,
    environ: object = None,
) -> Path:
    """Return the adjacent config used by the user-pinned r44 readonly BAT.

    r44 ignores ``IQC_MYSQL_CONFIG_FILE``. Keep ``environ`` in the signature
    for callers, but never let an external file satisfy the local precheck.
    This helper does not read credential contents or execute the config.
    """

    return Path(project_root).resolve() / "运行时" / "db" / "mysql_connection.bat"


def ensure_runtime_dirs(paths: AppPaths) -> None:
    """只创建本地输出/日志目录，不触碰数据库或 Windchill。"""

    paths.output_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
