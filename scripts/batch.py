"""Folder measurement bundles and a local, navigable scorecard."""
import html
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import measure as scanner
from .evidence import read_object, write_json, digest_file, load_lines


def measure_folders(root, output, folders, workers=6, budget_seconds=7200, file_timeout=30, max_file_mb=512):
    root = Path(root).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    selected = []
    for name in folders:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
            raise ValueError("folder names must be immediate children of the selected root")
        source = root / name
        if not source.is_dir() or source.is_symlink():
            raise ValueError("selected folder is missing or symlinked: " + name)
        if output.is_relative_to(source):
            raise ValueError("batch output must be outside every measured folder")
        if name in selected:
            raise ValueError("duplicate folder selection")
        selected.append(name)
    if not selected:
        raise ValueError("select at least one folder")
    output = scanner.new_output(output)
    manifest = {"schema": "artifact-health-batch@1", "source_root": str(root), "state": "running",
                "started_at": datetime.now(timezone.utc).isoformat(), "folders": selected, "completed": [],
                "options": {"workers": workers, "budget_seconds": budget_seconds,
                            "file_timeout": file_timeout, "max_file_mb": max_file_mb}}
    write_json(output / "batch.json", manifest)
    try:
        for name in selected:
            measurement, code = scanner.measure(root / name, output / name, **manifest["options"])
            manifest["completed"].append(name)
            write_json(output / "batch.json", manifest)
            print(json.dumps({"folder_completed": name, "files": measurement["coverage"]["files_discovered"],
                              "measurement_status": measurement["status"], "exit_code": code}), file=sys.stderr, flush=True)
        manifest["state"] = "completed"
    except BaseException as exc:
        manifest.update(state="error", error_type=type(exc).__name__)
        raise
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "batch.json", manifest)
    return refresh_batch(output)


