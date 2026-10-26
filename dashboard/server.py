"""Approvals dashboard + audit-log viewer (build plan §1.2 and §1.3).

A local-only web surface served with the standard library's
``http.server`` on 127.0.0.1 (default port 8765):

- **Pending approvals** — one card per proposal awaiting a human: action,
  risk class, the planner's justification, parameters, and one-click
  Approve / Refuse buttons wired to the approvals engine.
- **Decision history** — every proposal and how it was decided.
- **Audit log** — the hash-chained journal, entry by entry, with a
  "Verify chain" action that recomputes the whole chain and either
  confirms it or names the exact entry where it breaks.

Run the seeded demo (SYN-2026-001, synthetic data only)::

    python3 -m dashboard.server [--port 8765]

The demo run executes in a background thread and pauses on its two
sensitive steps; deciding them here resumes it live.

Standard library only. Binds 127.0.0.1 exclusively.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from approvals.engine import ApprovalQueue, ProposalExpired, Status
from auditlog.chain import AuditLog, verify_chain

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

DOSSIER_DIR = (
    Path(__file__).resolve().parents[3]
    / "files" / "dossier-synthetique-SYN-2026-001"
)
DOSSIER_FILES = [
    "01-courriel-attribution.md",
    "02-offre-bail-fictive.md",
    "03-liste-exigences.md",
    "04-inventaire-pieces.md",
]
DEMO_TASK = (
    "Prépare le dossier SYN-2026-001 : échéances, liste de documents, "
    "résumé du cas, paquet d'impression."
)

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, "Segoe UI", Roboto, Helvetica,
       Arial, sans-serif; background: #f4f5f7; color: #1c1e21; }
header { background: #101828; color: #fff; padding: 22px 32px; }
header h1 { margin: 0; font-size: 24px; letter-spacing: .2px; }
header .tagline { color: #9db2ce; margin-top: 4px; font-size: 14px; }
header .meta { color: #d0d7e3; margin-top: 10px; font-size: 13px; }
main { max-width: 980px; margin: 0 auto; padding: 24px 20px 48px; }
section { margin-bottom: 34px; }
h2 { font-size: 17px; margin: 0 0 6px; }
.sub { color: #5b6470; font-size: 13px; margin: 0 0 14px; }
.card { background: #fff; border: 1px solid #d9dee5; border-left: 5px solid #e8a33d;
        border-radius: 8px; padding: 16px 18px; margin-bottom: 12px;
        box-shadow: 0 1px 2px rgba(16,24,40,.05); }
.card .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.card .action { font-weight: 700; font-size: 15px; font-family: ui-monospace,
        SFMono-Regular, Menlo, monospace; }
.badge { font-size: 11px; font-weight: 700; text-transform: uppercase;
         letter-spacing: .5px; padding: 3px 8px; border-radius: 999px; }
.badge.write, .badge.execute, .badge.send, .badge.delete, .badge.print {
        background: #fdf0d5; color: #8a5a00; border: 1px solid #e8c87a; }
.badge.read { background: #e2f2e4; color: #1d6b2a; border: 1px solid #9fd0a6; }
.badge.pending { background: #eef1f5; color: #43536a; border: 1px solid #c3cedd; }
.badge.approved, .badge.auto_approved { background: #e2f2e4; color: #1d6b2a;
        border: 1px solid #9fd0a6; }
.badge.denied, .badge.expired { background: #fbe3e3; color: #8f1d1d;
        border: 1px solid #eda3a3; }
.justification { color: #333a44; font-size: 14px; margin: 10px 0 6px; }
.params { font-family: ui-monospace, Menlo, monospace; font-size: 12px;
          color: #43536a; background: #f6f8fa; border: 1px solid #e3e8ee;
          border-radius: 6px; padding: 8px 10px; white-space: pre-wrap;
          word-break: break-word; }
.expiry { color: #8a5a00; font-size: 12.5px; margin-top: 8px; }
.decisions { margin-top: 12px; display: flex; gap: 10px; }
button { font-size: 14px; font-weight: 600; padding: 9px 18px; border-radius: 7px;
         border: 1px solid transparent; cursor: pointer; }
button.approve { background: #1d7a34; color: #fff; }
button.approve:hover { background: #17652c; }
button.refuse { background: #fff; color: #b3261e; border-color: #d8a19d; }
button.refuse:hover { background: #fdf0ef; }
button.verify { background: #101828; color: #fff; }
.empty { background: #fff; border: 1px dashed #c3cedd; border-radius: 8px;
         padding: 18px; color: #5b6470; font-size: 14px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border: 1px solid #d9dee5; border-radius: 8px; overflow: hidden;
        font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e8ecf1;
         vertical-align: top; }
th { background: #f0f2f5; color: #43536a; font-size: 11.5px;
     text-transform: uppercase; letter-spacing: .4px; }
td.mono, .mono { font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
tr.broken { background: #fdecec; }
.banner { border-radius: 8px; padding: 13px 16px; font-size: 14px;
          margin-bottom: 14px; font-weight: 600; }
.banner.ok { background: #e2f2e4; color: #14541f; border: 1px solid #9fd0a6; }
.banner.broken { background: #fbe3e3; color: #7c1414; border: 1px solid #eda3a3; }
.flash { border-radius: 8px; padding: 11px 16px; font-size: 14px;
         margin-bottom: 16px; background: #eaf0fb; color: #1d3f7c;
         border: 1px solid #b9cbf0; }
footer { color: #7a8494; font-size: 12.5px; text-align: center; padding: 18px; }
"""


