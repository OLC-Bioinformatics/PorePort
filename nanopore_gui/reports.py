from __future__ import annotations

import csv
import html
import os
import json
import logging
from datetime import datetime
from pathlib import Path
import re
from typing import Any, Iterable

logger = logging.getLogger("nanopore_gui.reports")

LAB_ADDRESSES = {
    "GTA": "2301 Midland Ave., Scarborough, ON, M1P 4R7",
    "BUR": "3155 Willingdon Green, Burnaby, BC, V5G 4P2",
    "OLC": "960 Carling Ave, Building 22 CEF, Ottawa, ON, K1A 0Y9",
    "FFFM": "960 Carling Ave, Building 22 CEF, Ottawa, ON, K1A 0Y9",
    "DAR": "1992 Agency Dr., Dartmouth, NS, B2Y 3Z7",
    "CAL": "3650 36 Street NW, Calgary, AB, T2L 2L1",
    "STH": "3400 Casavant Boulevard W., St. Hyacinthe, QC, J2S 8E3",
}

_SAMPLE_KEYS = (
    "seqid", "sequence_id", "sample_id", "sample", "isolate", "strain"
)
_OLNID_KEYS = ("olnid", "oln_id", "lab_id", "laboratory_id")
_GENE_KEYS = ("gene_name", "gene", "target", "target_name", "allele")
_READ_KEYS = (
    "number_of_reads_mapped", "mapped_reads", "read_count", "reads", "depth"
)
_COVERAGE_KEYS = (
    "percent_coverage", "coverage", "genome_coverage", "coverage_percent"
)
# Numeric cutoff for the existing Coverage report field. Its upstream units
# are not guaranteed to represent fold depth (x).
COVERAGE_SUFFICIENT_THRESHOLD = 20.0

_REPORT_COLUMNS = (
    "SEQID", "OLN ID", "O-Type", "H-Type", "stx1", "stx2", "eae",
    "hylA", "aggR", "aaiC", "uidA", "GDCS", "Coverage",
)
_EXCLUDED_GDCS = re.compile(
    r"O/|H/|Stx|eae|ehxA|hylA|aggR|aaiC|uidA|#|genome_coverage",
    re.IGNORECASE,
)

REPORT_STATES = ("Draft", "Under Review", "Approved", "Released", "Superseded")


def repository_asset(name: str, context: dict | None = None) -> Path | None:
    configured = (context or {}).get(name.lower().replace(".", "_"))
    here = Path(__file__).resolve()
    candidates = ([Path(configured).expanduser()] if configured else [])
    # The package's assets directory is the canonical location. Keep the
    # previous locations as fallbacks for existing checkouts and deployments.
    candidates.append(here.parent / "assets" / name)
    for base in (here.parent, here.parent.parent, Path.cwd()):
        candidates.extend((base / name, base / "assets" / name))
    return next((path for path in candidates if path.is_file()), None)


def reference_gdcs_total(context: dict | None = None) -> int | None:
    reference = (repository_asset("PoreSippRDB_240509.fasta", context) or repository_asset("PoreSippRDB_240509.txt", context))
    if reference is None:
        return None
    with reference.open("r", encoding="utf-8", errors="replace") as handle:
        descriptions = [line[1:].strip() for line in handle if line.startswith(">")]
    return sum(1 for description in descriptions if not _EXCLUDED_GDCS.search(description))


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _lookup(row: dict, aliases: Iterable[str]):
    normalized = {_normalized_key(key): value for key, value in row.items()}
    for alias in aliases:
        value = normalized.get(alias)
        if value not in (None, ""):
            return value
    return None


