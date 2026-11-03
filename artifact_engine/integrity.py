"""Snapshot integrity, local report receipts and current-source verification."""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from . import VERSION
from .contracts import require
from .store import Store, now, write_json


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def state_hash(store):
    h=hashlib.sha256()
    for table in ('config','folders','files','artifacts','units','jobs','tasks','links','aliases'):
        h.update(table.encode())
        for row in store.db.execute('SELECT * FROM '+table+' ORDER BY 1'):
            h.update(json.dumps(list(row),ensure_ascii=False,separators=(',',':')).encode());h.update(b'\n')
    return h.hexdigest()


def seal(store):
    reports=['score-summary.json','score-summary.csv','tasks.jsonl','files.jsonl','provenance.jsonl','connections.jsonl','index.html']
    reports += [str(p.relative_to(store.run)) for p in sorted((store.run/'folders').glob('*.html'))]
    write_json(store.run/'run.json',{'schema':VERSION,'sealed_at':now(),'state_sha256':state_hash(store),
        'reports':{p:file_hash(store.run/p) for p in reports},'protocol':store.get('protocol'),'completion_state':store.get('status')})


def verify_seal(store):
    receipt=json.loads((store.run/'run.json').read_text())
    require(receipt.get('schema')==VERSION,'unsupported run receipt')
    require(receipt['state_sha256']==state_hash(store),'evaluation state changed after report sealing; resume the run to revalidate')
    for path,expected in receipt['reports'].items():
        require(file_hash(store.run/path)==expected,'report hash mismatch: '+path)
    return receipt


def read_run(directory):
    store=Store(directory)
    try:
        with store.lock():
            verify_seal(store)
            return json.loads((store.run/'score-summary.json').read_text())
    finally:store.close()


def verify_source(directory,source=None,**unused):
    from .pipeline import BUNDLES,SKIP,LOOSE_BUSINESS_FORMATS,bundle_copy
    from .reports import refresh
    store=Store(directory)
    try:
        with store.lock():
            verify_seal(store)
            config=store.get('config');root=Path(source or config['root']).resolve()
            current={};errors=[]
            for folder in config['folders']:
                for parent,dirs,files in os.walk(root/folder,followlinks=False,onerror=lambda e:errors.append(str(e))):
                    base=Path(parent);bundles=[n for n in dirs if Path(n).suffix.lower() in BUNDLES];symlinks=[n for n in dirs if (base/n).is_symlink()]
                    dirs[:]=[n for n in dirs if n not in SKIP and n not in bundles and n not in symlinks]
                    for name in files+bundles+symlinks: current[str((base/name).relative_to(root))]=base/name
            for path in root.iterdir():
                if path.is_file() and not path.is_symlink() and not path.name.startswith('.') and path.suffix.lower() in LOOSE_BUSINESS_FORMATS:
                    current[path.name]=path
            saved={r['path']:dict(r) for r in store.db.execute('SELECT * FROM files') if not json.loads(r['detail']).get('attachment_of')}
            changed=[];unchecked=[];checked=0
            for name in sorted(saved.keys()&current.keys()):
                row=saved[name];path=current[name]
                if row['status'] in {'non_material','excluded'}:continue
                if not row['sha256']:unchecked.append(name);continue
                try:
                    before=path.stat()
                    if path.is_symlink():changed.append(name);continue
                    if path.is_dir():
                        with tempfile.TemporaryDirectory(dir=store.run) as temporary:
                            value=file_hash(bundle_copy(path,Path(temporary)/('copy'+path.suffix)))
                    else:value=file_hash(path)
                    after=path.stat();checked+=1
                    if value!=row['sha256'] or (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):changed.append(name)
                except OSError as exc:unchecked.append(name);errors.append(str(exc))
            def incidental(name):
                value=Path(name).name
                return value.startswith(('~$','._')) or value in {'.DS_Store','Thumbs.db','desktop.ini'}
            added=sorted(name for name in current.keys()-saved.keys() if not incidental(name) and not current[name].is_symlink())
            removed=sorted(name for name in saved.keys()-current.keys() if saved[name]['status'] not in {'non_material','excluded'})
            result={'status':'failed' if changed or added or removed else 'incomplete' if unchecked or errors else 'passed',
                'checked_at':now(),'source':str(root),'checked_files':checked,'changed':changed,'added':added,'removed':removed,'unchecked':unchecked,'errors':errors,
                'exclusions':'temporary/system files and symlinks are not substantive source evidence'}
            store.set('source_freshness',result);refresh(store);seal(store)
            return result
    finally:store.close()


def compare(before,after,**unused):
    left,right=read_run(before),read_run(after)
    require(left['protocol']==right['protocol'],'comparison blocked: evaluator/rubric/runtime differs')
    for key in ('model','reasoning','folders','coverage','ocr_languages'):
        require(left['scope'][key]==right['scope'][key],'comparison blocked: scope or provider differs')
    require(left['completion_state']==right['completion_state']=='completed','comparison blocked: evaluation is not complete')
    a={r['folder']:r for r in left['folders']};b={r['folder']:r for r in right['folders']}
    return {'status':'passed','schema':VERSION,'folders':[{'folder':key,'before':a.get(key),'after':b.get(key),
        'readiness_delta':b[key]['readiness_score']-a[key]['readiness_score'] if key in a and key in b and
        a[key]['score_scope']==b[key]['score_scope']=='full' and a[key]['readiness_score'] is not None and b[key]['readiness_score'] is not None else None}
        for key in sorted(a.keys()|b.keys())]}
