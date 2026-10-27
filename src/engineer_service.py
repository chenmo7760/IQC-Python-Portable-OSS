from __future__ import annotations

import json
from pathlib import Path

from .models import Engineer


class EngineerConfigError(ValueError):
    pass


def load_engineers(path: Path) -> list[Engineer]:
    if not path.exists():
        raise EngineerConfigError(f"工程师配置不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineerConfigError(f"无法读取工程师配置：{path}") from exc
    entries = payload.get("engineers") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise EngineerConfigError("工程师配置必须包含 engineers 数组")
    result: list[Engineer] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise EngineerConfigError(f"第 {index} 项工程师配置不是对象")
        required = ("name", "display_name")
        if any(not str(entry.get(key, "")).strip() for key in required):
            raise EngineerConfigError(f"第 {index} 项缺少必填字段")
        # user_id/dept_id 可暂为空：先用姓名展示，WO-04 只读查询时必须解析到唯一有效用户。
        user_id = str(entry.get("user_id") or "").strip()
        dept_id = str(entry.get("dept_id") or "").strip()
        result.append(
            Engineer(
                user_id=user_id,
                name=str(entry["name"]).strip(),
                dept_id=dept_id,
                display_name=str(entry["display_name"]).strip(),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return [engineer for engineer in result if engineer.enabled]
