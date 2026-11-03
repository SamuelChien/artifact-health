"""Full-collection evaluation with durable, version-bound phase checkpoints."""
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from . import VERSION, WORKFLOW_VERSION
from .contracts import assess, digest, normalized, pair_fingerprint, resolve, task_identity
from .native import SUPPORTED, clean_env
from .provider import CodexProvider, ProviderUnavailable
from .store import Store, now, write_json

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOLDERS = ("Facilities","Finance","Incorporation","Invoices","Procurement","Sales","email")
SKIP = {".git",".hg",".svn",".venv","node_modules","__pycache__"}
BUNDLES = {".pages",".numbers",".key"}
LOOSE_BUSINESS_FORMATS = SUPPORTED - {'.md','.txt','.json','.html','.htm','.eml'}


def protocol():
    from importlib import metadata
    import platform
    import shutil
    files = sorted((ROOT/"artifact_engine").glob("*.py"))+[ROOT/'artifact_health.py',ROOT/'scripts/content_signals.py',ROOT/'requirements.lock',ROOT/'references/v3-rubric.json']
    dependencies={}
    for name in ('pypdf','Pillow','pillow-heif','openpyxl','python-docx','jsonschema','reportlab','charset-normalizer','c2pa-python','pyHanko'):
        try: dependencies[name]=metadata.version(name)
        except metadata.PackageNotFoundError: dependencies[name]=None
    native_versions={}
    for name,flag in (('pdftoppm','-v'),('soffice','--version'),('tesseract','--version'),('codex','--version')):
        executable=shutil.which(name)
        try:
            result=subprocess.run([executable,flag],capture_output=True,text=True,timeout=15,env=clean_env()) if executable else None
            native_versions[name]=(result.stdout+result.stderr).splitlines()[0] if result else None
        except (OSError,subprocess.SubprocessError,IndexError):native_versions[name]=None
    ocr={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'.venv/share/tessdata').glob('*.traineddata'))}
    return {"schema":VERSION,"workflow_schema":WORKFLOW_VERSION,
            "code_sha256":digest([[str(p.relative_to(ROOT)),hashlib.sha256(p.read_bytes()).hexdigest()] for p in files]),
            "python":platform.python_version(),"dependencies":dependencies,
            "native_versions":native_versions,"ocr_data":ocr,
            "scoring":"ready / (ready + not_ready); capacity and coverage remain separate"}


class NativePool:
    def __init__(self, workers, run):
        self.workers, self.run = workers, Path(run)
        self.local = threading.local(); self.processes=[]; self.lock=threading.Lock()

    def query(self, request, timeout=300):
        process=getattr(self.local,"process",None)
        if process is None or process.poll() is not None:
            log=(self.run/"native-worker.log").open("ab")
            temporary=self.run/'native-temp';temporary.mkdir(exist_ok=True)
            process=subprocess.Popen([sys.executable,"-E","-B","-m","artifact_engine.native"],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=log,cwd=ROOT,env={**clean_env(),'TMPDIR':str(temporary)},start_new_session=True)
            log.close(); self.local.process=process
            with self.lock: self.processes.append(process)
        try:
            process.stdin.write((json.dumps(request)+"\n").encode()); process.stdin.flush()
            deadline=time.monotonic()+timeout; response=bytearray()
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([process.stdout],[],[],remaining)[0]: raise TimeoutError("native extraction deadline exceeded")
                chunk=os.read(process.stdout.fileno(),1024*1024)
                if not chunk: raise RuntimeError("native extractor ended without a result")
                response.extend(chunk)
                if response.endswith(b"\n"): return json.loads(response)
        except BaseException:
            self.stop(process); self.local.process=None; raise

    @staticmethod
    def stop(process):
        if process.poll() is None:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        process.wait(timeout=10)
        for stream in (process.stdin,process.stdout):
            if stream: stream.close()

    def close(self):
        for process in self.processes: self.stop(process)

    def map(self, function, items):
        iterator=iter(items)
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            pending=set()
            for _ in range(self.workers*2):
                item=next(iterator,None)
                if item is not None: pending.add(executor.submit(function,item))
            while pending:
                done,pending=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    yield future.result()
                    item=next(iterator,None)
                    if item is not None: pending.add(executor.submit(function,item))


