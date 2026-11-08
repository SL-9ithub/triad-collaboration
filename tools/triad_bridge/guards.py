"""Hard guards: known schema profile, fixed account binding, bounded paths and sizes.

Every rejection here happens before a backup is created or a write transaction is
opened, so a failed guard can never leave a new automation row behind.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from . import config as config_module

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
_FINGERPRINT_COLUMNS = ("name", "type", "notnull", "dflt_value", "pk", "hidden")
_PROFILE_CACHE: dict[str, dict] | None = None


# --------------------------------------------------------------------------- profiles


def profile_files() -> list[Path]:
    return sorted(PROFILE_DIR.glob("*.sql"))


def table_columns(connection: sqlite3.Connection, table: str = "automations") -> list[list] | None:
    info = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return None
    return [[row[1], row[2], row[3], row[4], row[5], 0] for row in info]


def stored_ddl(connection: sqlite3.Connection, table: str = "automations") -> str | None:
    """The CREATE statement SQLite actually stored for the table, comments outside it excluded."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not row or not row[0]:
        return None
    return str(row[0])


def ddl_tokens(sql: str) -> list[str]:
    """Tokenise a CREATE statement so formatting stops mattering and meaning still does.

    Whitespace and `--` / `/* */` comments are dropped. Everything inside a quoted
    string literal is kept byte for byte, because a default like '[]' differs from
    ' []'. Quoted and bare identifiers are folded to lower case, which is how SQLite
    compares them.
    """
    tokens: list[str] = []
    index, length = 0, len(sql)
    while index < length:
        char = sql[index]
        if char in " \t\r\n\f\v":
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            closing = sql.find("*/", index + 2)
            index = length if closing < 0 else closing + 2
            continue
        if char == "'":
            cursor, literal = index + 1, []
            while cursor < length:
                if sql[cursor] == "'":
                    if cursor + 1 < length and sql[cursor + 1] == "'":
                        literal.append("''")
                        cursor += 2
                        continue
                    cursor += 1
                    break
                literal.append(sql[cursor])
                cursor += 1
            tokens.append("'" + "".join(literal) + "'")
            index = cursor
            continue
        if char in "\"`[":
            closer = "]" if char == "[" else char
            closing = sql.find(closer, index + 1)
            closing = length if closing < 0 else closing
            tokens.append("id:" + sql[index + 1:closing].lower())
            index = closing + 1
            continue
        if char.isalnum() or char in "_$":
            cursor = index
            while cursor < length and (sql[cursor].isalnum() or sql[cursor] in "_$"):
                cursor += 1
            tokens.append(sql[index:cursor].lower())
            index = cursor
            continue
        tokens.append(char)
        index += 1
    return tokens


def ddl_digest(sql: str) -> str:
    return hashlib.sha256("\x1f".join(ddl_tokens(sql)).encode("utf-8")).hexdigest()


def unique_index_names(connection: sqlite3.Connection, table: str = "automations") -> list[str]:
    """Explicitly declared unique indexes; the automatic PRIMARY KEY index is not one of them."""
    rows = connection.execute(f"PRAGMA index_list({table})").fetchall()
    return sorted(str(row[1]) for row in rows if row[2] and row[3] in ("c", "u"))


def fingerprint(columns: list[list]) -> str:
    payload = [[list(row)[i] for i in range(len(_FINGERPRINT_COLUMNS))] for row in columns]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def columns_from_ddl(ddl: str) -> list[list]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(ddl)
        columns = table_columns(connection)
        if columns is None:
            raise ValueError("profile_ddl_without_automations_table")
        return columns
    finally:
        connection.close()


def _profile_material(ddl: str) -> dict:
    """Execute the reviewed DDL once: column list, stored DDL digest, unique index baseline."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(ddl)
        columns = table_columns(connection)
        stored = stored_ddl(connection)
        if columns is None or stored is None:
            raise ValueError("profile_ddl_without_automations_table")
        return {"columns": columns, "fingerprint": fingerprint(columns),
                "ddl_sha256": ddl_digest(stored), "unique_indexes": unique_index_names(connection)}
    finally:
        connection.close()


def load_profiles() -> dict[str, dict]:
    global _PROFILE_CACHE
    if _PROFILE_CACHE is None:
        profiles: dict[str, dict] = {}
        for path in profile_files():
            material = _profile_material(path.read_text(encoding="utf-8"))
            material["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            profiles[path.stem] = material
        if not profiles:
            raise ValueError("no_schema_profiles_available")
        _PROFILE_CACHE = profiles
    return _PROFILE_CACHE


def describe_profiles() -> list[dict]:
    return [{"name": name, "fingerprint": item["fingerprint"], "columns": len(item["columns"]),
             "ddl_sha256": item["ddl_sha256"], "profile_sha256": item["sha256"]}
            for name, item in sorted(load_profiles().items())]


# --------------------------------------------------------------------------- schema


def inspect(connection: sqlite3.Connection) -> dict:
    """Read-only description of the live table, used by the guard and by init's probe."""
    columns = table_columns(connection)
    if columns is None:
        raise ValueError("unsupported_schema")
    if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='automations'").fetchone():
        raise ValueError("unexpected_automation_trigger")
    stored = stored_ddl(connection)
    if stored is None:
        raise ValueError("unsupported_schema")
    return {"columns": columns, "fingerprint": fingerprint(columns), "ddl_sha256": ddl_digest(stored),
            "unique_indexes": unique_index_names(connection), "trigger_present": False}