@dataclass
class DashboardState:
    """Everything the dashboard renders from. Shared with the executor."""

    queue: ApprovalQueue
    audit_log_path: Path
    run_label: str = "SYN-2026-001 — synthetic demo run"
    lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _proposal_card(proposal, now: float) -> str:
    remaining = max(0, int(proposal.expires_at - now))
    params = html.escape(json.dumps(proposal.params, ensure_ascii=False, indent=1))
    return f"""
    <div class="card">
      <div class="row">
        <span class="action">{html.escape(proposal.action)}</span>
        <span class="badge {html.escape(proposal.action_class.value)}">
          {html.escape(proposal.action_class.value)} — approval required</span>
        <span class="badge pending">awaiting your decision</span>
      </div>
      <p class="justification">{html.escape(proposal.justification)}</p>
      <div class="params">{params}</div>
      <p class="expiry">Proposed at {_fmt_time(proposal.created_at)} ·
        expires in {remaining // 60}:{remaining % 60:02d} — on expiry the
        action is refused by default.</p>
      <form class="decisions" method="post" action="/decide">
        <input type="hidden" name="proposal_id" value="{proposal.proposal_id}">
        <button class="approve" type="submit" name="decision" value="approve">
          ✓ Approve</button>
        <button class="refuse" type="submit" name="decision" value="deny">
          ✕ Refuse</button>
      </form>
    </div>"""


def _history_rows(proposals) -> str:
    rows = []
    for p in reversed(proposals):
        decided = (
            f"{html.escape(p.decided_by or '—')}"
            + (f" · {_fmt_time(p.decided_at)}" if p.decided_at else "")
        )
        rows.append(
            f"<tr><td class='mono'>{html.escape(p.action)}</td>"
            f"<td><span class='badge {html.escape(p.action_class.value)}'>"
            f"{html.escape(p.action_class.value)}</span></td>"
            f"<td><span class='badge {p.status.value}'>{p.status.value.replace('_', ' ')}</span></td>"
            f"<td>{decided}</td></tr>"
        )
    return "\n".join(rows)


def _audit_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"seq": "?", "action": "(unreadable entry)"})
    return entries