def progress(store, event, **fields):
    store.event(event,**fields)
    print(json.dumps({"event":event,**fields},ensure_ascii=False),file=sys.stderr,flush=True)


def discover(store):
    config=store.get("config"); root=Path(config["root"])
    if store.get("discovery_complete"): return
    errors=[]; count=0
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO folders VALUES ('.')")
        for selected in config["folders"]:
            base=root/selected
            store.db.execute('INSERT OR IGNORE INTO folders VALUES (?)',(selected,))
            for parent,dirs,files in os.walk(base,followlinks=False,onerror=lambda e:errors.append(str(e))):
                current=Path(parent); relative=str(current.relative_to(root))
                store.db.execute("INSERT OR IGNORE INTO folders VALUES (?)",(relative,))
                bundles=[name for name in dirs if Path(name).suffix.lower() in BUNDLES]
                symlinks=[name for name in dirs if (current/name).is_symlink()]
                dirs[:]=sorted(d for d in dirs if d not in SKIP and d not in bundles and d not in symlinks)
                for name in sorted(files+bundles+symlinks):
                    path=current/name; rel=str(path.relative_to(root)); ext=path.suffix.lower()
                    status="pending"; detail={}
                    if path.is_symlink(): status="excluded"; detail={"reason":"symlink_not_followed"}
                    elif name.startswith("~$") or name in {".DS_Store","Thumbs.db","desktop.ini"} or name.startswith("._"):
                        status="non_material"; detail={"reason":"temporary_or_system_file"}
                    elif ext not in SUPPORTED: status="unsupported"; detail={"reason":"native_format_not_supported"}
                    try: size=path.stat().st_size
                    except OSError as exc: size=None; status="unavailable"; detail={"reason":str(exc)}
                    store.db.execute("INSERT OR IGNORE INTO files VALUES (?,?,?,?,?,?,?)",
                        (rel,relative,None,ext,size,status,json.dumps(detail)))
                    count+=1
        # Loose business documents at the common root belong to its own direct scope.
        for path in sorted(root.iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in LOOSE_BUSINESS_FORMATS and not path.name.startswith('.'):
                if path.name in {"measurement.json","score-summary.json","artifact-health.json"}: continue
                store.db.execute("INSERT OR IGNORE INTO files VALUES (?,?,?,?,?,?,?)",
                    (path.name,".",None,path.suffix.lower(),path.stat().st_size,"pending","{}")); count+=1
    store.set("discovery_errors",errors); store.set("discovery_complete",not errors)
    progress(store,"discovery_complete",files=count,errors=len(errors))


def bundle_copy(path, destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,"w",compression=zipfile.ZIP_STORED) as archive:
        for member in sorted(path.rglob("*")):
            if member.is_symlink(): raise ValueError("native document bundle contains a symlink")
            if member.is_file():
                info=zipfile.ZipInfo(str(member.relative_to(path))); info.compress_type=zipfile.ZIP_STORED
                archive.writestr(info,member.read_bytes())
    return destination


def extract_all(store):
    config=store.get("config"); source=Path(config["root"])
    # Snapshot once per path; repeated bytes reuse one native extraction.
    for n,row in enumerate(store.db.execute("SELECT * FROM files WHERE status IN ('pending','unsupported') AND sha256 IS NULL ORDER BY path").fetchall(),1):
        path=source/row["path"]
        try:
            if path.is_dir(): path=bundle_copy(path,store.run/"bundle-copies"/(digest(row["path"])+row["extension"]))
            sha,blob,size=store.snapshot(path)
            identity=digest([sha,row["extension"]])
            detail={"artifact_id":identity,"blob":str(blob)}
            with store.db:
                store.db.execute("UPDATE files SET sha256=?,bytes=?,status=?,detail=? WHERE path=?",
                    (sha,size,"unsupported" if row["status"]=="unsupported" else "snapshotted",json.dumps(detail),row["path"]))
            if row["status"]!="unsupported":
                store.job("extract",{"artifact_id":identity,"sha256":sha,"extension":row["extension"],"blob":str(blob)})
        except (OSError,ValueError) as exc:
            with store.db: store.db.execute("UPDATE files SET status='unavailable',detail=? WHERE path=?",(json.dumps({"reason":str(exc)}),row["path"]))
        if n%500==0: progress(store,"snapshot_progress",files=n)
    pool=NativePool(config["workers"],store.run)
    native_options={"render":config.get("render",True),"tessdata":config.get("tessdata"),
                    "ocr_languages":config.get("ocr_languages","eng")}
    def process(row):
        payload=json.loads(row["payload"]); work=store.run/"native"/payload["artifact_id"]
        try:
            result=pool.query({"path":payload["blob"],"artifact_id":payload["artifact_id"],"work":str(work),"options":native_options},timeout=config["file_timeout"])
            return row,payload,result,None
        except (OSError,ValueError,RuntimeError,TimeoutError) as exc:
            return row,payload,{"status":"unavailable","units":[],"findings":[{"code":"EXTRACTION_FAILED","kind":"NOT_CHECKED","detail":str(exc)}]},str(exc)
    try:
        while jobs:=store.queued("extract"):
            for n,(row,payload,result,error) in enumerate(pool.map(process,jobs),1):
                identity=payload["artifact_id"]
                paths=[r[0] for r in store.db.execute("SELECT path FROM files WHERE sha256=? AND extension=?",(payload["sha256"],payload["extension"]))]
                result.update(id=identity,sha256=payload["sha256"],extension=payload["extension"],blob=payload["blob"],paths=paths)
                availability=[json.loads(r[0]).get("available_at") for r in store.db.execute("SELECT detail FROM files WHERE sha256=?",(payload["sha256"],))]
                available=min((d for d in availability if d),default=None)
                if available:
                    for unit in result.get("units",[]):
                        if not unit.get("available_at"): unit.update(available_at=available,availability_basis="containing_email_native_date")
                store.add_artifact(identity,payload["sha256"],payload["extension"],result)
                with store.db:
                    store.db.execute("UPDATE files SET status=? WHERE sha256=? AND extension=?",(result["status"],payload["sha256"],payload["extension"]))
                for attachment in result.get("attachments",[]):
                    ext=Path(attachment["path"]).suffix.lower()
                    child_id=digest([attachment["sha256"],ext])
                    sha,blob,size=store.snapshot(attachment["path"])
                    for parent in paths:
                        virtual=parent+"/@attachment/"+str(attachment["part"])+"-"+(Path(attachment.get("name") or "unnamed").name)
                        folder=str(Path(parent).parent)
                        detail={"artifact_id":child_id,"blob":str(blob),"attachment_of":parent,"available_at":attachment.get("available_at")}
                        existing=store.db.execute("SELECT status FROM artifacts WHERE id=?",(child_id,)).fetchone()
                        child_status=existing[0] if existing else "snapshotted" if ext in SUPPORTED else "unsupported"
                        with store.db: store.db.execute("INSERT OR IGNORE INTO files VALUES (?,?,?,?,?,?,?)",
                            (virtual,folder,sha,ext,size,child_status,json.dumps(detail)))
                    if attachment.get("available_at"):
                        for saved in store.db.execute("SELECT id,data FROM units WHERE artifact_id=?",(child_id,)).fetchall():
                            unit=json.loads(saved["data"])
                            if not unit.get("available_at") or attachment["available_at"] < unit["available_at"]:
                                unit.update(available_at=attachment["available_at"],availability_basis="containing_email_native_date")
                                with store.db: store.db.execute("UPDATE units SET data=? WHERE id=?",(json.dumps(unit),saved["id"]))
                    if ext in SUPPORTED:
                        store.job("extract",{"artifact_id":child_id,"sha256":sha,"extension":ext,"blob":str(blob)})
                if result['status']!='inspected' and not error:
                    error='native evidence unavailable: '+json.dumps(result.get('findings',[]),ensure_ascii=False)[:2000]
                store.finish(row["id"],{"artifact_id":identity,"status":result["status"]},error)
                if n%25==0: progress(store,"native_progress",completed=n,total=len(jobs))
    finally: pool.close()
    store.set("native_complete",not store.db.execute("SELECT 1 FROM files WHERE status IN ('pending','snapshotted') LIMIT 1").fetchone())


def chunks(units, max_chars=55000, max_images=6):
    group=[]; size=0; images=set()
    for unit in units:
        image=unit.get("image"); length=len(json.dumps(unit,ensure_ascii=False))
        if group and (size+length>max_chars or image and image not in images and len(images)>=max_images):
            yield group; group=[]; size=0; images=set()
        group.append(unit); size+=length
        if image: images.add(image)
    if group: yield group


def packet(store, unit_ids):
    units=store.units(unit_ids)
    images=list(dict.fromkeys(u["image"] for u in units.values() if u.get("image")))
    prepared=[]
    for unit in units.values():
        copy=dict(unit)
        if copy.get("image"): copy["attached_image_number"]=images.index(copy["image"])+1
        prepared.append(copy)
    artifacts=[]
    for identity in sorted({u["artifact_id"] for u in units.values()}):
        row=store.db.execute("SELECT data FROM artifacts WHERE id=?",(identity,)).fetchone()
        if row:
            data=json.loads(row[0]); artifacts.append({k:data.get(k) for k in ("id","paths","metadata","provenance","findings","representation")})
    return {"artifacts":artifacts,"units":prepared},images


def classify_all(store, provider, max_jobs=None):
    from .reconcile import reconcile_aliases
    reconcile_aliases(store)
    if not store.get("classification_queued"):
        seen=set(); pending_units=[];completed_units=set()
        for row in store.db.execute("SELECT result FROM jobs WHERE phase='classify' AND state='done'"):
            completed_units.update(u['unit_id'] for u in json.loads(row[0])['unit_reviews'])
        for row in store.db.execute("SELECT * FROM artifacts WHERE status='inspected' ORDER BY id").fetchall():
            if store.db.execute('SELECT 1 FROM aliases WHERE artifact_id=?',(row['id'],)).fetchone():continue
            data=json.loads(row["data"]); logical=data.get("logical_id",row["id"])
            if logical in seen: continue
            seen.add(logical)
            units=[json.loads(r[0]) for r in store.db.execute("SELECT data FROM units WHERE artifact_id=? ORDER BY rowid",(row["id"],))]
            if data.get("administrative_index"):
                job=store.job("administrative",{"artifact_id":row["id"],"reason":"export index; review belongs to linked source records"})
                store.finish(job,{"disposition":"structural_export_index","verification":data.get('index_verification'),"semantic_content":"reviewed in linked source records"}); continue
            pending_units.extend(u for u in units if u['id'] not in completed_units)
            # Batch small documents without dropping units or merging document identities.
            # Flush bounded windows so large archives do not accumulate their full text in RAM.
            if sum(len(u.get('text',''))+600 for u in pending_units)>=90000:
                for group in chunks(pending_units):store.job('classify',{'unit_ids':[u['id'] for u in group]})
                pending_units=[]
        for group in chunks(pending_units):store.job('classify',{'unit_ids':[u['id'] for u in group]})
        store.set("classification_queued",True)
    for n,job in enumerate(store.queued("classify")):
        if store.get('provider_blocked'):break
        if max_jobs is not None and n>=max_jobs: break
        payload=json.loads(job["payload"]); evidence,images=packet(store,payload["unit_ids"])
        def validate_classification(result):
            if {x["unit_id"] for x in result["unit_reviews"]}!=set(payload["unit_ids"]):
                raise ValueError("model did not account for every supplied evidence unit")
            units={u["id"]:u for u in evidence["units"]}
            expected={u["artifact_id"] for u in units.values()}
            if {d["artifact_id"] for d in result["documents"]}!=expected:
                raise ValueError("classification omitted or invented an artifact")
            for doc in result["documents"]:
                for entity in doc["entities"]:
                    unit,text,_=resolve(entity["evidence"],units)
                    if unit["artifact_id"]!=doc["artifact_id"] or normalized(entity["value"]) not in normalized(text):
                        raise ValueError("entity identifier is not grounded in the cited source text")
                for finding in doc["findings"]:
                    if finding['kind']=='DEFECT' and finding['attribution']!='source':
                        raise ValueError('inspection or context limitations cannot be source defects')
                    for ref in finding["evidence"]: resolve(ref,units)
        try:
            result,receipt=provider.call("classify",evidence,images,validate=validate_classification)
            result["execution"]=receipt
            store.finish(job["id"],result)
        except ProviderUnavailable as exc:
            store.finish(job['id'],error=str(exc));store.set('provider_blocked',str(exc));break
        except (ValueError,RuntimeError,OSError) as exc:
            store.finish(job["id"],error=str(exc))
        progress(store,"classification_progress",completed=n+1)
        if (n+1)%20==0:
            from .reports import refresh
            refresh(store)
    remaining=store.db.execute("SELECT COUNT(*) FROM jobs WHERE phase='classify' AND state!='done'").fetchone()[0]
    store.set("classification_complete",remaining==0)


def queue_cases(store):
    if store.get("cases_queued"): return
    if store.db.execute("SELECT 1 FROM jobs WHERE phase='classify' AND state='pending' LIMIT 1").fetchone():return
    artifacts={r["id"]:json.loads(r["data"]) for r in store.db.execute("SELECT id,data FROM artifacts WHERE status='inspected'")}
    parent={key:key for key in artifacts}
    def root(key):
        while parent[key]!=key: parent[key]=parent[parent[key]]; key=parent[key]
        return key
    def join(a,b):
        if a in parent and b in parent: parent[root(b)]=root(a)
    identifiers=defaultdict(list); metadata=defaultdict(list); message_ids={}; logical_ids={}
    for key,data in artifacts.items():
        logical=data.get("logical_id",key)
        if logical in logical_ids: join(key,logical_ids[logical])
        else: logical_ids[logical]=key
        mid=data.get("metadata",{}).get("asserted",{}).get("Message-ID")
        if mid:
            if mid in message_ids and logical==artifacts[message_ids[mid]].get("logical_id"): join(key,message_ids[mid])
            message_ids[mid]=key
    for row in store.db.execute('SELECT * FROM aliases'):join(row['artifact_id'],row['canonical_artifact'])
    for key,data in artifacts.items():
        for link in data.get("links",[]):
            if link.get("message_id") in message_ids: join(key,message_ids[link["message_id"]])
        for attachment in data.get("attachments",[]): join(key,digest([attachment["sha256"],Path(attachment["path"]).suffix.lower()]))
    for row in store.db.execute("SELECT result FROM jobs WHERE phase='classify' AND state='done'"):
        result=json.loads(row[0])
        for doc in result["documents"]:
            metadata[doc["artifact_id"]].append(doc)
            for entity in doc["entities"]:
                if entity["kind"]!="party": identifiers[(entity["kind"],normalized(entity["value"]))].append((doc["artifact_id"],entity))
    for (kind,value),members in identifiers.items():
        for a,b in zip(members,members[1:]):
            join(a[0],b[0])
            link={"kind":"shared_identifier","identifier_kind":kind,"value":value,"source":a[0],"target":b[0],
                  "evidence":[a[1]["evidence"],b[1]["evidence"]],"status":"candidate_business_link"}
            with store.db: store.db.execute("INSERT OR IGNORE INTO links VALUES (?,?)",(digest(link),json.dumps(link)))
    groups=defaultdict(list)
    for identity in artifacts: groups[root(identity)].append(identity)
    small_units=[];small_components={};small_metadata=[]
    def flush_small():
        for group in chunks(small_units,max_chars=100000,max_images=12):
            ids={u['artifact_id'] for u in group}
            store.job('mine',{'unit_ids':[u['id'] for u in group],'component':digest(sorted(ids)),
                'component_artifacts':sorted(ids),'component_parts':1,
                'unit_components':{u['id']:small_components[u['id']] for u in group},
                'classifications':[m for m in small_metadata if m['artifact_id'] in ids]})
        small_units.clear();small_components.clear();small_metadata.clear()
    for members in groups.values():
        units=[]; seen_units=set()
        for identity in sorted(members):
            if artifacts[identity].get("administrative_index"): continue
            for row in store.db.execute("SELECT data FROM units WHERE artifact_id=? ORDER BY rowid",(identity,)):
                unit=json.loads(row[0]); canonical=unit.get("canonical_id",unit["id"])
                if canonical not in seen_units: units.append(unit); seen_units.add(canonical)
        if units and not any(u.get('image') for u in units) and sum(len(json.dumps(u,ensure_ascii=False)) for u in units)<10000:
            small_units.extend(units);component=digest(sorted(members))
            small_components.update({u['id']:component for u in units});small_metadata.extend(m for key in members for m in metadata[key])
            if sum(len(u.get('text',''))+600 for u in small_units)>=90000:flush_small()
            continue
        # Every chunk is inspected; catalogue retrieval expands cross-chunk task evidence.
        parts=list(chunks(units,max_chars=100000,max_images=12))
        if len(parts)>1:
            catalog=[{'id':u['id'],'artifact_id':u['artifact_id'],'locator':u['locator'],'text':u['text'][:250],
                      'available_at':u.get('available_at')} for u in units]
            for selection in chunks(catalog,max_chars=120000):
                store.job('connect',{'catalogue':selection,'component':digest(sorted(members)),
                    'classifications':[{'artifact_id':key,'document_type':m['document_type'],'summary':m['summary']} for key in members for m in metadata[key]]})
        for part in parts:
            store.job("mine",{"unit_ids":[u["id"] for u in part],"component":digest(sorted(members)),
                              "component_artifacts":members,"component_parts":len(parts),
                              "classifications":[m for key in members for m in metadata[key]]})
    flush_small()
    store.set("cases_queued",True)


def mine_all(store,provider,max_jobs=None):
    queue_cases(store)
    for n,job in enumerate(store.queued('connect')):
        if max_jobs is not None and n>=max_jobs:break
        payload=json.loads(job['payload']);allowed={u['id'] for u in payload['catalogue']}
        def validate_retrieval(result):
            if any(not b['unit_ids'] or not set(b['unit_ids'])<=allowed for b in result['bundles']):
                raise ValueError('retrieval must use nonempty supplied unit IDs')
        try:
            result,receipt=provider.call('connect',payload,validate=validate_retrieval)
            for bundle in result['bundles']:
                units=list(store.units(bundle['unit_ids']).values())
                selected=list(chunks(units,max_chars=160000,max_images=16))
                for group in selected:
                    store.job('mine',{'unit_ids':[u['id'] for u in group],'component':payload['component'],
                        'component_artifacts':sorted({u['artifact_id'] for u in group}),'component_parts':len(selected),
                        'classifications':payload['classifications'],'retrieval_reason':bundle['reason']})
            result['execution']=receipt;store.finish(job['id'],result)
        except ProviderUnavailable as exc:
            store.finish(job['id'],error=str(exc));store.set('provider_blocked',str(exc));break
        except (ValueError,RuntimeError,OSError) as exc:store.finish(job['id'],error=str(exc))
        progress(store,'connection_progress',completed=n+1)
    for n,job in enumerate(store.queued("mine")):
        if store.get('provider_blocked'):break
        if max_jobs is not None and n>=max_jobs: break
        payload=json.loads(job["payload"]); evidence,images=packet(store,payload["unit_ids"])
        evidence.update(classifications=payload["classifications"],component_parts=payload["component_parts"])
        try:
            units={u["id"]:u for u in evidence["units"]}
            def validate_candidates(result):
                for task in result['tasks']:
                    task_identity(task)
                    if not task['inputs']:raise ValueError('mined task is not grounded in inputs')
                    refs=task['inputs']+task['outputs']
                    refs += [r for n in task['stages'] for r in n['evidence']]
                    refs += [r for e in task['edges'] for r in e['evidence']]
                    for check in task['checks']:refs+=check['sources']+([check['target']] if check['target'] else [])
                    for ref in refs:resolve(ref,units)
            result,receipt=provider.call("mine",evidence,images,validate=validate_candidates)
            for task in result["tasks"]:
                for ref in task["inputs"]+task["outputs"]: resolve(ref,units)
                if not task["inputs"]: raise ValueError("mined task is not grounded in inputs")
                task.update(proposer_execution_id=receipt["execution_id"],component=payload["component"],
                            component_parts=payload["component_parts"])
                if payload.get('unit_components'):
                    components={payload['unit_components'][r['unit_id']] for r in task['inputs']+task['outputs']}
                    task['component']=next(iter(components)) if len(components)==1 else digest(sorted(components))
                identity=task_identity(task); pair=pair_fingerprint(task,units)
                duplicate=store.db.execute("SELECT id FROM tasks WHERE id=? OR (pair_key=? AND pair_key IS NOT NULL)",(identity,pair)).fetchone()
                if duplicate:
                    store.event("duplicate_task_collapsed",task_id=identity,existing=duplicate[0]); continue
                with store.db: store.db.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",(identity,pair,json.dumps(task,ensure_ascii=False),None,None))
            result["execution"]=receipt; store.finish(job["id"],result)
        except ProviderUnavailable as exc:
            store.finish(job['id'],error=str(exc));store.set('provider_blocked',str(exc));break
        except (ValueError,RuntimeError,OSError) as exc: store.finish(job["id"],error=str(exc))
        progress(store,"mining_progress",completed=n+1)
    store.set("mining_complete",bool(store.get("cases_queued")) and bool(store.get('classification_complete')) and not store.db.execute("SELECT 1 FROM jobs WHERE phase IN ('mine','connect') AND state!='done' LIMIT 1").fetchone())


def validate_all(store,provider,max_jobs=None):
    from .deliverables import create_output
    from .datasets import selected_inputs, solver_task
    for n,row in enumerate(store.db.execute("SELECT * FROM tasks WHERE assessment IS NULL ORDER BY id").fetchall()):
        if store.get('provider_blocked'):break
        if max_jobs is not None and n>=max_jobs: break
        task=json.loads(row["data"])
        try:
            input_ids=[r["unit_id"] for r in task["inputs"]]
            if not task["outputs"] and task["kind"]=="reconstructed_analysis" and not task["missing_dependencies"]:
                evidence,images=selected_inputs(store,task['inputs'])
                solution,execution=provider.call("solve",{"task":solver_task(task),"inputs":evidence},images)
                task=create_output(store,task,solution,execution)
                with store.db:store.db.execute('UPDATE tasks SET data=? WHERE id=?',(json.dumps(task,ensure_ascii=False),row['id']))
            ids=[r["unit_id"] for r in task["inputs"]+task["outputs"]]
            for node in task["stages"]: ids += [r["unit_id"] for r in node["evidence"]]
            for edge in task["edges"]: ids += [r["unit_id"] for r in edge["evidence"]]
            units=store.units(ids)
            preliminary=assess(task,units)
            if task["outputs"]:
                evidence,images=packet(store,list(units))
                def validate_review(review):
                    for entry in list(review['checks'].values())+[review['end_to_end']]:
                        if entry['state']!='unknown' and not entry['evidence']:raise ValueError('substantive review omitted evidence')
                        for ref in entry['evidence']:resolve(ref,units)
                    for ref in review['business_completion_evidence']:resolve(ref,units)
                    for check in review['objective_checks']:
                        for ref in check['sources']+([check['target']] if check['target'] else []):resolve(ref,units)
                related=[]
                for other in store.db.execute("SELECT id,data FROM tasks WHERE json_extract(data,'$.component')=? AND id!=? ORDER BY id",(task.get('component'),row['id'])):
                    candidate=json.loads(other['data'])
                    related.append({'task_id':other['id'],**{k:candidate[k] for k in ('business_case','role','outcome_key','title','instruction','inputs','outputs')}})
                review,execution=provider.call("validate",{"task_id":row['id'],"task":task,"evidence":evidence,
                    "related_tasks":related,"related_task_catalogue_complete":bool(store.get('mining_complete')),
                    "related_task_catalogue_scope":"same connected component in this inspected snapshot",
                    "objective_results":preliminary["checks"]},images,validate=validate_review)
                review.update(task_sha256=digest(task),execution_id=execution["execution_id"],
                              proposer_execution_id=task["proposer_execution_id"],execution=execution)
            else: review=None
            assessment=assess(task,units,review)
            with store.db: store.db.execute("UPDATE tasks SET data=?,pair_key=?,assessment=?,review=? WHERE id=?",
                (json.dumps(task,ensure_ascii=False),pair_fingerprint(task,units),json.dumps(assessment),json.dumps(review),row["id"]))
        except ProviderUnavailable as exc:
            store.set('provider_blocked',str(exc));break
        except (ValueError,RuntimeError,OSError) as exc:
            store.event("task_validation_blocked",task_id=row["id"],error=str(exc))
            with store.db: store.db.execute("UPDATE tasks SET assessment=? WHERE id=?",
                (json.dumps({"task_id":row["id"],"state":"unknown","end_to_end":False,"unknowns":[str(exc)],"failures":[]}),row["id"]))
        progress(store,"validation_progress",completed=n+1)


def evaluate(root,output,folders=None,provider="codex",coverage="full",model="gpt-6-astra",reasoning="max",
             workers=4,file_timeout=300,max_jobs=None,render=True):
    root=Path(root).expanduser().resolve(); folders=list(folders or DEFAULT_FOLDERS)
    if provider!="codex" or coverage!="full": raise ValueError("v3 supports the selected Codex full-coverage workflow")
    if len(folders)!=len(set(folders)) or not folders: raise ValueError("select distinct business folders")
    if any(Path(f).name!=f or not (root/f).is_dir() or (root/f).is_symlink() for f in folders): raise ValueError("folders must be immediate real directories")
    output=Path(output).expanduser().resolve()
    if output.is_relative_to(root): raise ValueError("evaluation output must be outside the source tree")
    if not 1<=workers<=32 or file_timeout<=0: raise ValueError("invalid worker/deadline configuration")
    store=Store(output,create=True)
    tessdata=ROOT/".venv/share/tessdata"
    config={"root":str(root),"folders":folders,"provider":provider,"coverage":coverage,"model":model,"reasoning":reasoning,
            "workers":workers,"file_timeout":file_timeout,"render":render,
            "loose_root_formats":sorted(LOOSE_BUSINESS_FORMATS),
            "excluded_directories":sorted(SKIP),
            "tessdata":str(tessdata) if tessdata.is_dir() else None,
            "ocr_languages":"eng+chi_sim+chi_tra" if all((tessdata/(l+".traineddata")).exists() for l in ["eng","chi_sim","chi_tra"]) else "eng"}
    store.set("config",config); store.set("protocol",protocol()); store.set("created_at",now()); store.close()
    return resume(output,max_jobs=max_jobs)


def resume(directory,max_jobs=None,retry_blocked=False):
    store=Store(directory)
    try:
        with store.lock():
            if store.get("protocol")!=protocol(): raise ValueError("evaluator changed; start a new run instead of mixing versions")
            if store.get('status') in {'completed','incomplete'} and (store.run/'run.json').exists():
                from .integrity import verify_seal
                verify_seal(store)
            if retry_blocked:
                store.set('provider_blocked',None)
                store.set('cases_queued',False)
                native_retry=store.db.execute("SELECT 1 FROM jobs WHERE phase='extract' AND state='blocked' LIMIT 1").fetchone()
                if native_retry:
                    store.set('aliases_complete',False);store.set('classification_queued',False)
                    with store.db:store.db.execute('UPDATE tasks SET assessment=NULL,review=NULL')
                with store.db:
                    store.db.execute("UPDATE jobs SET state='pending',error=NULL WHERE state='blocked'")
                    store.db.execute("UPDATE tasks SET assessment=NULL,review=NULL WHERE json_extract(assessment,'$.state')='unknown'")
            store.set("status","running")
            from .reports import refresh
            discover(store); refresh(store)
            extract_all(store); refresh(store)
            config=store.get("config"); engine=CodexProvider(store.run,config["model"],config["reasoning"])
            classify_all(store,engine,max_jobs); refresh(store)
            if not store.get('provider_blocked'):
                mine_all(store,engine,max_jobs); refresh(store)
                validate_all(store,engine,max_jobs)
            pending=store.db.execute("SELECT COUNT(*) FROM jobs WHERE phase!='administrative' AND state!='done'").fetchone()[0]
            pending += store.db.execute('SELECT COUNT(*) FROM tasks WHERE assessment IS NULL').fetchone()[0]
            store.set("status","incomplete" if pending or not store.get('mining_complete') or not store.get('discovery_complete') else "completed")
            result=refresh(store)
            from .integrity import seal
            seal(store)
            return result
    except BaseException:
        store.set("status","interrupted")
        raise
    finally: store.close()
