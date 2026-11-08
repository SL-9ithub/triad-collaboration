"""Online-consistent backup with a read-back ACL check (pywin32 only, no downgrade).

The previous verified mechanism is reused as-is: build a DACL granting only the
current process user and SYSTEM, apply it as a protected DACL, then read the DACL
back. If pywin32 is missing or the read-back cannot prove the ACL, submit refuses.
Never touch the permissions of the application's own directories or credentials.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


def _win32():
    try:
        import ntsecuritycon
        import win32api
        import win32con
        import win32security
    except ImportError:
        raise ValueError("backup_acl_unavailable") from None
    return ntsecuritycon, win32api, win32con, win32security


def current_user_sid() -> str:
    _, win32api, win32con, win32security = _win32()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    return win32security.ConvertSidToStringSid(sid)


def system_sid() -> str:
    _, _, _, win32security = _win32()
    sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    return win32security.ConvertSidToStringSid(sid)


def restrict_new_directory(directory: Path) -> None:
    """Protect a freshly created private directory: current user + SYSTEM, inheritable."""
    ntsecuritycon, win32api, win32con, win32security = _win32()
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
    acl = win32security.ACL()
    for principal in (user, system):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
                                  win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
                                  ntsecuritycon.FILE_ALL_ACCESS, principal)
    win32security.SetNamedSecurityInfo(str(directory), win32security.SE_FILE_OBJECT,
                                       win32security.DACL_SECURITY_INFORMATION
                                       | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                                       None, None, acl, None)


def verify_directory_acl(directory: Path) -> dict:
    """Read the DACL back from disk; only an allow-ACL for this user + SYSTEM passes."""
    _, _, _, win32security = _win32()
    descriptor = win32security.GetNamedSecurityInfo(
        str(directory), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    dacl = descriptor.GetSecurityDescriptorDacl()
    control, _revision = descriptor.GetSecurityDescriptorControl()
    if dacl is None:
        raise ValueError("backup_acl_unverified")
    if not control & win32security.SE_DACL_PROTECTED:
        raise ValueError("backup_acl_not_protected")
    allowed = {current_user_sid(), system_sid()}
    seen: set[str] = set()
    for index in range(dacl.GetAceCount()):
        ace_type, _flags = dacl.GetAce(index)[0]
        sid = win32security.ConvertSidToStringSid(dacl.GetAce(index)[2])
        if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise ValueError("backup_acl_unverified")
        seen.add(sid)
    if seen - allowed:
        raise ValueError("backup_acl_foreign_principal")
    if current_user_sid() not in seen:
        raise ValueError("backup_acl_unverified")
    return {
        "protected": True,
        "principal_count": len(seen),
        "only_current_user_and_system": True,
        "system_principal_present": system_sid() in seen,
        "mechanism": "pywin32-token-sid+readback",
    }


def quick_check(path: Path) -> tuple[str, int]:
    """Integrity + row count of a completed backup file. Patched by tests to simulate failure."""
    with closing(sqlite3.connect(path)) as check:
        return (check.execute("PRAGMA quick_check").fetchone()[0],
                check.execute("SELECT COUNT(*) FROM automations").fetchone()[0])


def create(config: dict, source: sqlite3.Connection) -> dict:
    """sqlite3 online backup into a new private directory; verify before returning."""
    directory = Path(config["backup_dir"]) / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    restrict_new_directory(directory)
    acl_before = verify_directory_acl(directory)
    target = directory / "before.sqlite"
    with closing(sqlite3.connect(target)) as destination:
        source.backup(destination)
    result, rows = quick_check(target)
    if result != "ok":
        raise ValueError("backup_integrity_failed")
    acl_after = verify_directory_acl(directory)
    return {
        "path": str(target),
        "directory": str(directory),
        "quick_check": "ok",
        "automations_rows_at_backup": rows,
        "acl_before": acl_before,
        "acl": acl_after,
    }