def match_profile(report: dict, expected: str | None = None) -> dict:
    """Return the reviewed profile that the live table matches on columns *and* full DDL."""
    profiles = load_profiles()
    if expected is not None:
        profile = profiles.get(expected)
        if profile is None:
            raise ValueError("unsupported_profile")
        candidates = {expected: profile}
    else:
        candidates = profiles
    for name, profile in sorted(candidates.items()):
        if report["ddl_sha256"] != profile["ddl_sha256"]:
            continue
        if report["fingerprint"] != profile["fingerprint"]:
            continue
        extra = set(report["unique_indexes"]) - set(profile["unique_indexes"])
        if extra:
            raise ValueError("unexpected_unique_index")
        return {"profile": name, "fingerprint": profile["fingerprint"],
                "ddl_sha256": profile["ddl_sha256"], "columns": len(profile["columns"])}
    if report["fingerprint"] in {item["fingerprint"] for item in candidates.values()}:
        raise ValueError("schema_ddl_mismatch")
    raise ValueError("unsupported_schema")


def schema_guard(connection: sqlite3.Connection, config: dict) -> dict:
    """Only a reviewed profile may be used; a manual pin cannot widen support."""
    declared = config["profile"]
    if declared["name"] not in load_profiles():
        raise ValueError("unsupported_profile")
    report = inspect(connection)
    matched = match_profile(report, declared["name"])
    profile = load_profiles()[declared["name"]]
    if declared["fingerprint"] != profile["fingerprint"]:
        raise ValueError("unsupported_schema")
    pin = config.get("schema_pin")
    if pin and pin != profile["ddl_sha256"]:
        raise ValueError("schema_pin_mismatch")
    return {"profile": matched["profile"], "fingerprint": matched["fingerprint"],
            "ddl_sha256": matched["ddl_sha256"], "unique_indexes": len(report["unique_indexes"])}


# --------------------------------------------------------------------------- account


def owner_digest(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def reference_owner(connection: sqlite3.Connection, config: dict) -> str:
    """Both reference tasks must be confirmed, undeleted, and hash to the bound owner."""
    references = tuple(config["owner_reference_task_ids"])
    rows = connection.execute(
        "SELECT id,owner_user_id,owner_status,deleted_at FROM automations WHERE id IN (?,?)",
        references).fetchall()
    if len(rows) != 2 or {str(row[0]) for row in rows} != set(references):
        raise ValueError("owner_reference_not_verified")
    owners = set()
    for row in rows:
        owner, status, deleted = row[1], row[2], row[3]
        if not owner or status != "confirmed" or deleted is not None:
            raise ValueError("owner_reference_not_verified")
        owners.add(owner)
    if len(owners) != 1:
        raise ValueError("owner_reference_conflict")
    owner = owners.pop()
    if owner_digest(owner) != config["owner_binding_sha256"]:
        raise ValueError("owner_binding_mismatch")
    return owner


# --------------------------------------------------------------------------- paths


def resolve_project(config: dict, raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("unexpected_project_boundary")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise ValueError("unexpected_project_boundary") from None
    root = config["cooperation_root"]
    if resolved == root or resolved.parent != root:
        raise ValueError("unexpected_project_boundary")
    if not resolved.is_dir():
        raise ValueError("unexpected_project_boundary")
    source = config["public_source_root"]
    if resolved == source or config_module.is_within(resolved, source):
        raise ValueError("unexpected_project_boundary")
    return resolved


def resolve_task(project: Path, raw: str, limits: dict) -> tuple[Path, bytes, str]:
    candidate = Path(raw)
    path = candidate if candidate.is_absolute() else project / candidate
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise ValueError("task_not_found") from None
    if resolved.parent != project or resolved.suffix.lower() != ".md":
        raise ValueError("task_must_be_project_markdown")
    size = resolved.stat().st_size
    if size > limits["max_task_bytes"]:
        raise ValueError("task_markdown_too_large")
    raw_bytes = resolved.read_bytes()
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("task_not_utf8") from None
    return resolved, raw_bytes, text


def resolve_report(project: Path, raw: str, limits: dict, *, must_exist: bool = False) -> Path:
    name = Path(str(raw))
    if name.name != str(raw) or name.is_absolute():
        raise ValueError("report_must_be_direct_in_project")
    if name.suffix.lower() != ".md":
        raise ValueError("report_must_be_markdown")
    resolved = (project / name).resolve()
    if resolved.parent != project:
        raise ValueError("report_must_be_direct_in_project")
    if must_exist:
        if not resolved.is_file():
            raise ValueError("report_not_found")
        if resolved.stat().st_size > limits["max_report_bytes"]:
            raise ValueError("report_too_large")
    elif resolved.exists() and resolved.stat().st_size > limits["max_report_bytes"]:
        raise ValueError("report_too_large")
    return resolved


# --------------------------------------------------------------------------- task spec


def validate_spec(spec: dict, config: dict) -> dict:
    if not config_module.ID_RE.fullmatch(spec["request_id"]):
        raise ValueError("invalid_request_id")
    try:
        uuid.UUID(spec["task_id"])
    except (ValueError, TypeError):
        raise ValueError("invalid_task_id") from None
    limits = config["limits"]
    name = spec["name"]
    if not 1 <= len(name) <= limits["max_name_chars"] or any(ord(ch) < 32 for ch in name):
        raise ValueError("invalid_task_name")
    nonce = spec["nonce"]
    if not 1 <= len(nonce) <= limits["max_nonce_chars"] or any(ord(ch) < 32 for ch in nonce):
        raise ValueError("invalid_nonce")
    if len(spec["prompt"].encode("utf-8")) > limits["max_prompt_bytes"]:
        raise ValueError("final_prompt_too_large")
    if spec["difficulty"] not in ("simple", "hard"):
        raise ValueError("invalid_difficulty")
    model = spec["model"]
    if model not in config["model_allowlist"]:
        raise ValueError("unsupported_model_selection")
    if config["models"][spec["difficulty"]] != model:
        raise ValueError("model_not_matching_difficulty_mapping")
    return spec
