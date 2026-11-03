"""Role datasets with exact input selections and physically separate answer evidence."""
import hashlib
import json
import shutil
from collections import defaultdict
from pathlib import Path

from . import VERSION
from .contracts import assess, digest, require, resolve
from .store import Store, now, write_json


def selected_inputs(store, refs):
    """Preserve source offsets while removing all text outside selected intervals."""
    units=store.units([r["unit_id"] for r in refs]); by_unit=defaultdict(list)
    for ref in refs:
        unit, text, (_,start,end)=resolve(ref,units)
        by_unit[unit["id"]].append((start,end))
    result=[]; images=[]
    for key,intervals in by_unit.items():
        unit=units[key]; text=unit.get("text",""); masked=[" "]*len(text)
        for start,end in intervals: masked[start:end]=text[start:end]
        record={k:unit[k] for k in ("id","artifact_id","canonical_id","locator","available_at","availability_basis") if k in unit}
        record.update(text="".join(masked),selected_ranges=[list(r) for r in sorted(set(intervals))],status="inspected")
        if unit.get("image"):
            require(any(a==0 and b==len(text) for a,b in intervals),"partial image inputs require a separately reviewed redaction")
            path=unit["image"]
            if path not in images: images.append(path)
            record["attached_image_number"]=images.index(path)+1
        result.append(record)
    return {"units":result,"selection_policy":"unselected text replaced with spaces to preserve source offsets"},images


def solver_task(task):
    return {k:task[k] for k in ("title","role","instruction","acceptance_criteria","temporal_cutoff","output_format")}


def verify_execution(store, review, task):
    require(review and review.get("execution_id"),"independent execution is missing")
    folder=store.run/"executions"/review["execution_id"]
    receipt=json.loads((folder/"receipt.json").read_text())
    response=json.loads((folder/"response.json").read_text())
    prompt=(folder/"prompt.txt").read_text()
    require(receipt.get("phase")=="validate" and receipt.get("execution_id")==review["execution_id"],"wrong review execution")
    require(receipt==review.get("execution"),"review execution receipt was modified")
    require(digest(prompt)==receipt["prompt_sha256"] and digest(response)==receipt["response_sha256"],"review content hash mismatch")
    payload=json.loads(prompt.split("\nEVIDENCE PACKET:\n",1)[1])
    require(payload["task"]==task,"independent reviewer did not inspect this task version")
    require(all(review.get(k)==v for k,v in response.items()),"review differs from actual provider response")
    for path,expected in receipt.get("image_sha256",{}).items():
        require(hashlib.sha256(Path(path).read_bytes()).hexdigest()==expected,"review image changed")
    live=store.units([u["id"] for u in payload["evidence"]["units"]])
    for saved in payload["evidence"]["units"]:
        saved=dict(saved); saved.pop("attached_image_number",None)
        require(saved==live.get(saved["id"]),"review evidence changed after independent execution")