def _number(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    text = text.rstrip("Xx")
    if not text or text.lower() in ("na", "n/a", "none", "nan", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _sample_from_path(path: Path) -> str:
    return re.sub(r"_iteration\d+$", "", path.stem, flags=re.IGNORECASE)


def read_iteration_csvs(csv_paths: Iterable[Path]):
    documents = []
    for csv_path in sorted(Path(path) for path in csv_paths):
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = []
            for row in reader:
                clean = {
                    str(key).strip(): ("" if value is None else str(value).strip())
                    for key, value in row.items()
                    if key is not None
                }
                if any(clean.values()):
                    rows.append(clean)
        documents.append({
            "path": csv_path,
            "sample": _sample_from_path(csv_path),
            "columns": list(rows[0]) if rows else fieldnames,
            "rows": rows,
        })
    return documents


def _metadata_map(context: dict | None) -> dict[str, dict]:
    result = {}
    metadata = (context or {}).get("sample_metadata") or {}
    for sample in metadata.get("samples", []):
        seqid = str(sample.get("seqid") or "").strip()
        if seqid:
            result[seqid] = sample
    return result


def _gene_rows(rows):
    values = []
    for row in rows:
        gene = _lookup(row, _GENE_KEYS)
        if not gene:
            continue
        reads = _number(_lookup(row, _READ_KEYS))
        values.append((str(gene), reads or 0.0))
    return values


def _sum_matching(values, pattern) -> int | str:
    total = sum(reads for gene, reads in values if re.search(pattern, gene, re.I))
    return int(total) if total > 1 else "-"


def _serotype(values, marker: str) -> str:
    grouped = {}
    for gene, reads in values:
        if not re.search(r"^{0}/".format(marker), gene, re.I):
            continue
        match = re.search(r"{0}/[^0-9]*([0-9]+)".format(marker), gene, re.I)
        if not match:
            continue
        label = "{0}{1}".format(marker.upper(), match.group(1))
        grouped[label] = grouped.get(label, 0.0) + reads
    supported = [(label, reads) for label, reads in grouped.items() if reads > 1]
    if not supported:
        return "-"
    label, reads = max(supported, key=lambda item: item[1])
    return "{0} ({1})".format(label, int(reads))


def _coverage(rows, values) -> float:
    for gene, reads in values:
        if "genome_coverage" in gene.lower():
            return round(float(reads), 2)
    coverages = [
        value for value in (_number(_lookup(row, _COVERAGE_KEYS)) for row in rows)
        if value is not None
    ]
    return round(max(coverages), 2) if coverages else 0.0


def summarize_documents(documents, context: dict | None = None):
    """Create the PoreSippr identification summary used by GUI and PDF."""
    metadata = _metadata_map(context)
    summaries = []
    for document in documents:
        rows = document["rows"]
        first = rows[0] if rows else {}
        seqid = str(_lookup(first, _SAMPLE_KEYS) or document["sample"])
        sample_meta = metadata.get(seqid, {})
        olnid = str(
            _lookup(first, _OLNID_KEYS)
            or sample_meta.get("olnid")
            or ""
        )
        values = _gene_rows(rows)
        gdcs = [(gene, reads) for gene, reads in values if not _EXCLUDED_GDCS.search(gene)]
        gdcs_total = reference_gdcs_total(context) or len({gene for gene, _reads in gdcs})
        gdcs_detected = len({gene for gene, reads in gdcs if reads > 1})
        summaries.append({
            "SEQID": seqid,
            "OLN ID": olnid,
            "O-Type": _serotype(values, "O"),
            "H-Type": _serotype(values, "H"),
            "stx1": _sum_matching(values, r"Stx1"),
            "stx2": _sum_matching(values, r"Stx2"),
            "eae": _sum_matching(values, r"(^|[/_-])eae($|[/_-])|^eae"),
            "hylA": _sum_matching(values, r"ehxA|hylA"),
            "aggR": _sum_matching(values, r"aggR"),
            "aaiC": _sum_matching(values, r"aaiC"),
            "uidA": _sum_matching(values, r"uidA"),
            "GDCS": "{0}/{1}".format(gdcs_detected, gdcs_total),
            "Coverage": _coverage(rows, values),
        })
    return summaries


def _write_combined_csv(documents, destination: Path) -> Path:
    columns = ["source_csv"]
    for document in documents:
        for column in document["columns"]:
            if column not in columns:
                columns.append(column)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for document in documents:
            for row in document["rows"]:
                item = {"source_csv": document["path"].name}
                item.update(row)
                writer.writerow(item)
    return destination


def _write_summary_csv(rows, destination: Path) -> Path:
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_REPORT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return destination


def _cell_class(column, value):
    if value in (None, "", "-"):
        return "neutral"
    if column == "Coverage":
        return "low" if float(value) < COVERAGE_SUFFICIENT_THRESHOLD else "positive"
    if column == "GDCS":
        detected, total = str(value).split("/", 1)
        return "positive" if total != "0" and detected == total else "low"
    if column not in ("SEQID", "OLN ID"):
        return "positive"
    return "neutral"



def _provenance_lines(context: dict) -> list[tuple[str, str]]:
    reference = context.get("reference_database") or {}
    return [
        ("GUI / workflow", str(context.get("version") or "FoodPort PoreSippr")),
        ("Generated", str(context.get("analysis_generated_at") or datetime.now().isoformat())),
        ("Reference database", str(reference.get("name") or "Not recorded")),
        ("Reference records", str(reference.get("records") or "Not recorded")),
        ("Reference SHA-256", str(reference.get("sha256") or "Not recorded")),
        ("Detection rule", "Mapped-read support greater than 1; Coverage below {0:g} is flagged (source metric units)".format(COVERAGE_SUFFICIENT_THRESHOLD)),
    ]

def _write_html(iteration: int, rows: list[dict], destination: Path, context: dict) -> Path:
    lab = context.get("lab_name", "OLC")
    address = LAB_ADDRESSES.get(lab, "")
    run_name = context.get("run_name") or "Nanopore run"
    issued = datetime.today().strftime("%Y-%m-%d")
    parts = ["<!doctype html><html><head><meta charset='utf-8'>",
             "<title>{0} report</title>".format(html.escape(run_name)),
             "<style>body{font-family:Arial,sans-serif;margin:28px;color:#20302d}",
             "h1{color:#12483f} .meta{color:#52645f}.address{margin:16px 0}",
             "table{border-collapse:collapse;width:100%;font-size:12px}",
             "th,td{border:1px solid #bbc9c4;padding:7px;text-align:center}",
             "th{background:#176b5b;color:white}.positive{background:#2f75b5;color:white}",
             ".low{background:#d3d3d3}.neutral{background:white}.doc-fields{border:1px solid #bbc9c4;padding:12px;margin-top:18px}.doc-fields label{display:block;margin:8px 0}.doc-fields input,.doc-fields select{min-width:300px;padding:5px}</style></head><body>",
             "<p class='meta'>Date Report Issued: {0}</p>".format(issued),
             "<h1>Report of PoreSippr Analysis Identification Summary</h1>",
             "<p>PoreSippr analyses were conducted on {0} <i>Escherichia coli</i> strains. "
             "Strains are confirmed <i>Escherichia coli</i> based on the presence of the "
             "<i>uidA</i> marker.</p>".format(len(rows)),
             "<div class='address'><b>{0} Laboratory Address</b><br>{1}</div>".format(
                 html.escape(lab), html.escape(address)),
             "<p class='meta'>Run: {0} | Iteration: {1} | Report state: {2}</p>".format(
                 html.escape(run_name), iteration, html.escape(str(context.get("report_state", "Draft")))),
             "<h2>Analysis provenance</h2><ul>{0}</ul>".format("".join("<li><b>{0}:</b> {1}</li>".format(html.escape(k), html.escape(v)) for k, v in _provenance_lines(context))),
             "<p><b>Interpretation:</b> A dash indicates no supported detection. Low coverage is flagged separately and must not be interpreted as a confirmed negative.</p>",
             "<table><thead><tr>"]
    parts.extend("<th>{0}</th>".format(html.escape(column)) for column in _REPORT_COLUMNS)
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for column in _REPORT_COLUMNS:
            value = row.get(column, "-")
            parts.append("<td class='{0}'>{1}</td>".format(
                _cell_class(column, value), html.escape(str(value))))
        parts.append("</tr>")
    parts.extend([
        "</tbody></table>",
        "<p class='meta'>This report was generated with {0}.</p>".format(
            html.escape(str(context.get("version", "FoodPort PoreSippr")))),
        "<div class='doc-fields'><h2>Report document fields</h2>",
        "<label>Report state: <select><option{0}>Draft</option><option{1}>Under Review</option><option{2}>Approved</option><option{3}>Released</option><option{4}>Superseded</option></select></label>".format(
            *[" selected" if context.get("report_state", "Draft") == state else "" for state in REPORT_STATES]),
        "<label>RDIMS document ID: <input value='{0}'></label>".format(html.escape(str(context.get("rdims_document_id") or ""), quote=True)),
        "<label>Reviewer: <input value='{0}'></label>".format(html.escape(str(context.get("reviewer_name") or ""), quote=True)),
        "<label>Approval timestamp: <input value='{0}'></label>".format(html.escape(str(context.get("approval_timestamp") or ""), quote=True)),
        "<label>Digital signature reference: <input value='{0}'></label></div>".format(html.escape(str(context.get("digital_signature") or ""), quote=True)),
        "</body></html>",
    ])
    destination.write_text("".join(parts), encoding="utf-8")
    return destination


def _pdf_background(column, value):
    from reportlab.lib import colors
    cell_class = _cell_class(column, value)
    if cell_class == "positive":
        return colors.HexColor("#2f75b5"), colors.white
    if cell_class == "low":
        return colors.HexColor("#d3d3d3"), colors.black
    return colors.white, colors.black


def _write_pdf(iteration: int, rows: list[dict], destination: Path, context: dict) -> Path:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, Image,
        )
    except ImportError as exc:
        raise RuntimeError(
            "PDF report generation requires reportlab. Install it with "
            "'python -m pip install reportlab'."
        ) from exc

    lab = context.get("lab_name", "OLC")
    address = LAB_ADDRESSES.get(lab, "")
    run_name = context.get("run_name") or "Nanopore run"
    version = context.get("version", "FoodPort PoreSippr")
    issued = datetime.today().strftime("%Y-%m-%d")
    document = SimpleDocTemplate(
        str(destination), pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=10 * mm, bottomMargin=10 * mm,
        title="Report of PoreSippr Analysis Identification Summary",
        author="Canadian Food Inspection Agency",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=16, leading=19, textColor=colors.HexColor("#12483f"),
        spaceAfter=7,
    )
    body = ParagraphStyle(
        "ReportBody", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=9, leading=12, spaceAfter=5,
    )
    small = ParagraphStyle(
        "ReportSmall", parent=body, fontSize=7.5, leading=9,
        textColor=colors.HexColor("#52645f"),
    )
    story = []
    logo = repository_asset("CFIA_logo.png", context) or repository_asset("cfia.png", context)
    if logo:
        story.extend([Image(str(logo), width=48 * mm, height=15 * mm, kind="proportional"), Spacer(1, 2 * mm)])
    story.extend([
        Paragraph("Date Report Issued: {0}".format(issued), small),
        Paragraph("Report of PoreSippr Analysis Identification Summary", title_style),
        Paragraph(
            "PoreSippr analyses were conducted on {0} <i>Escherichia coli</i> "
            "strains. Strains are confirmed <i>Escherichia coli</i> based on "
            "the presence of the <i>uidA</i> marker.".format(len(rows)), body),
        Paragraph("<b>{0} Laboratory Address</b><br/>{1}".format(lab, address), body),
        Paragraph("Run: {0} | Iteration: {1} | Report state: {2}".format(run_name, iteration, context.get("report_state", "Draft")), small),
        Spacer(1, 2 * mm),
        Paragraph("<b>Analysis provenance</b>", body),
        Table([[key, value] for key, value in _provenance_lines(context)], colWidths=[38 * mm, 180 * mm], style=TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd8d3")), ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e1eee9")), ("FONTSIZE", (0, 0), (-1, -1), 7)])),
        Paragraph("A dash indicates no supported detection. Low coverage is flagged separately and must not be interpreted as a confirmed negative.", small),
        Spacer(1, 2 * mm),
    ])
    table_data = [list(_REPORT_COLUMNS)] + [
        [str(row.get(column, "-")) for column in _REPORT_COLUMNS]
        for row in rows
    ]
    widths = [31, 28, 23, 23, 14, 14, 14, 14, 14, 14, 14, 20, 21]
    table = Table(table_data, colWidths=[value * mm for value in widths], repeatRows=1)
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#176b5b")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 6.8),
        ("LEADING", (0, 0), (-1, -1), 8.2),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#aab8b3")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for row_index, row in enumerate(rows, start=1):
        for column_index, column in enumerate(_REPORT_COLUMNS):
            background, foreground = _pdf_background(column, row.get(column, "-"))
            commands.append(("BACKGROUND", (column_index, row_index), (column_index, row_index), background))
            commands.append(("TEXTCOLOR", (column_index, row_index), (column_index, row_index), foreground))
    table.setStyle(TableStyle(commands))
    story.extend([
        KeepTogether(table), Spacer(1, 4 * mm),
        Paragraph(
            "RDIMS interpretation document ID: {0}".format(
                context.get("rdims_document_id") or "____________________________"
            ), small),
        Paragraph("This report was generated with {0}.".format(version), small),
        Spacer(1, 3 * mm),
        Table([
            ["Report state", str(context.get("report_state", "Draft")), "Reviewer", str(context.get("reviewer_name") or "____________________________")],
            ["Approval timestamp", str(context.get("approval_timestamp") or "____________________________"), "Digital signature", str(context.get("digital_signature") or "____________________________")],
        ], colWidths=[30 * mm, 72 * mm, 35 * mm, 105 * mm], style=TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#aab8b3")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e1eee9")),
            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#e1eee9")),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])),
    ])
    document.build(story)
    return destination


def _write_summary_table_image(iteration: int, rows: list[dict], destination: Path) -> Path:
    """Save the same summary values and highlighting as the PDF as a PNG."""
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Summary table image generation requires matplotlib. Install it "
            "with 'python -m pip install matplotlib'."
        ) from exc

    # Match the PDF column proportions and the HTML/PDF classification colours.
    widths = [31, 28, 23, 23, 14, 14, 14, 14, 14, 14, 14, 20, 21]
    figure_width = 16.5
    figure_height = max(1.1, 0.43 * (len(rows) + 1) + 0.18)
    figure, axis = plt.subplots(figsize=(figure_width, figure_height))
    try:
        figure.patch.set_facecolor("white")
        axis.axis("off")
        cells = [list(_REPORT_COLUMNS)] + [
            [str(row.get(column, "-")) for column in _REPORT_COLUMNS]
            for row in rows
        ]
        table = axis.table(
            cellText=cells,
            cellLoc="center",
            colWidths=[float(width) / sum(widths) for width in widths],
            bbox=[0, 0, 1, 1],
        )
        table.auto_set_font_size(False)
        table.set_fontsize(8.5)
        for (row_index, column_index), cell in table.get_celld().items():
            cell.set_edgecolor("#aab8b3")
            cell.set_linewidth(0.6)
            if row_index == 0:
                cell.set_facecolor("#176b5b")
                cell.get_text().set_color("white")
                cell.get_text().set_weight("bold")
            else:
                column = _REPORT_COLUMNS[column_index]
                cell_class = _cell_class(column, rows[row_index - 1].get(column, "-"))
                if cell_class == "positive":
                    cell.set_facecolor("#2f75b5")
                    cell.get_text().set_color("white")
                    cell.get_text().set_weight("bold")
                elif cell_class == "low":
                    cell.set_facecolor("#d3d3d3")
                    cell.get_text().set_color("black")
                else:
                    cell.set_facecolor("white")
                    cell.get_text().set_color("black")
        path = destination / "iteration-{0:06d}-summary.png".format(iteration)
        figure.savefig(str(path), dpi=160, facecolor="white", pad_inches=0.04)
        return path
    finally:
        plt.close(figure)


