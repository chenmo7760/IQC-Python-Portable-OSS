from __future__ import annotations

from pathlib import Path
import os

from .config import (
    AppPaths,
    application_root,
    ensure_runtime_dirs,
    load_entry_api_base_url,
    load_import_authorization,
    resolve_readonly_mysql_config_path,
)
from .generation_service import (
    GenerationService,
    ResolvedOrderProvider,
    RuntimeGenerationProvider,
    UnconfiguredGenerationProvider,
)
from .import_orchestrator import ImportOrchestrator, WindowsBatExecutor
from .inspection_adjustment_excel import InspectionAdjustmentService
from .mysql_query import ReadonlyMysqlClient, ReadonlyQueryService
from .paper_standard_exclusion import PaperStandardCatalog
from .pqc_query import PqcReadonlyQueryService
from .ui import IqcApp
from .entry_api import EntryDraftApiClient, UrllibEntryApiTransport
from .processend_api import ProcessEndDraftApiClient
from .windchill_adapter import (
    InformationXlsBackend,
    WindchillTechnicalSpecificationExporterAdapter,
    XlrdInformationXlsBackend,
)


def load_entry_api_auth_headers(environ: object = None) -> dict[str, str]:
    """Load one opaque Authorization value without logging or persisting it.

    The preferred source is ``IQC_ENTRY_API_AUTHORIZATION``.  A workstation
    may instead point ``IQC_ENTRY_API_AUTH_FILE`` at an absolute, access-
    controlled text file whose sole non-empty line is the complete header
    value (for example ``JWT ...``).  Package configuration and engineer IDs
    are intentionally not authentication sources.
    """

    source = os.environ if environ is None else environ
    getter = getattr(source, "get", None)
    if not callable(getter):
        return {}
    value = str(getter("IQC_ENTRY_API_AUTHORIZATION", "") or "").strip()
    if not value:
        path_text = str(getter("IQC_ENTRY_API_AUTH_FILE", "") or "").strip()
        if path_text:
            path = Path(path_text)
            if path.is_absolute() and path.is_file():
                try:
                    lines = [
                        line.strip()
                        for line in path.read_text(encoding="utf-8-sig").splitlines()
                        if line.strip()
                    ]
                except (OSError, UnicodeError):
                    lines = []
                if len(lines) == 1:
                    value = lines[0]
    if not value or "\r" in value or "\n" in value:
        return {}
    return {"Authorization": value}


def build_generation_service(
    paths: AppPaths,
    provider: ResolvedOrderProvider | None = None,
    *,
    enable_runtime: bool = False,
    information_backend: InformationXlsBackend | None = None,
) -> GenerationService:
    """构造生成服务，默认保持显式阻断，现场适配器只能由调用方注入。

    ``provider`` 必须已经绑定明确的只读快照和 Windchill 名称结果；本函数
    不读取配置、不启动外部程序，也不猜测模型、人员或检查字段。这样测试、
    现场只读联调和桌面入口共享同一安全边界，而交付包在未配置适配器时仍会
    给出可见的“生成依赖未配置”提示。
    """

    selected_provider = provider
    if selected_provider is None and enable_runtime:
        selected_provider = build_runtime_generation_provider(paths, information_backend)
    if selected_provider is None:
        selected_provider = UnconfiguredGenerationProvider()
    return GenerationService(paths.template_path, paths.output_dir, selected_provider)