def _table(headers, rows):
    return '<table><thead><tr>' + ''.join('<th>' + html.escape(str(v)) + '</th>' for v in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + (v if isinstance(v, SafeHTML) else html.escape(str(v))) + '</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table>'


class SafeHTML(str):
    pass


def _link(target, label):
    from urllib.parse import quote
    return SafeHTML('<a href="' + html.escape(quote(target, safe='/')) + '">' + html.escape(label) + '</a>')


def _page(title, body):
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + html.escape(title) + '</title><style>body{font:16px/1.5 system-ui,sans-serif;color:#17253b;background:#f5f7fb;margin:0}main{max-width:1300px;margin:auto;padding:32px}h1{font-size:32px;margin-bottom:8px}h2{margin-top:36px}p{max-width:1000px}table{border-collapse:collapse;background:white;width:100%;font-variant-numeric:tabular-nums}th,td{padding:12px;border-bottom:1px solid #dce3ed;text-align:left;vertical-align:top}th{background:#eaf0f8}a{color:#174b9d}.note{background:#fff4d7;border-left:4px solid #bc8619;padding:14px}.score{font-size:36px;font-weight:700}.scroll{overflow:auto}</style><main><h1>' + html.escape(title) + '</h1>' + body + '</main></html>'


def refresh_batch(output):
    from artifact_health import read_artifact_health
    output = Path(output).expanduser().resolve()
    manifest = read_object(output / "batch.json")
    rubric = read_object(scanner.RUBRIC_PATH)
    rows = []
    for name in manifest["folders"]:
        directory = output / name
        if name not in manifest["completed"]:
            rows.append({"folder": name, "status": "not_measured", "artifact_health_score": None})
            continue
        grade_dir = output / "grades" / name
        has_grade = (grade_dir / "run.json").is_file()
        health = read_artifact_health(directory, grade_dir if has_grade else None, require="quality")
        measurement = read_object(directory / "measurement.json")
        quality = health["quality"]
        review = read_object(grade_dir / "review.json") if has_grade else None
        records = load_lines(directory / "inventory.jsonl")
        findings = load_lines(directory / "findings.jsonl")
        reviewed = set()
        if review:
            reviewed = {path for d in review["dimensions"].values() for path in d["reviewed_paths"]}
        total = measurement["coverage"]["files_discovered"]
        counts = measurement["coverage"]["status_counts"]
        score = quality["observed_quality_score"] if quality else None
        factor_scores = {key: quality["factors"][key]["score"] if quality else None for key in rubric["dimensions"]}
        row = {"folder": name, "artifact_health_score": score,
               "score_scope": "full" if quality and quality["quality_score"] is not None else "sample" if score is not None else "unscored",
               "status": health["status"], "files": total, "inspected": counts.get("inspected", 0),
               "invalid": counts.get("invalid", 0), "unavailable": counts.get("unavailable", 0) + counts.get("changed", 0),
               "files_hashed": measurement["coverage"]["files_hashed"], "reviewed_files": len(reviewed),
               "extraction_coverage_pct": round(100 * counts.get("inspected", 0) / total, 2) if total else None,
               "exact_duplicate_copies": measurement["metrics"]["exact_duplicate_copies"],
               "confirmed_findings": measurement["finding_counts"].get("DEFECT", 0),
               "review_findings": measurement["finding_counts"].get("REVIEW", 0),
               "material_points": health["material"].get("material_points"),
               "validated_task_count": health["tasks"].get("validated_task_count"),
               **factor_scores, "measurement_run_id": measurement["run_id"],
               "source_fingerprint": measurement["source_fingerprint"],
               "measurement_sha256": digest_file(directory / "measurement.json"),
               "grade_sha256": digest_file(grade_dir / "grade.json") if has_grade else None,
               "report": name + "/report.html"}
        rows.append(row)
        write_json(directory / "artifact-health.json", {"summary": row, "health": health})
        note = 'Score is a weighted assessment of the explicitly reviewed sample; it is not a whole-folder acceptance grade.' if row["score_scope"] == "sample" else 'No contextual grade has been completed.' if score is None else 'All required contextual dimensions were reviewed within the declared scope.'
        body = '<p>' + str(_link('../index.html', 'All folders')) + '</p><p class="score">' + ('Unscored' if score is None else str(score) + ' / 100') + '</p><p class="note">' + html.escape(note) + ' The rubric is provisional. Confirmed defects still block acceptance.</p>'
        body += _table(['Files', 'Inspected', 'Unavailable', 'Reviewed files', 'Health status'], [[total, row['inspected'], row['unavailable'], len(reviewed), row['status']]])
        body += '<h2>Factor scores</h2>' + _table(['Factor', 'Weight', 'Score / 100', 'Evidence and reasoning'], [[v['label'],v['weight'],factor_scores[k] if factor_scores[k] is not None else 'Unscored',review['dimensions'][k]['reason'] if review else 'Contextual review has not run.'] for k,v in rubric['dimensions'].items()])
        body += '<h2>Measurements by subfolder</h2>' + _table(['Subfolder', 'Files', 'Inspected', 'Invalid', 'Unavailable', 'Duplicate copies'], [[r[k] for k in ('collection','files','inspected','invalid','unavailable','duplicate_copies')] for r in measurement['collections']])
        body += '<h2>File formats</h2>' + _table(['Format','Files','Inspected','Unavailable'], [[ext,c['files'],c.get('inspected',0),c.get('unavailable',0)+c.get('changed',0)] for ext,c in measurement['coverage']['by_extension'].items()])
        finding_counts = Counter((f['kind'],f['code']) for f in findings)
        body += '<h2>Automated findings</h2>' + _table(['Type','Check','Occurrences'], [[kind,code,count] for (kind,code),count in sorted(finding_counts.items())])
        body += '<h2>Detailed outputs</h2><ul>' + ''.join('<li>' + str(_link(file,file)) + '</li>' for file in ['measurement.json','inventory.jsonl','findings.jsonl','artifact_collections.csv','artifact-health.json','run.json','measurement.log'] + (['../grades/' + name + '/' + file for file in ['grade.json','review.json','artifact_mining.json','artifact_material.json']] if has_grade else [])) + '</ul>'
        (directory / 'report.html').write_text(_page(name + ' — Artifact Health',body),encoding='utf-8')
    fields = ['folder','artifact_health_score','score_scope','status','files','inspected','invalid','unavailable','files_hashed','reviewed_files','extraction_coverage_pct','exact_duplicate_copies','confirmed_findings','review_findings','material_points','validated_task_count',*rubric['dimensions'],'report']
    scanner.write_csv(output / 'score-summary.csv', rows, fields)
    summary = {'schema':'artifact-health-folder-summary@1','updated_at':datetime.now(timezone.utc).isoformat(),
               'score_metric':'observed_quality_score','rubric_version':rubric['version'],'calibration_status':rubric['calibration_status'],
               'score_note':'Numeric scores describe actual contextual review in the stated scope; sampled scores do not certify entire folders.',
               'folders':rows}
    write_json(output / 'score-summary.json',summary)
    table_rows = [[_link(r['folder']+'/report.html',r['folder']),r.get('artifact_health_score') if r.get('artifact_health_score') is not None else 'Unscored',r.get('score_scope','unscored'),r['status'],r.get('files',''),r.get('inspected',''),r.get('reviewed_files',''),r.get('confirmed_findings','')] for r in rows]
    body = '<p>Folder-level measurements, quality factors, findings, and source-bound review evidence.</p><p class="note">Scores marked “sample” describe the files/pages actually reviewed. Automated inspection covers the reported inventory. Missing checks remain visible; a high score does not clear confirmed defects.</p><div class="scroll">' + _table(['Folder','Score / 100','Score scope','Health status','Files','Inspected','Reviewed files','Confirmed findings'],table_rows) + '</div><p>' + str(_link('score-summary.csv','Download score summary CSV')) + ' · ' + str(_link('score-summary.json','Full summary JSON')) + '</p>'
    (output/'index.html').write_text(_page('Artifact Health — Folder scorecard',body),encoding='utf-8')
    return {'status':'failed' if any(r['status']=='failed' for r in rows) else 'incomplete' if any(r['status']!='passed' for r in rows) else 'passed','output_directory':str(output),'folders':len(rows),'scored_folders':sum(r.get('artifact_health_score') is not None for r in rows)}
