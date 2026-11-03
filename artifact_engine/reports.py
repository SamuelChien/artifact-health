"""Recursive folder scorecards; task unions, not sums of child scores."""
import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from . import VERSION
from .contracts import ROLES, score
from .store import now, write_json


def ancestors(folder):
    yield folder
    for parent in Path(folder).parents:
        value=str(parent)
        if value!=folder: yield value


def csv_file(path, rows, fields):
    with Path(path).open("w",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore"); writer.writeheader()
        for row in rows:
            writer.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(("=","+","-","@")) else v) for k,v in row.items()})


def refresh(store):
    folders=[r[0] for r in store.db.execute("SELECT path FROM folders ORDER BY path")]
    direct={f:Counter() for f in folders}; recursive={f:Counter() for f in folders}
    for row in store.db.execute("SELECT folder,status,COUNT(*) AS n FROM files GROUP BY folder,status"):
        direct.setdefault(row["folder"],Counter())[row["status"]]+=row["n"]
        for folder in ancestors(row["folder"]): recursive.setdefault(folder,Counter())[row["status"]]+=row["n"]
    artifact_paths=defaultdict(list)
    for row in store.db.execute("SELECT path,folder,detail FROM files WHERE sha256 IS NOT NULL"):
        details=json.loads(row["detail"])
        if details.get("artifact_id"): artifact_paths[details["artifact_id"]].append((row["path"],row["folder"]))
    aliases={r['artifact_id']:r['canonical_artifact'] for r in store.db.execute('SELECT * FROM aliases')}
    for alias,canonical in aliases.items():
        artifact_paths[canonical].extend(p for p in artifact_paths[alias] if p not in artifact_paths[canonical])
    documents=defaultdict(list); unit_reviews={}; defects=[]
    for row in store.db.execute("SELECT result FROM jobs WHERE phase='classify' AND state='done'"):
        result=json.loads(row[0])
        for doc in result['documents']:
            documents[doc['artifact_id']].append(doc)
            defects.extend({'artifact_id':doc['artifact_id'],**f} for f in doc['findings'] if f['kind']=='DEFECT')
        for unit in result['unit_reviews']:unit_reviews[unit['unit_id']]=unit
    reviewed=set(); all_units=defaultdict(set); native_missing=defaultdict(int)
    administrative=set()
    for row in store.db.execute('SELECT id,data FROM artifacts'):
        data=json.loads(row['data'])
        if data.get('administrative_index') and data.get('index_verification',{}).get('state')=='met':administrative.add(row['id'])
    for row in store.db.execute('SELECT id,artifact_id,data FROM units'):
        unit=json.loads(row['data']);key=unit.get('canonical_id',unit['id'])
        if unit_reviews.get(unit['id'],{}).get('state')=='reviewed' or row['artifact_id'] in administrative:reviewed.add(key)
        for _,folder in artifact_paths[row['artifact_id']]:
            for folder in ancestors(folder):all_units[folder].add(key)
    format_counts=defaultdict(Counter); type_counts=defaultdict(Counter); origins=defaultdict(Counter)
    file_records=[]
    for row in store.db.execute('SELECT id,extension,data FROM artifacts'):
        data=json.loads(row['data']);identity=row['id']; reviews=documents[aliases.get(identity,identity)]
        defects.extend({'artifact_id':identity,**f} for f in data.get('findings',[]) if f['kind']=='DEFECT')
        related={folder for _,f in artifact_paths[identity] for folder in ancestors(f)}
        for folder in related:
            format_counts[folder][row['extension']]+=1
            if identity not in aliases:
                for dtype in {d['document_type'] for d in reviews}:type_counts[folder][dtype]+=1
                origin={d['origin'] for d in reviews}
                origins[folder][next(iter(origin)) if len(origin)==1 else 'unknown']+=1
            native_missing[folder]+=sum(f['kind']=='NOT_CHECKED' for f in data.get('findings',[]))
        file_records.append({'artifact_id':identity,'source_paths':[p for p,_ in artifact_paths[identity]],'status':data['status'],
            'format':row['extension'],'native_coverage':data.get('coverage',{}),'native_findings':data.get('findings',[]),
            'content_reviews':reviews,'representation':data.get('representation'),'metadata':data.get('metadata'),
            'alias_of':aliases.get(identity),'provenance':data.get('provenance'),
            'estimated_human_hours':None,'work_time_basis':'not inferable from save timestamps'})
    tasks=[]; folder_tasks=defaultdict(dict); role_tasks=defaultdict(list)
    for row in store.db.execute("SELECT id,data,assessment,review FROM tasks ORDER BY id"):
        task=json.loads(row["data"]); assessment=json.loads(row["assessment"]) if row["assessment"] else {"task_id":row["id"],"state":"unknown","end_to_end":False,"unknowns":["validation_pending"]}
        units=store.units([r["unit_id"] for r in task["inputs"]+task["outputs"]])
        required={u["artifact_id"] for u in units.values() if u["artifact_id"] in artifact_paths}
        paths=sorted({p for a in required for p,_ in artifact_paths[a]})
        contributing={f for a in required for _,f in artifact_paths[a]}
        review=json.loads(row['review'] or 'null') or {}
        entry={"task_id":row["id"],"role":task["role"],"kind":task["kind"],"title":task["title"],"origin":task.get("origin","unknown"),
               "high_value":review.get('checks',{}).get('high_value',{}).get('state')=='met',"source_paths":paths,"required_artifacts":sorted(required),
               "task":task,**assessment}
        tasks.append(entry); role_tasks[task["role"]].append(entry)
        for folder in {p for f in contributing for p in ancestors(f)}: folder_tasks[folder][row["id"]]=entry
    complete=bool(store.get("mining_complete")) and bool(store.get('discovery_complete'))
    records=[]
    for folder in folders:
        members=list(folder_tasks[folder].values()); local=[]; dependencies=set()
        for task in members:
            outside=[]
            for identity in task["required_artifacts"]:
                aliases=artifact_paths[identity]
                if not any(folder=="." or f==folder or f.startswith(folder+"/") for _,f in aliases): outside.extend(p for p,_ in aliases)
            dependencies.update(outside)
            local.append({**task,"state":"not_ready" if outside and task["state"]=="ready" else task["state"],
                          "end_to_end":task.get("end_to_end",False) and not outside})
        counts=recursive.get(folder,Counter()); missing=sum(counts[k] for k in ["pending","snapshotted","unsupported","unavailable","invalid"])
        total_units=len(all_units[folder]);reviewed_units=len(all_units[folder]&reviewed)
        scoped_complete=complete and not missing and not native_missing[folder] and total_units==reviewed_units
        linked=score(members,scoped_complete); standalone=score(local,scoped_complete)
        records.append({"folder":folder,"files":sum(counts.values()),"direct_files":sum(direct.get(folder,{}).values()),
                        "file_statuses":dict(counts),"unavailable_files":missing,"standalone":standalone,"linked":linked,
                        "readiness_score":standalone["readiness_score"],"linked_readiness_score":linked["readiness_score"],
                        "verified_capacity":linked["capacity"],"task_coverage":linked["task_coverage"],
                         "native_units":total_units,"reviewed_units":reviewed_units,
                         "content_coverage":round(100*reviewed_units/total_units,2) if total_units else None,
                         "native_checks_unavailable":native_missing[folder],"formats":dict(format_counts[folder]),
                         "document_types":dict(type_counts[folder]),"origins":dict(origins[folder]),
                         "format_count_basis":"distinct byte artifacts; logical aliases share content-review coverage",
                        "external_dependencies":sorted(dependencies),"roles":sorted({t["role"] for t in members}),
                        "task_ids":[t["task_id"] for t in members],"score_scope":linked["score_scope"]})
    phases={}
    for row in store.db.execute("SELECT phase,state,COUNT(*) AS n FROM jobs GROUP BY phase,state"):
        phases.setdefault(row["phase"],{})[row["state"]]=row["n"]
    source_unknown=store.db.execute("SELECT COUNT(*) FROM files WHERE status IN ('unavailable','unsupported','pending','snapshotted','invalid')").fetchone()[0]
    operational=store.get("status","incomplete")
    total_complete=complete and not source_unknown and not native_missing['.'] and all_units['.']<=reviewed
    summary={"schema":VERSION,"status":"failed" if defects else "passed" if operational=="completed" and total_complete and not any(t["state"]=="unknown" for t in tasks) else "incomplete",
             "completion_state":operational,"created_at":store.get("created_at"),"updated_at":now(),"protocol":store.get("protocol"),
             "scope":store.get("config"),"phases":phases,"folders":records,
             "roles":{role:score(role_tasks[role],total_complete) for role in ROLES},
             "origin_cohorts":{origin:score([t for t in tasks if t["origin"]==origin],total_complete)
                               for origin in ("corroborated_business","declared_simulation","unknown")},
             "tasks":score(tasks,total_complete),"counts":{"candidate_tasks":len(tasks),"confirmed_defects":len(defects),
                 "historical_tasks":sum(t["kind"]=="historical_employee_task" for t in tasks),
                 "reconstructed_tasks":sum(t["kind"]=="reconstructed_analysis" for t in tasks),
                 "ready_high_value_tasks":sum(t["state"]=="ready" and t["high_value"] for t in tasks)},
             "source_freshness":store.get("source_freshness",{"status":"not_checked"}),
             "provider_blocked":store.get('provider_blocked'),
             "defects":defects,"native_unit_coverage":{"total":len(all_units['.']),'reviewed':len(all_units['.']&reviewed)},
             "capacity_definition":"independently reviewed usable input/output task sets, deduplicated by business outcome and canonical evidence",
             "limitations":["Readiness describes assessed eligible tasks, not authenticity probability or total economic value.",
                 "Unknown origin is preserved; metadata does not prove human authorship.",
                 "Task counts are enumerated cases within the reported content coverage; no extrapolation.",
                 "Analytical completion does not establish an external business transaction."]}
    write_json(store.run/"score-summary.json",summary)
    csv_file(store.run/"score-summary.csv",records,["folder","files","direct_files","unavailable_files","readiness_score","linked_readiness_score","verified_capacity","task_coverage","native_units","reviewed_units","content_coverage","score_scope"])
    with (store.run/'files.jsonl').open('w') as stream:
        for record in file_records:stream.write(json.dumps(record,ensure_ascii=False)+'\n')
    with (store.run/"tasks.jsonl").open("w") as stream:
        for task in tasks: stream.write(json.dumps(task,ensure_ascii=False)+"\n")
    with (store.run/"provenance.jsonl").open("w") as stream:
        for row in store.db.execute("SELECT id,data FROM artifacts"):
            data=json.loads(row["data"])
            stream.write(json.dumps({"artifact_id":row["id"],"paths":data.get("paths"),"metadata":data.get("metadata"),"provenance":data.get("provenance"),"origin_signals":data.get("origin_signals")},ensure_ascii=False)+"\n")
    with (store.run/"connections.jsonl").open("w") as stream:
        for row in store.db.execute("SELECT data FROM links"): stream.write(row[0]+"\n")
    render_html(store.run,summary,tasks)
    return {"schema":VERSION,"status":summary["status"],"completion_state":operational,"directory":str(store.run),
            "report":str(store.run/"index.html"),"folders":len(records),"tasks":summary["tasks"],"phases":phases}