def _audit_rows(entries: list[dict], break_index: int | None) -> str:
    rows = []
    for index, entry in enumerate(entries):
        cls = " class='broken'" if break_index == index else ""
        decision = entry.get("decision") or {}
        decision_text = (
            f"{decision.get('status', '')} by {decision.get('by', '')}".strip()
            if decision else "—"
        )
        rows.append(
            f"<tr{cls}><td class='mono'>{entry.get('seq', '?')}</td>"
            f"<td class='mono'>{html.escape(str(entry.get('ts', ''))[11:19])}</td>"
            f"<td>{html.escape(str(entry.get('actor', '')))}</td>"
            f"<td class='mono'>{html.escape(str(entry.get('action', '')))}</td>"
            f"<td>{html.escape(decision_text)}</td>"
            f"<td>{html.escape(str(entry.get('result') or '—'))}</td>"
            f"<td class='mono'>{html.escape(str(entry.get('hash', ''))[:12])}…</td></tr>"
        )
    return "\n".join(rows)


def render_dashboard(state: DashboardState, show_verify: bool = False,
                     flash: str = "") -> str:
    now = time.time()
    with state.lock:
        proposals = state.queue.all_proposals()
    pending = [p for p in proposals if p.status is Status.PENDING]
    entries = _audit_entries(state.audit_log_path)

    verify_banner = ""
    break_index = None
    if show_verify:
        result = verify_chain(state.audit_log_path)
        if result.ok:
            verify_banner = (
                f"<div class='banner ok'>✓ Chain verified — {result.entries_checked} "
                "entries, every hash and link intact. Nothing was altered "
                "in silence.</div>"
            )
        else:
            break_index = result.break_index
            verify_banner = (
                f"<div class='banner broken'>✗ Chain BROKEN at entry "
                f"#{result.break_index} (seq={result.break_seq}) — "
                f"{html.escape(result.reason or '')}. The tampered entry is "
                "highlighted below.</div>"
            )

    pending_html = (
        "\n".join(_proposal_card(p, now) for p in pending)
        if pending else
        "<div class='empty'>No action is waiting for you. When the agent "
        "proposes a sensitive step, it will appear here and the run will "
        "pause until you decide.</div>"
    )
    history_html = (
        "<table><tr><th>Action</th><th>Class</th><th>Status</th>"
        "<th>Decided by</th></tr>" + _history_rows(proposals) + "</table>"
        if proposals else
        "<div class='empty'>No proposals yet.</div>"
    )
    audit_html = (
        "<table><tr><th>#</th><th>Time (UTC)</th><th>Actor</th><th>Event</th>"
        "<th>Decision</th><th>Result</th><th>Hash</th></tr>"
        + _audit_rows(entries, break_index) + "</table>"
        if entries else
        "<div class='empty'>The audit log is empty.</div>"
    )
    flash_html = f"<div class='flash'>{html.escape(flash)}</div>" if flash else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Governed Agent Bridge — Approvals</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Governed Agent Bridge</h1>
  <div class="tagline">Your agent. Your PC. Your rules.</div>
  <div class="meta">Run: {html.escape(state.run_label)} · The agent proposes,
  you decide — nothing sensitive executes without your traced “yes”, and
  everything is written to a tamper-evident audit log.</div>
</header>
<main>
  {flash_html}
  <section>
    <h2>Pending approvals ({len(pending)})</h2>
    <p class="sub">The run is paused on each of these. Approving executes the
    step; refusing — or letting it expire — skips it, and either way the
    decision is recorded in the audit log.</p>
    {pending_html}
  </section>
  <section>
    <h2>Decision history</h2>
    <p class="sub">Every proposal of this run, including read-class steps
    auto-approved by policy.</p>
    {history_html}
  </section>
  <section>
    <h2>Audit log — tamper-evident</h2>
    <p class="sub">Append-only journal; each entry carries the SHA-256 of
    the previous one. Edit a single byte anywhere and verification names
    the exact entry where the chain breaks.</p>
    {verify_banner}
    <form method="get" action="/">
      <button class="verify" type="submit" name="verify" value="1">
        Verify the chain</button>
    </form>
    <div style="height:12px"></div>
    {audit_html}
  </section>
