"""Approvals dashboard + audit-log viewer (plan §1.2, §1.3).

Local-only web surface on 127.0.0.1 (default port 8765), standard library
only: pending proposals with Approve / Refuse buttons wired to the
approvals engine, decision history, and the hash-chained audit log with a
one-click "Verify chain" action.
"""

from .server import DEFAULT_HOST, DEFAULT_PORT, DashboardState, make_server

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "DashboardState", "make_server"]