def render_html(run,summary,tasks):
    E=lambda x:html.escape(str(x))
    cell=lambda v:"—" if v is None else E(v)
    rows="".join('<tr data-search="'+E(r["folder"]+' '+" ".join(r["roles"])+ ' '+ ' '.join(r['formats'])+' '+' '.join(r['origins'])+' '+r['score_scope'])+'"><td><a href="folders/'+str(i)+'.html">'+E(r["folder"])+"</a></td>"+
                 "".join("<td>"+cell(r[k])+"</td>" for k in ["files","readiness_score","linked_readiness_score","verified_capacity","task_coverage","content_coverage","score_scope"])+"</tr>" for i,r in enumerate(summary["folders"]))
    style="body{font:15px/1.5 system-ui;color:#193247;background:#f5f7fa;margin:0}main{max-width:1250px;margin:auto;padding:28px}table{border-collapse:collapse;width:100%;background:white}th,td{text-align:left;padding:10px;border-bottom:1px solid #d9e1e8;vertical-align:top}th{background:#e9f1f8}a{color:#075b9c}input{padding:10px;width:95%;margin:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:18px}small{color:#5a6c7b}.scroll{overflow:auto}"
    def page(title,body): return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+E(title)+'</title><style>'+style+'</style></head><body><main>'+body+'</main></body></html>'
    body='<h1>Artifact task readiness</h1><p>Completion: '+E(summary["completion_state"])+'. Scores describe assessed tasks. Unknown evidence remains visible.</p><p><a href="score-summary.csv">CSV</a> · <a href="score-summary.json">Full summary</a> · <a href="tasks.jsonl">Task records</a> · <a href="provenance.jsonl">Provenance</a> · <a href="connections.jsonl">Connections</a></p><input id="filter" placeholder="Filter by folder or employee role" aria-label="Filter folders"><div class="scroll"><table><thead><tr><th>Folder</th><th>Files</th><th>Standalone /100</th><th>Linked /100</th><th>Ready tasks</th><th>Task coverage %</th><th>Scope</th></tr></thead><tbody>'+rows+'</tbody></table></div><h2>Employee roles</h2><pre>'+E(json.dumps(summary["roles"],ensure_ascii=False,indent=2))+'</pre><h2>Work remaining</h2><pre>'+E(json.dumps(summary["phases"],indent=2))+'</pre><script>document.getElementById("filter").addEventListener("input",e=>{const q=e.target.value.toLowerCase();document.querySelectorAll("tr[data-search]").forEach(r=>r.hidden=!r.dataset.search.toLowerCase().includes(q))})</script>'
    body=body.replace('Filter by folder or employee role','Filter folder, role, format, origin or scope').replace('<th>Scope</th>','<th>Content coverage %</th><th>Scope</th>').replace('<a href="provenance.jsonl">Provenance</a>','<a href="provenance.jsonl">Provenance</a> · <a href="files.jsonl">File evidence</a>')
    (run/"index.html").write_text(page("Artifact task readiness",body))
    directory=run/"folders"; directory.mkdir(exist_ok=True)
    indexed={t["task_id"]:t for t in tasks}
    for i,record in enumerate(summary["folders"]):
        detail='<p><a href="../index.html">All folders</a></p><h1>'+E(record["folder"])+'</h1><pre>'+E(json.dumps(record,ensure_ascii=False,indent=2))+'</pre>'
        for key in record["task_ids"]:
            task=indexed[key]
            detail+='<h2>'+E(task["title"])+'</h2><p>'+E(task["role"]+' · '+task["kind"]+' · '+task["state"])+'</p><pre>'+E(json.dumps(task,ensure_ascii=False,indent=2))+'</pre>'
        (directory/(str(i)+".html")).write_text(page(record["folder"],detail))