def export_dataset(directory, output, role=None):
    from .integrity import verify_seal
    from .contracts import ROLES
    store=Store(directory)
    try:
        with store.lock():
            verify_seal(store)
            require(role is None or role in ROLES,"unknown employee role")
            output=Path(output).expanduser().resolve()
            require(not output.is_relative_to(Path(store.get("config")["root"])),"dataset output must be outside the source tree")
            require(not output.exists(),"dataset output must be a new directory")
            all_tasks=[dict(r) for r in store.db.execute("SELECT * FROM tasks ORDER BY id")]
            parent={r["id"]:r["id"] for r in all_tasks}; seen={}
            def root(key):
                while parent[key]!=key: parent[key]=parent[parent[key]];key=parent[key]
                return key
            prepared=[]
            for row in all_tasks:
                task=json.loads(row["data"])
                units=store.units([r["unit_id"] for r in task["inputs"]+task["outputs"]])
                keys=["case:"+task.get("component",task["business_case"])]
                keys += ["unit:"+u.get("canonical_id",u["id"]) for u in units.values()]
                for key in keys:
                    if key in seen: parent[root(row["id"])]=root(seen[key])
                    else: seen[key]=row["id"]
                prepared.append((row,task,units))
            groups=defaultdict(list)
            for key in parent: groups[root(key)].append(key)
            group_ids={key:digest(sorted(values)) for values in groups.values() for key in values}
            output.mkdir(parents=True)
            exported=[]; verified_blobs=set()
            for row,task,units in prepared:
                assessment=json.loads(row["assessment"] or "null")
                if not assessment or assessment["state"]!="ready" or role and task["role"]!=role: continue
                review=json.loads(row["review"] or "null")
                verify_execution(store,review,task)
                ids=[r["unit_id"] for node in task["stages"] for r in node["evidence"]]
                ids += [r["unit_id"] for edge in task["edges"] for r in edge["evidence"]]
                units.update(store.units(ids))
                require(assess(task,units,review)==assessment,"stored readiness does not reproduce")
                for artifact_id in {u["artifact_id"] for u in units.values()}:
                    if artifact_id in verified_blobs: continue
                    artifact=json.loads(store.db.execute("SELECT data FROM artifacts WHERE id=?",(artifact_id,)).fetchone()[0])
                    h=hashlib.sha256()
                    with Path(artifact["blob"]).open("rb") as stream:
                        for block in iter(lambda:stream.read(1024*1024),b""):h.update(block)
                    require(h.hexdigest()==artifact["sha256"],"retained source bytes changed")
                    verified_blobs.add(artifact_id)
                group=group_ids[row["id"]]; bucket=int(group[:8],16)%100
                split="train" if bucket<80 else "validation" if bucket<90 else "test"
                folder=output/task["role"]/split/row["id"]; folder.mkdir(parents=True)
                inputs,images=selected_inputs(store,task["inputs"])
                image_dir=folder/"inputs"; image_dir.mkdir()
                for n,path in enumerate(images,1): shutil.copyfile(path,image_dir/(f"image-{n}.png"))
                write_json(image_dir/"evidence.json",inputs)
                write_json(folder/"task.json",{"schema":VERSION,"task_id":row["id"],"kind":task["kind"],"origin":task["origin"],
                    **solver_task(task),"inputs":"inputs/evidence.json","images":[f"inputs/image-{i}.png" for i in range(1,len(images)+1)]})
                evaluator=output/"evaluator-only"/row["id"]; evaluator.mkdir(parents=True)
                answers=[]
                for ref in task["outputs"]:
                    unit,text,_=resolve(ref,units)
                    record={"reference":ref,"text":text,"locator":unit["locator"],"artifact_id":unit["artifact_id"]}
                    if unit.get("image"):
                        name="answer-"+unit["id"]+".png";shutil.copyfile(unit["image"],evaluator/name);record["image"]=name
                    answers.append(record)
                write_json(evaluator/"acceptance.json",{"task":task,"outputs":answers,"review":review,"assessment":assessment})
                exported.append({"task_id":row["id"],"role":task["role"],"kind":task["kind"],"origin":task["origin"],
                    "split":split,"leakage_group":group,"task_file":str((folder/"task.json").relative_to(output))})
            role_counts={name:sum(t['role']==name for t in exported) for name in ROLES if role is None or name==role}
            for name,count in role_counts.items():
                write_json(output/name/'dataset.json',{'role':name,'verified_tasks':count,
                    'tasks':[t for t in exported if t['role']==name],
                    'state':'available' if count else 'no_verified_tasks_in_this_export',
                    'search_complete':bool(store.get('mining_complete'))})
            write_json(output/"dataset.json",{"schema":VERSION,"created_at":now(),"source_run":str(store.run),"protocol":store.get("protocol"),
                "role_counts":role_counts,
                "tasks":exported,"split_policy":"sha256 of connected case/shared evidence group; 80/10/10 hash buckets, not a guaranteed size ratio",
                "input_policy":"exact selected evidence and safe full-page images; no unfiltered source-file copies",
                "answer_policy":"evaluator-only must not be supplied to the task performer",
                "coverage":"verified tasks from this run; does not imply complete corpus mining"})
            manifest={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob('*')) if p.is_file()}
            write_json(output/"manifest.json",{"files":manifest})
            return {"status":"passed","directory":str(output),"exported_tasks":len(exported),"roles":sorted({t["role"] for t in exported})}
    finally:store.close()