def build_runtime_generation_provider(
    paths: AppPaths,
    information_backend: InformationXlsBackend | None = None,
    paper_standard_catalog: PaperStandardCatalog | None = None,
) -> ResolvedOrderProvider:
    """按现场文件构造运行时 provider；BIFF backend 未注入时安全阻断。

    这里只检查配置/运行时文件存在性，不读取 ``mysql_connection.bat`` 的
    内容，也不启动 BAT、Windchill EXE 或数据库。``information_backend``
    是明确的依赖注入口；在旧版 BIFF 协议未被现场确认前绝不猜测实现。
    """

    config_path = resolve_readonly_mysql_config_path(paths.project_root)
    required = (
        ("MySQL 连接配置", config_path),
        ("只读查询 BAT", paths.readonly_query_bat),
        ("Windchill EXE", paths.windchill_exe),
        ("information.xls", paths.information_xls),
    )
    missing = [label for label, path in required if not path.is_file()]
    if information_backend is None:
        if XlrdInformationXlsBackend.dependencies_available():
            information_backend = XlrdInformationXlsBackend()
        else:
            missing.append("information.xls BIFF backend")
    if missing:
        return UnconfiguredGenerationProvider(
            "生成运行时依赖未完整配置：" + ", ".join(missing) + "；未执行 MySQL 或 Windchill 操作。"
        )
    client = ReadonlyMysqlClient(paths.readonly_query_bat)
    # 只记录配置来源/哈希、编码和退出码，不记录 SQL、密码或配置正文。
    client.diagnostic_log_path = paths.log_dir / "mysql_query_diagnostic.jsonl"
    query_service = ReadonlyQueryService(client)
    adapter = WindchillTechnicalSpecificationExporterAdapter(
        exe_path=paths.windchill_exe,
        information_xls=paths.information_xls,
        backend=information_backend,
    )
    return RuntimeGenerationProvider(
        query_service,
        adapter,
        mysql_config_path=config_path,
        procedure_aliases_path=paths.procedure_aliases,
        paper_standard_catalog=paper_standard_catalog,
    )


def main(generation_provider: ResolvedOrderProvider | None = None) -> None:
    project_root = application_root()
    paths = AppPaths.from_project_root(project_root)
    ensure_runtime_dirs(paths)
    # WO-09 接入编排器但保持真实执行锁定；只有后续明确授权并显式启用执行器，
    # 才会进入 Windows BAT 进程边界。
    importer = ImportOrchestrator(
        paths.project_root / "运行时" / "db" / "run_generated_sql.bat",
        WindowsBatExecutor(authorized=load_import_authorization(paths.project_root / "运行时" / "db" / "mysql_connection.bat")),
        state_path=paths.log_dir / "iqc_import_state.json",
        report_dir=paths.log_dir,
    )
    # 生成按钮已经接入模板/SQL/BAT/诊断编排层；真实只读快照和 Windchill
    # BIFF backend 尚未配置时，使用显式阻断提供者，绝不静默跳过或猜填字段。
    generator = build_generation_service(paths, generation_provider, enable_runtime=True)
    # Entry API 地址优先由显式环境变量覆盖，其次读取包内可编辑配置；
    # 两者均为空时保持历史 BAT 提交流程，不在源码中写死现场地址，
    # 也不在启动阶段发起网络请求。
    entry_base_url = os.environ.get("IQC_ENTRY_API_BASE_URL", "").strip()
    if not entry_base_url:
        entry_base_url = load_entry_api_base_url(paths.project_root / "config" / "launcher.json")
    entry_headers = load_entry_api_auth_headers()
    transport = (
        UrllibEntryApiTransport(entry_base_url, headers=entry_headers)
        if entry_base_url
        else None
    )
    entry_client = EntryDraftApiClient(transport) if transport is not None else None
    # PQC 只读服务只保存 BAT 路径；启动和切换页签时不查数据库。
    # 真正 SELECT 只在操作员完成第二步并点击“加载设备候选”后执行。
    pqc_client = ReadonlyMysqlClient(paths.readonly_query_bat)
    # PQC 与普通生成共用同一条只读诊断链，现场失败可按 query_stage 区分。
    pqc_client.diagnostic_log_path = paths.log_dir / "mysql_query_diagnostic.jsonl"
    pqc_query_service = PqcReadonlyQueryService(pqc_client)
    pqc_entry_client = ProcessEndDraftApiClient(transport) if transport is not None else None
    inspection_adjustment_service = InspectionAdjustmentService()
    app = IqcApp(
        paths,
        importer=importer,
        generator=generator,
        entry_client=entry_client,
        pqc_query_service=pqc_query_service,
        pqc_entry_client=pqc_entry_client,
        inspection_adjustment_service=inspection_adjustment_service,
    )
    app.mainloop()


if __name__ == "__main__":
    main()