</main>
<footer>Governed Agent Bridge — demo surface. All data on this page is
synthetic (fictional persona “Mme A. Tremblay”, dossier SYN-2026-001).
Local only: served on 127.0.0.1.</footer>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def make_handler(state: DashboardState):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "GovernedAgentBridge/0.1"

        def log_message(self, *args):  # keep the console quiet
            pass

        def _send_html(self, body: str, status: int = 200) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            show_verify = query.get("verify", ["0"])[0] == "1"
            flash = query.get("flash", [""])[0]
            self._send_html(render_dashboard(state, show_verify, flash))

        def do_POST(self):
            if urlparse(self.path).path != "/decide":
                self._send_html("Not found", status=404)
                return
            length = int(self.headers.get("Content-Length", 0))
            form = parse_qs(self.rfile.read(length).decode("utf-8"))
            proposal_id = form.get("proposal_id", [""])[0]
            decision = form.get("decision", [""])[0]
            flash = ""
            try:
                with state.lock:
                    if decision == "approve":
                        proposal = state.queue.approve(
                            proposal_id, decided_by="you (dashboard)")
                        flash = f"Approved: {proposal.action} — the run resumes."
                    elif decision == "deny":
                        proposal = state.queue.deny(
                            proposal_id, decided_by="you (dashboard)")
                        flash = f"Refused: {proposal.action} — the step will be skipped."
                    else:
                        flash = "Unknown decision."
            except ProposalExpired:
                flash = "That proposal had already expired — refused by default."
            except (KeyError, ValueError) as exc:
                flash = f"Could not record the decision: {exc}"
            self.send_response(303)
            self.send_header("Location", "/?flash=" + quote(flash))
            self.end_headers()

    return DashboardHandler


def make_server(state: DashboardState, host: str = DEFAULT_HOST,
                port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state))


# ---------------------------------------------------------------------------
# Seeded demo
# ---------------------------------------------------------------------------

def build_demo_state(base_dir: Path | None = None) -> tuple[DashboardState, threading.Thread]:
    """Create the SYN-2026-001 sandbox and start the governed run.

    The run pauses on its two sensitive steps; decisions made in the
    dashboard resume it. Returns the state and the run thread.
    """
    from executor.executor import Executor
    from planner.planner import planner_from_env

    base = base_dir or Path(tempfile.mkdtemp(prefix="gab-dashboard-demo-"))
    sandbox_root = base / "sandbox"
    sandbox_root.mkdir(parents=True, exist_ok=True)
    for name in DOSSIER_FILES:
        shutil.copyfile(DOSSIER_DIR / name, sandbox_root / name)

    audit_path = base / "audit-log.jsonl"
    audit_log = AuditLog(audit_path)
    queue = ApprovalQueue(journal_path=base / "approvals-journal.jsonl")
    state = DashboardState(queue=queue, audit_log_path=audit_path)
    executor = Executor(
        sandbox_root, audit_log, queue=queue, ttl_seconds=900.0
    )
    planner = planner_from_env(audit_log=audit_log)
    plan = planner.plan(DEMO_TASK, {"dossier_id": "SYN-2026-001"})
    thread = threading.Thread(target=executor.run, args=(plan,), daemon=True,
                              name="gab-demo-run")
    thread.start()
    return state, thread


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Governed Agent Bridge — approvals dashboard (local only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST)
    args = parser.parse_args()

    state, _thread = build_demo_state()
    server = make_server(state, host=args.host, port=args.port)
    print(f"Governed Agent Bridge dashboard: http://{args.host}:{args.port}/")
    print("Demo run SYN-2026-001 is executing; it will pause on its two "
          "sensitive steps. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
