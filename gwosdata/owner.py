"""Protected owner connection for explicitly authorized operations."""

from pathlib import Path

from .errors import reject
from .postgres import local_setting, validate_reader_settings


def run(operation):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    root = Path(__file__).resolve().parents[1]
    try:
        settings = {name: conninfo_to_dict(local_setting(key, path)) for name, key, path in (
            ("owner", "GROWTHOS_OWNER_DSN", root / ".env"),
            ("writer", "GROWTHOS_WRITER_DSN", root / ".env"),
            ("reader", "GROWTHOS_READER_DSN", root / ".env"))}
    except Exception:
        reject("CONFIGURATION", "Owner operation requires protected local owner/writer/reader settings; details withheld.")
    for cfg in settings.values():
        validate_reader_settings(cfg)
        if any(cfg.get(key) != settings["owner"].get(key) for key in ("host", "port", "dbname")):
            reject("CONFIGURATION", "All owner-operation settings must identify the same database endpoint.")
    if len({cfg.get("user") for cfg in settings.values()}) != 3:
        reject("CREDENTIAL_BOUNDARY", "Owner operation requires separate owner, writer and reader logins.")
    with psycopg.connect(**settings["owner"], autocommit=True, connect_timeout=10,
                         options="-c statement_timeout=60000 -c lock_timeout=5000") as conn:
        return operation(conn, settings["writer"]["user"], settings["reader"]["user"])
