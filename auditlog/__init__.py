"""Append-only, hash-chained audit log (plan §1.3)."""

from .chain import AuditLog, VerifyResult, verify_chain

__all__ = ["AuditLog", "VerifyResult", "verify_chain"]
