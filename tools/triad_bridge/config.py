"""Private configuration: loading, strict path boundaries, read-only database access.

Every value that could steer a write lives here instead of in code constants.
Nothing in this module writes the database; it only opens read-only handles.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import quote

ID_RE = re.compile(r"^[a-z0-9-]{1,80}$")
NAMESPACE_RE = re.compile(r"^[a-z0-9/._-]{1,64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{2,60}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._-]{2,80}$")

DEFAULT_LIMITS = {
    "max_task_bytes": 28000,
    "max_prompt_bytes": 32768,
    "max_report_bytes": 1048576,
    "max_name_chars": 120,
    "max_nonce_chars": 200,
}

REQUIRED_KEYS = (
    "version",
    "cooperation_root",
    "database_path",
    "backup_dir",
    "namespace_tag",
    "profile",
    "owner_reference_task_ids",
    "owner_binding_sha256",
    "model_allowlist",
    "models",
)


def repo_root() -> Path:
    """The public source tree this package ships in (…/tools/triad_bridge -> repo)."""
    return Path(__file__).resolve().parents[2]


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _absolute(raw: str, field: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"config_relative_path:{field}")
    if path.anchor and path.parent == path:
        raise ValueError(f"config_path_is_drive_root:{field}")
    return path


def _resolved(raw: str, field: str, *, must_exist: bool) -> Path:
    path = _absolute(raw, field)
    try:
        return path.resolve(strict=must_exist)
    except OSError:
        raise ValueError(f"config_path_missing:{field}") from None


def sqlite_uri(path: Path, mode: str) -> str:
    return "file:" + quote(path.as_posix(), safe="/:") + "?mode=" + mode


def readonly(path: Path) -> sqlite3.Connection:
    """Read-only handle. The path must already exist: `mode=ro` never creates a file."""
    connection = sqlite3.connect(sqlite_uri(path, "ro"), uri=True, timeout=5)
    connection.execute("PRAGMA query_only=1")
    return connection


def load(path: Path, *, require_existing: bool = True) -> dict:
    path = Path(path)
    if require_existing and not path.is_file():
        raise ValueError("config_not_found")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError("config_not_an_object")
    return validate(raw, config_path=path)


def validate(raw: dict, *, config_path: Path | None = None) -> dict:
    missing = [key for key in REQUIRED_KEYS if key not in raw]
    if missing:
        raise ValueError("config_missing_fields:" + ",".join(missing))
    if raw["version"] != 1:
        raise ValueError("config_unsupported_version")

    config_path = Path(config_path).resolve() if config_path else None
    declared_source = _resolved(raw.get("public_source_root") or str(repo_root()),
                                "public_source_root", must_exist=True)
    actual_source = repo_root().resolve()
    if declared_source != actual_source:
        raise ValueError("config_source_root_mismatch")
    source_root = actual_source
    root = _resolved(raw["cooperation_root"], "cooperation_root", must_exist=True)
    database = _resolved(raw["database_path"], "database_path", must_exist=True)
    backup_dir = _resolved(raw["backup_dir"], "backup_dir", must_exist=False)

    if not root.is_dir():
        raise ValueError("config_cooperation_root_not_a_directory")
    if is_within(root, source_root) or root == source_root:
        raise ValueError("config_cooperation_root_inside_source")
    if not database.is_file():
        raise ValueError("config_database_not_a_file")
    if is_within(database, root) or database == root:
        raise ValueError("config_database_inside_cooperation_root")
    if is_within(database, source_root) or is_within(backup_dir, root) or is_within(backup_dir, source_root):
        raise ValueError("config_private_path_inside_public_or_projects")
    if backup_dir == database.parent or is_within(database, backup_dir):
        raise ValueError("config_backup_dir_overlaps_database")
    if config_path is not None:
        if is_within(config_path, source_root):
            raise ValueError("config_file_inside_public_source")
        if is_within(config_path, root) or config_path.parent == root:
            raise ValueError("config_file_inside_cooperation_root")

    tag = str(raw["namespace_tag"])
    if not NAMESPACE_RE.fullmatch(tag):
        raise ValueError("config_invalid_namespace_tag")

    profile = raw["profile"]
    if not isinstance(profile, dict):
        raise ValueError("config_invalid_profile")
    name = str(profile.get("name", ""))
    fingerprint = str(profile.get("fingerprint", ""))
    if not PROFILE_NAME_RE.fullmatch(name):
        raise ValueError("config_invalid_profile_name")
    if not DIGEST_RE.fullmatch(fingerprint):
        raise ValueError("config_invalid_profile_fingerprint")

    pin = raw.get("schema_pin")
    if pin is not None and not DIGEST_RE.fullmatch(str(pin)):
        raise ValueError("config_invalid_schema_pin")

    references = raw["owner_reference_task_ids"]
    if not isinstance(references, list) or len(references) != 2:
        raise ValueError("config_requires_two_reference_tasks")
    normalised = []
    for value in references:
        try:
            normalised.append(str(uuid.UUID(str(value))))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("config_invalid_reference_task_id") from None
    if len(set(normalised)) != 2:
        raise ValueError("config_reference_tasks_not_distinct")

    binding = str(raw["owner_binding_sha256"])
    if not DIGEST_RE.fullmatch(binding):
        raise ValueError("config_invalid_owner_binding")

    allowlist = raw["model_allowlist"]
    if not isinstance(allowlist, list) or not allowlist:
        raise ValueError("config_model_allowlist_empty")
    allowlist = [str(item) for item in allowlist]
    if any(not MODEL_RE.fullmatch(item) for item in allowlist):
        raise ValueError("config_invalid_model_id")
    if len(set(allowlist)) != len(allowlist):
        raise ValueError("config_duplicate_model_id")

    models = raw["models"]
    if not isinstance(models, dict) or "simple" not in models or "hard" not in models:
        raise ValueError("config_models_incomplete")
    for difficulty in ("simple", "hard"):
        if str(models[difficulty]) not in allowlist:
            raise ValueError("config_model_not_in_allowlist:" + difficulty)

    limits = dict(DEFAULT_LIMITS)
    supplied = raw.get("limits") or {}
    if not isinstance(supplied, dict):
        raise ValueError("config_invalid_limits")
    for key, value in supplied.items():
        if key not in DEFAULT_LIMITS or not isinstance(value, int) or value < 1:
            raise ValueError("config_invalid_limit:" + str(key))
        limits[key] = value
    if limits["max_task_bytes"] > DEFAULT_LIMITS["max_task_bytes"]:
        raise ValueError("config_task_limit_above_upstream")
    if limits["max_prompt_bytes"] > DEFAULT_LIMITS["max_prompt_bytes"]:
        raise ValueError("config_prompt_limit_above_upstream")

    template = raw.get("prompt_template")
    if template is not None and not isinstance(template, str):
        raise ValueError("config_invalid_prompt_template")

    return {
        "version": 1,
        "cooperation_root": root,
        "database_path": database,
        "backup_dir": backup_dir,
        "public_source_root": source_root,
        "namespace_tag": tag,
        "profile": {"name": name, "fingerprint": fingerprint},
        "schema_pin": pin,
        "owner_reference_task_ids": tuple(normalised),
        "owner_binding_sha256": binding,
        "model_allowlist": allowlist,
        "models": {"simple": str(models["simple"]), "hard": str(models["hard"])},
        "limits": limits,
        "prompt_template": template,
        "config_path": config_path,
    }


def serialise(config: dict) -> dict:
    """Round-trip form used by `init --write` (paths as strings, no runtime extras)."""
    return {
        "version": 1,
        "cooperation_root": str(config["cooperation_root"]),
        "database_path": str(config["database_path"]),
        "backup_dir": str(config["backup_dir"]),
        "public_source_root": str(config["public_source_root"]),
        "namespace_tag": config["namespace_tag"],
        "profile": dict(config["profile"]),
        "schema_pin": config.get("schema_pin"),
        "owner_reference_task_ids": list(config["owner_reference_task_ids"]),
        "owner_binding_sha256": config["owner_binding_sha256"],
        "model_allowlist": list(config["model_allowlist"]),
        "models": dict(config["models"]),
        "limits": dict(config["limits"]),
        "prompt_template": config.get("prompt_template"),
    }


def write_once(path: Path, value: dict) -> str:
    """Create a file atomically; identical existing content is idempotent, different content is refused."""
    path = Path(path)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8-sig"))
        if existing != value:
            raise ValueError("config_exists_with_different_content")
        return "already_exists"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        try:
            temporary.rename(path)
        except FileExistsError:
            if json.loads(path.read_text(encoding="utf-8-sig")) != value:
                raise ValueError("config_exists_with_different_content") from None
            return "already_exists"
    finally:
        temporary.unlink(missing_ok=True)
    return "created"
