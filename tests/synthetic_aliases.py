"""Generated synthetic regression configuration; never a site mapping."""
import json
import tempfile
from pathlib import Path
from src.procedure_alias import (SYNTHETIC_GROUP_ITEM_CODES, SYNTHETIC_GROUP_SOURCE_NAME,
                                SYNTHETIC_GROUP_TARGET_NAME, load_procedure_aliases)

def synthetic_alias_payload():
    return {"aliases": [dict(enabled=True, item_code=code,
             source_name=SYNTHETIC_GROUP_SOURCE_NAME, normalized_name=SYNTHETIC_GROUP_SOURCE_NAME,
             target_procedure_name=SYNTHETIC_GROUP_TARGET_NAME, strict_target_match=True,
             rule_id=f"full_confirmed_50299821_{code}", note="SYNTHETIC", version="demo", effective_date="")
             for code in sorted(SYNTHETIC_GROUP_ITEM_CODES)]}

def synthetic_alias_resolver():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic_aliases.json"
        path.write_text(json.dumps(synthetic_alias_payload()), encoding="utf-8")
        return load_procedure_aliases(path)