def _write_figures(documents, destination: Path):
    artifacts = []
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        logger.info("report_figures_skipped reason=matplotlib_not_installed")
        return artifacts

    samples, maximum_coverage, detected_targets = [], [], []
    for document in documents:
        rows = document["rows"]
        first = rows[0] if rows else {}
        samples.append(str(_lookup(first, _SAMPLE_KEYS) or document["sample"]))
        values = _gene_rows(rows)
        maximum_coverage.append(_coverage(rows, values))
        detected_targets.append(sum(1 for _gene, reads in values if reads > 1))

    def save(values, title, ylabel, filename):
        if not samples or not any(values):
            return
        figure, axis = plt.subplots(figsize=(8.5, 4.8))
        axis.bar(samples, values, color="#176b5b")
        axis.set_title(title)
        axis.set_ylabel(ylabel)
        axis.tick_params(axis="x", rotation=35)
        axis.grid(axis="y", alpha=0.25)
        figure.tight_layout()
        path = destination / filename
        figure.savefig(path, dpi=160)
        plt.close(figure)
        artifacts.append(path)

    save(maximum_coverage, "Maximum coverage by sample", "Coverage", "coverage.png")
    save(detected_targets, "Detected targets by sample", "Targets", "detected-targets.png")
    return artifacts


