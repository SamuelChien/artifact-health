"""Only measured representation equivalence and verified export-index hashes create aliases."""
import json
from pathlib import Path

from .contracts import digest


def reconcile_aliases(store):
    if store.get('aliases_complete'):return
    documents={r['id']:json.loads(r['data']) for r in store.db.execute('SELECT id,data FROM artifacts')}
    projections={};logical={}
    for key,data in documents.items():
        if data.get('status')!='inspected':continue
        if data.get('extension')=='.eml':
            for value in data.get('mail_projections',[]):projections.setdefault(value,key)
        signature=data.get('logical_id',key)
        if signature in logical:
            with store.db:store.db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?,?)',(key,logical[signature],'identical decoded native message, headers and attachments'))
        else:logical[signature]=key
    for key,data in documents.items():
        match=projections.get(data.get('mail_text_projection'))
        if match:
            native=logical.get(documents[match].get('logical_id'),match)
            original=[json.loads(r[0]) for r in store.db.execute('SELECT data FROM units WHERE artifact_id=? ORDER BY rowid',(native,))]
            copies=[json.loads(r[0]) for r in store.db.execute('SELECT data FROM units WHERE artifact_id=? ORDER BY rowid',(key,))]
            # Exact chunks, including offsets; a partial readable export is not a complete message alias.
            mapped=[]
            for unit in copies:
                choices=[u for u in original if u['text']==unit['text'] and u['locator'].get('text_offset')==unit['locator'].get('text_offset')]
                if not choices:break
                mapped.append((unit,choices[0]))
            if len(mapped)==len(copies) and copies:
                with store.db:
                    store.db.execute('INSERT OR REPLACE INTO aliases VALUES (?,?,?)',(key,native,'exact decoded text/header projection; attachments reviewed in native EML'))
                    for unit,source in mapped:
                        unit.update(canonical_id=source['canonical_id'],available_at=source.get('available_at'),availability_basis='verified projection of native EML')
                        store.db.execute('UPDATE units SET data=? WHERE id=?',(json.dumps(unit,ensure_ascii=False),unit['id']))
    files={r['path']:dict(r) for r in store.db.execute('SELECT path,sha256,detail FROM files')}
    for key,data in documents.items():
        if not data.get('index_records'):continue
        checked=0;issues=[]
        for path in data.get('paths',[]):
            parent=Path(path).parent
            with Path(data['index_records']).open() as stream:
                for line in stream:
                    row=json.loads(line)
                    pairs=[(row.get('path'),row.get('sha256'))] if 'path' in row else [(row.get('eml_file'),row.get('sha256')),(row.get('text_file'),row.get('text_sha256'))]
                    for relative,expected in pairs:
                        if relative is None:continue
                        parts=(parent/relative).parts
                        if Path(relative).is_absolute() or '..' in parts:
                            issues.append({'path':relative,'reason':'unsafe index path'});continue
                        target=str(parent/relative);actual=files.get(target)
                        if not expected or not actual or expected!=actual['sha256']:
                            issues.append({'path':target,'reason':'missing source or checksum mismatch'})
                        else:checked+=1
        data['index_verification']={'state':'failed' if issues else 'met','checked_references':checked,'issues':issues}
        if issues:data.setdefault('findings',[]).append({'kind':'DEFECT','code':'EXPORT_INDEX_MISMATCH','detail':f'{len(issues)} indexed source references did not verify'})
        with store.db:store.db.execute('UPDATE artifacts SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),key))
    store.set('aliases_complete',True)
