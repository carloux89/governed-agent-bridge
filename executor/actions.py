"""Deterministic action handlers for plan steps.

Each handler receives the step's ``params`` and the :class:`Sandbox`, and
returns a JSON-serializable result dict. Handlers only touch the sandbox
(every path goes through the guard) and use no network, no randomness and
no clock beyond what results report. Plan B of the build plan (§1.5):
sensitive content steps are deterministic templates over synthetic data.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from .sandbox import Sandbox

_MONTHS = (
    "janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|"
    "novembre|décembre"
)
_DATE_RE = re.compile(rf"\d{{1,2}} (?:{_MONTHS}) \d{{4}}")
_BOLD_DATE_RE = re.compile(rf"\*\*(\d{{1,2}} (?:{_MONTHS}) \d{{4}})\*\*")
_MONTH_NUM = {
    name: i + 1
    for i, name in enumerate(
        ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
         "août", "septembre", "octobre", "novembre", "décembre"]
    )
}


def _date_key(date_text: str) -> tuple[int, int, int]:
    day, month, year = date_text.split()
    return (int(year), _MONTH_NUM[month], int(day))


def _snippet(line: str) -> str:
    text = re.sub(r"\*\*", "", line).strip().lstrip("- ").strip()
    return text[:140]


# ---------------------------------------------------------------------------
# Read / internal generation steps
# ---------------------------------------------------------------------------

def read_document(params: dict, sandbox: Sandbox) -> dict:
    name = params["document"]
    text = sandbox.read_text(name)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "document": name,
        "chars": len(text),
        "lines": text.count("\n") + 1,
        "sha256_16": digest[:16],
    }


def inspect_workspace(params: dict, sandbox: Sandbox) -> dict:
    files = sandbox.list_files()
    return {"files": files, "count": len(files)}


def extract_deadlines(params: dict, sandbox: Sandbox) -> dict:
    """Collect deadlines from the dossier into ``echeances.md``.

    Deterministic rule: bolded dates in the allocation email, plus the
    dates on the lease-offer duration line (lease start/end). This yields
    exactly the four fictional deadlines of the reference sheet.
    """
    sources = ["01-courriel-attribution.md", "02-offre-bail-fictive.md"]
    found: dict[str, dict] = {}
    for source in sources:
        for line in sandbox.read_text(source).splitlines():
            dates = _BOLD_DATE_RE.findall(line)
            if source.endswith("02-offre-bail-fictive.md") and "Durée" in line:
                dates += _DATE_RE.findall(line)
            for date_text in dates:
                found.setdefault(
                    date_text, {"date": date_text, "source": source,
                                "context": _snippet(line)}
                )
    ordered = sorted(found.values(), key=lambda d: _date_key(d["date"]))

    lines = [
        "# Échéances — dossier SYN-2026-001 (document fictif de démonstration)",
        "",
        "| Date | Événement (extrait de la source) | Source |",
        "|---|---|---|",
    ]
    for item in ordered:
        lines.append(f"| {item['date']} | {item['context']} | {item['source']} |")
    lines.append("")
    output = params.get("output", "echeances.md")
    sandbox.write_text(output, "\n".join(lines))
    return {"output": output, "deadlines": ordered, "count": len(ordered)}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"\([^)]*\)", " ", text)  # drop parentheticals
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_requirements(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        match = re.match(r"\s*\d+\.\s+(.*)", line)
        if match:
            items.append(match.group(1).strip())
    return items


def _parse_inventory(text: str) -> tuple[list[str], list[str]]:
    present, absent = [], []
    section = None
    for line in text.splitlines():
        if line.startswith("Présent"):
            section = present
        elif line.startswith("Absent"):
            section = absent
        match = re.match(r"\s*- \[[ x]\]\s+(.*)", line)
        if match and section is not None:
            name = match.group(1)
            name = name.split(" — ")[0].replace("**", "").strip()
            section.append(name)
    return present, absent


def build_checklist(params: dict, sandbox: Sandbox) -> dict:
    requirements = _parse_requirements(
        sandbox.read_text(params.get("requirements", "03-liste-exigences.md"))
    )
    present, absent = _parse_inventory(
        sandbox.read_text(params.get("inventory", "04-inventaire-pieces.md"))
    )
    present_norm = {_normalize(name): name for name in present}
    absent_norm = {_normalize(name): name for name in absent}

    rows, missing = [], []
    for requirement in requirements:
        req_norm = _normalize(requirement)
        hit = next(
            (orig for norm, orig in present_norm.items()
             if req_norm.startswith(norm) or norm.startswith(req_norm)),
            None,
        )
        if hit is not None:
            rows.append((requirement, True))
        else:
            gone = next(
                (orig for norm, orig in absent_norm.items()
                 if req_norm.startswith(norm) or norm.startswith(req_norm)),
                requirement,
            )
            rows.append((requirement, False))
            missing.append(gone)

    lines = [
        "# Liste des documents — dossier SYN-2026-001 (démonstration fictive)",
        "",
    ]
    for requirement, ok in rows:
        lines.append(f"- [{'x' if ok else ' '}] {requirement}")
    lines.append("")
    if missing:
        lines.append("## Pièce manquante à signaler")
        for name in missing:
            lines.append(f"- ⚠ **{name}** — non versée au dossier")
        lines.append("")
    output = params.get("output", "checklist.md")
    sandbox.write_text(output, "\n".join(lines))
    return {"output": output, "checked": len(rows), "missing": missing}


# ---------------------------------------------------------------------------
# Sensitive steps (executed only after explicit approval)
# ---------------------------------------------------------------------------

def draft_case_summary_letter(params: dict, sandbox: Sandbox) -> dict:
    deadlines = []
    if sandbox.exists("echeances.md"):
        for line in sandbox.read_text("echeances.md").splitlines():
            if line.startswith("| ") and "Date" not in line and "---" not in line:
                cells = [c.strip() for c in line.strip("|").split("|")]
                if cells:
                    deadlines.append(cells[0])
    missing = ["Preuve de revenu récente"]
    if sandbox.exists("checklist.md"):
        found = re.findall(r"⚠ \*\*(.+?)\*\*", sandbox.read_text("checklist.md"))
        if found:
            missing = found

    letter = "\n".join([
        "# [LETTRE FICTIVE — DÉMO SYN-2026-001]",
        "",
        "Ville-Fictive, date de démonstration",
        "",
        "À l'Office municipal d'habitation de Ville-Fictive (organisme inventé)",
        "Objet : Résumé du cas — dossier SYN-2026-001 — Mme A. Tremblay (persona fictive)",
        "",
        "Madame, Monsieur,",
        "",
        "Ce résumé fictif concerne le dossier SYN-2026-001. Un logement de "
        "2 pièces au 100, rue de la Démonstration, à Ville-Fictive, est "
        "attribué à Mme A. Tremblay ; le loyer mensuel fictif est de 940 $, "
        "chauffage et eau chaude inclus, pour un bail fictif du 15 novembre "
        "2026 au 30 juin 2027.",
        "",
        "Échéances relevées au dossier : "
        + (", ".join(deadlines) if deadlines else "voir echeances.md") + ".",
        "",
        "État des pièces : le dossier est complet à l'exception de la pièce "
        "suivante, signalée à l'étape de la liste de contrôle : "
        + ", ".join(missing) + ".",
        "",
        "Ce document est entièrement fictif et produit pour la "
        "démonstration du Governed Agent Bridge.",
        "",
        "Salutations distinguées,",
        "L'agente de démonstration (fictive)",
        "",
    ])
    output = params.get("output", "lettre-resume-cas.md")
    sandbox.write_text(output, letter)
    return {"output": output, "chars": len(letter)}


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap(text: str, width: int = 92) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            if cut <= 0:
                cut = width
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return out


def _build_pdf(documents: list[tuple[str, str]]) -> bytes:
    """Minimal, valid, multi-page text PDF — standard library only."""
    pages: list[list[str]] = []
    for title, body in documents:
        lines = [title, "=" * len(title), ""] + _wrap(body)
        for i in range(0, len(lines), 46):
            pages.append(lines[i:i + 46])
    if not pages:
        pages = [["(empty package)"]]

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, page_lines in enumerate(pages):
        content_lines = ["BT /F1 9 Tf 14 TL 48 792 Td"]
        for line in page_lines:
            safe = _pdf_escape(line).encode("latin-1", "replace").decode("latin-1")
            content_lines.append(f"({safe}) Tj T*")
        content_lines.append("ET")
        content = "\n".join(content_lines).encode("latin-1", "replace")
        page_obj = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>"
        ).encode()
        stream_obj = (
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n"
            + content + b"\nendstream"
        )
        objects.append(page_obj)
        objects.append(stream_obj)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


def assemble_export_package(params: dict, sandbox: Sandbox) -> dict:
    documents, skipped = [], []
    for name in params.get("includes", []):
        if sandbox.exists(name):
            documents.append((name, sandbox.read_text(name)))
        else:
            skipped.append(name)
    pdf = _build_pdf(documents)
    output = params.get("output", "paquet-SYN-2026-001.pdf")
    sandbox.write_bytes(output, pdf)
    return {
        "output": output,
        "format": params.get("format", "pdf"),
        "documents": [name for name, _ in documents],
        "skipped_missing": skipped,
        "bytes": len(pdf),
        "sha256_16": hashlib.sha256(pdf).hexdigest()[:16],
    }


ACTION_HANDLERS = {
    "read_document": read_document,
    "inspect_workspace": inspect_workspace,
    "extract_deadlines": extract_deadlines,
    "build_checklist": build_checklist,
    "draft_case_summary_letter": draft_case_summary_letter,
    "assemble_export_package": assemble_export_package,
}