def build_cross_iteration_outputs(run_directory: Path, context: dict) -> list[Path]:
    manifests = []
    for path in sorted(run_directory.glob("iteration-*/report.json")):
        try:
            manifests.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            logger.exception("trend_manifest_load_failed path=%s", path)
    pages = sorted([page for manifest in manifests for page in manifest.get("iterations", [])], key=lambda page: int(page.get("iteration", 0)))
    if not pages:
        return []
    outputs = []
    try:
        import matplotlib.pyplot as plt
        samples = sorted({row.get("SEQID", "Unknown") for page in pages for row in page.get("rows", [])})
        xs = [int(page.get("iteration", 0)) for page in pages]
        for field, title, ylabel, filename in (
            ("Coverage", "Coverage across iterations", "Coverage", "trend-coverage.png"),
            ("GDCS", "GDCS targets across iterations", "Detected targets", "trend-gdcs.png"),
            ("stx1", "stx1 reads across iterations", "Mapped reads", "trend-stx1.png"),
        ):
            figure, axis = plt.subplots(figsize=(8.5, 4.8)); plotted = False
            for sample in samples:
                values = []
                for page in pages:
                    row = next((item for item in page.get("rows", []) if item.get("SEQID") == sample), {})
                    value = row.get(field, 0)
                    if field == "GDCS": value = str(value).split("/", 1)[0]
                    values.append(_number(value) or 0)
                if any(values): axis.plot(xs, values, marker="o", label=sample); plotted = True
            if plotted:
                axis.set_title(title); axis.set_xlabel("Iteration"); axis.set_ylabel(ylabel); axis.grid(alpha=.25); axis.legend(); figure.tight_layout()
                output = run_directory / filename; figure.savefig(output, dpi=160); outputs.append(output)
            plt.close(figure)
    except ImportError:
        logger.info("trend_figures_skipped reason=matplotlib_not_installed")
    final_pdf = run_directory / "{0}_final-report.pdf".format(re.sub(r"[^A-Za-z0-9_.-]+", "-", str(context.get("run_name") or "nanopore")))
    latest = pages[-1]
    _write_pdf(int(latest.get("iteration", 0)), latest.get("rows", []), final_pdf, context)
    outputs.append(final_pdf)
    return outputs


