"""Regression tests for PorePort report parsing and artifact generation."""

import csv
from pathlib import Path

import pytest

from nanopore_gui import reports


def make_csv(tmp_path, name="sampleA_iteration2.csv", rows=None):
    path = tmp_path / name
    rows = rows if rows is not None else [
        {"SEQID": "sampleA", "gene_name": "Stx1", "number_of_reads_mapped": "2"},
        {"SEQID": "sampleA", "gene_name": "genome_coverage", "number_of_reads_mapped": "20"},
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["SEQID", "gene_name", "number_of_reads_mapped"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_read_iteration_csvs_strips_bom_whitespace_and_blank_rows(tmp_path):
    path = tmp_path / "sampleA_iteration2.csv"
    path.write_text("\ufeffSEQID,gene_name,number_of_reads_mapped\n"
                    " sampleA , Stx1 , 2 \n,,\n", encoding="utf-8")
    document, = reports.read_iteration_csvs([path])
    assert document["sample"] == "sampleA"
    assert document["columns"] == ["SEQID", "gene_name", "number_of_reads_mapped"]
    assert document["rows"] == [{"SEQID": "sampleA", "gene_name": "Stx1", "number_of_reads_mapped": "2"}]


def test_summary_detects_only_support_above_one_and_uses_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(reports, "reference_gdcs_total", lambda context=None: 3)
    csv_path = make_csv(tmp_path, rows=[
        {"SEQID": "sampleA", "gene_name": "Stx1", "number_of_reads_mapped": "1"},
        {"SEQID": "sampleA", "gene_name": "Stx2", "number_of_reads_mapped": "2"},
        {"SEQID": "sampleA", "gene_name": "uidA", "number_of_reads_mapped": "3"},
        {"SEQID": "sampleA", "gene_name": "targetX", "number_of_reads_mapped": "2"},
        {"SEQID": "sampleA", "gene_name": "targetY", "number_of_reads_mapped": "1"},
        {"SEQID": "sampleA", "gene_name": "genome_coverage", "number_of_reads_mapped": "19.99"},
    ])
    context = {"sample_metadata": {"samples": [{"seqid": "sampleA", "olnid": "OLN-1"}]}}
    summary, = reports.summarize_documents(reports.read_iteration_csvs([csv_path]), context)
    assert summary["SEQID"] == "sampleA"
    assert summary["OLN ID"] == "OLN-1"
    assert summary["stx1"] == "-"
    assert summary["stx2"] == 2
    assert summary["uidA"] == 3
    assert summary["GDCS"] == "1/3"
    assert summary["Coverage"] == 19.99


@pytest.mark.parametrize("value,expected", [(19.99, "low"), (20, "positive"), (20.01, "positive")])
def test_coverage_classification_at_threshold(value, expected):
    assert reports._cell_class("Coverage", value) == expected


def test_html_escapes_user_supplied_context(tmp_path):
    output = reports._write_html(2, [], tmp_path / "report.html", {
        "run_name": "<unsafe>", "lab_name": "OLC", "reviewer_name": "' onfocus='bad",
    })
    html = output.read_text(encoding="utf-8")
    assert "&lt;unsafe&gt;" in html
    assert "<unsafe>" not in html
    assert "&#x27; onfocus=&#x27;bad" in html


def test_target_report_writes_pdf_html_and_marker_last(monkeypatch, tmp_path):
    csv_path = make_csv(tmp_path)
    destination = tmp_path / "run-1"
    events = []

    def fake_pdf(iteration, rows, path, context):
        events.append("pdf")
        path.write_bytes(b"%PDF-1.4\n")
        return path

    def fake_html(iteration, rows, path, context):
        events.append("html")
        assert not (destination / "generated-target-report.json").exists()
        path.write_text("<html></html>", encoding="utf-8")
        return path

    monkeypatch.setattr(reports, "_write_pdf", fake_pdf)
    monkeypatch.setattr(reports, "_write_html", fake_html)
    context = {"run_name": "run / 1", "lab_name": "OLC"}
    marker = reports.generate_target_report(2, [csv_path], destination, context)
    assert events == ["pdf", "html"]
    assert marker["iteration"] == 2
    assert marker["context"] == context
    assert Path(marker["pdf"]).name == "run-1_report.pdf"
    assert Path(marker["pdf"]).is_file()
    assert Path(marker["html"]).is_file()
    assert reports.load_report(destination / "generated-target-report.json") == marker
    assert not (destination / "generated-target-report.json.part").exists()


def test_target_report_does_not_publish_marker_if_rendering_fails(monkeypatch, tmp_path):
    csv_path = make_csv(tmp_path)
    destination = tmp_path / "run-1"
    def fail(*args):
        raise RuntimeError("PDF renderer failed")
    monkeypatch.setattr(reports, "_write_pdf", fail)
    with pytest.raises(RuntimeError, match="PDF renderer failed"):
        reports.generate_target_report(2, [csv_path], destination)
    assert not (destination / "generated-target-report.json").exists()


@pytest.mark.parametrize("function", ["generate_target_report", "build_iteration_report"])
def test_report_rejects_missing_csv_paths(function, tmp_path):
    with pytest.raises(ValueError, match="did not provide report CSV files"):
        getattr(reports, function)(1, [], tmp_path / "output")


def test_iteration_report_writes_manifest_and_artifacts(monkeypatch, tmp_path):
    csv_path = make_csv(tmp_path)
    destination = tmp_path / "run-1" / "iteration-000002"
    def fake_pdf(iteration, rows, path, context):
        path.write_bytes(b"%PDF-1.4\n")
        return path
    def fake_image(iteration, rows, path):
        output = path / "summary.png"
        output.write_bytes(b"PNG")
        return output
    monkeypatch.setattr(reports, "_write_pdf", fake_pdf)
    monkeypatch.setattr(reports, "_write_summary_table_image", fake_image)
    monkeypatch.setattr(reports, "_write_figures", lambda documents, path: [])
    monkeypatch.setattr(reports, "build_cross_iteration_outputs", lambda path, context: [])
    report = reports.build_iteration_report(2, [csv_path], destination, {"run_name": "run-1"})
    manifest = reports.load_report(destination / "report.json")
    assert report["iterations"][0]["iteration"] == 2
    assert manifest["iterations"] == report["iterations"]
    assert report["csv_files"] == [str(csv_path)]
    assert Path(report["summary_pdf"]).is_file()
    assert Path(report["summary_image"]).is_file()
    assert Path(report["latest_summary_pdf"]).read_bytes() == b"%PDF-1.4\n"
    assert all(Path(path).is_file() for path in report["artifacts"])
    with (destination / "iteration-000002-summary.csv").open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle))[0]["SEQID"] == "sampleA"
