"""PyInstaller entry point for the no-install IQC portable directory package."""

import sys

from src.config import AppPaths, application_root
from src.main import main


def portable_self_test() -> int:
    """Check bundled resources without opening the UI or calling an external program."""

    root = application_root()
    paths = AppPaths.from_project_root(root)
    # Windchill v1.1 的完整运行目录固定为 ``运行时\\squery_item_name``；
    # 自检只验证关键文件存在，不启动该 .NET EXE。
    windchill_root = root / "运行时" / "squery_item_name"
    db_config = root / "运行时" / "db" / "mysql_connection.bat"
    required = (
        paths.template_path,
        paths.engineer_config,
        root / "运行时" / "db" / "run_generated_sql.bat",
        root / "运行时" / "db" / "run_readonly_query.bat",
        root / "运行时" / "db" / "mysql_connection.example.bat",
        db_config,
        paths.windchill_exe,
        windchill_root / "WindchillTechnicalSpecificationExporter.exe.config",
        windchill_root / "information.xls",
        windchill_root / "WindchillInterface.dll",
        root / "PACKAGE_MANIFEST_SHA256.txt",
    )
    missing = [str(path) for path in required if not path.is_file()]
    # 可选部署自检要求唯一连接配置；公开源码不附带这些运行时文件。
    # 拒绝重复副本，避免配置来源不明确。
    duplicate_configs = [
        path for path in root.rglob("mysql_connection.bat")
        if path.resolve() != db_config.resolve()
    ]
    if missing or duplicate_configs:
        return 1
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(portable_self_test())
    # 旧快捷方式仍指向 portable_entry.py 时，先转交稳定 launcher；
    # launcher 启动新版会附带 --launched-by-launcher，避免递归回到更新检查。
    if "--launched-by-launcher" not in sys.argv[1:]:
        from launcher import main as launcher_main

        raise SystemExit(launcher_main(sys.argv[1:]))
    main()