def build_iteration_report(
    iteration: int,
    csv_paths: Iterable[Path],
    destination: Path,
    context: dict | None = None,
) -> dict[str, Any]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    context = dict(context or {})
    documents = read_iteration_csvs(csv_paths)
    if not documents:
        raise ValueError("The iteration manifest did not provide report CSV files.")
    rows = summarize_documents(documents, context)
    run_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(context.get("run_name") or "nanopore"))
    combined_csv = _write_combined_csv(
        documents, destination / "iteration-{0:06d}-all-rows.csv".format(iteration))
    summary_csv = _write_summary_csv(
        rows, destination / "iteration-{0:06d}-summary.csv".format(iteration))
    html_path = _write_html(
        iteration, rows,
        destination / "iteration-{0:06d}-report.html".format(iteration), context)
    pdf_path = _write_pdf(
        iteration, rows,
        destination / "{0}_iteration-{1:06d}_report.pdf".format(run_name, iteration),
        context)
    latest_pdf = destination.parent / "{0}_report.pdf".format(run_name)
    latest_pdf.write_bytes(pdf_path.read_bytes())
    summary_image = _write_summary_table_image(iteration, rows, destination)
    figures = _write_figures(documents, destination)
    artifacts = [summary_csv, combined_csv, html_path, pdf_path, summary_image] + figures
    report = {
        "iterations": [{"iteration": iteration, "rows": rows}],
        "artifacts": [str(path) for path in artifacts],
        "csv_files": [str(document["path"]) for document in documents],
        "summary_pdf": str(pdf_path),
        "summary_image": str(summary_image),
        "latest_summary_pdf": str(latest_pdf),
        "context": context,
    }
    manifest_path = destination / "report.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    trends = build_cross_iteration_outputs(destination.parent, context)
    report["artifacts"].extend(str(path) for path in trends)
    report["trend_artifacts"] = [str(path) for path in trends]
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["manifest_path"] = str(manifest_path)
    report["report_directory"] = str(destination)
    return report


def generate_target_report(
    iteration: int,
    csv_paths: Iterable[Path],
    destination: Path,
    context: dict | None = None,
) -> dict[str, Any]:
    """Generate a user-requested PDF/HTML report without changing cached iterations.

    Write the marker last, so the GUI only exposes a fully generated report.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    context = dict(context or {})
    documents = read_iteration_csvs(csv_paths)
    if not documents:
        raise ValueError("The iteration manifest did not provide report CSV files.")
    rows = summarize_documents(documents, context)
    run_name = re.sub(
        r"[^A-Za-z0-9_.-]+", "-", str(context.get("run_name") or "nanopore")
    )
    pdf_path = _write_pdf(
        iteration, rows, destination / "{0}_report.pdf".format(run_name), context
    )
    html_path = _write_html(
        iteration, rows, destination / "{0}_report.html".format(run_name), context
    )
    marker = {
        "iteration": iteration,
        "pdf": str(pdf_path.resolve()),
        "html": str(html_path.resolve()),
        "context": context,
    }
    marker_path = destination / "generated-target-report.json"
    temporary = marker_path.with_name(marker_path.name + ".part")
    temporary.write_text(
        json.dumps(marker, indent=2, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(marker_path)
    return marker


def load_report(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
