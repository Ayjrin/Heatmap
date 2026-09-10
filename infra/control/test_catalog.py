"""Keep Athena's physical schema aligned with the curated Parquet writer."""
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "src"))
from proleague.curated import FACT_COLUMNS
from proleague.dataset import DIM_MATCH_COLUMNS, DIM_PARTICIPANT_COLUMNS


def test_glue_schema_matches_written_parquet():
    types = {"VARCHAR": "string", "BIGINT": "bigint", "BOOLEAN": "boolean"}
    expected = {
        name: [{"name": field, "type": types[kind]} for field, kind in columns]
        for name, columns in {"fact_kill": FACT_COLUMNS, "dim_match": DIM_MATCH_COLUMNS,
                              "dim_participant": DIM_PARTICIPANT_COLUMNS}.items()
    }
    assert json.loads((ROOT / "catalog-schema.json").read_text()) == expected


def test_static_site_upload_includes_modules_and_excludes_local_data(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("aws_deploy", ROOT.parent / "scripts/aws_deploy.py")
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    web = tmp_path / "web"
    (web / "data").mkdir(parents=True)
    for name in ("engine.mjs", "index.html", "data/current.json", ".env", "private.pem"):
        (web / name).write_text("isolated upload test")
    (web / "linked.json").symlink_to(web / ".env")
    assets = {key: mime for _, key, mime in deploy.site_files()}
    assert assets == {"engine.mjs": "text/javascript", "index.html": "text/html"}
