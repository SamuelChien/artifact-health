import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from artifact_engine.contracts import (assess, contained, digest, execute_check, graph_check,
    pair_fingerprint, references_overlap, score, task_identity)
from artifact_engine.datasets import selected_inputs, solver_task, export_dataset
from artifact_engine.integrity import seal, verify_seal, verify_source
from artifact_engine.native import extract
from artifact_engine.pipeline import discover, extract_all, classify_all, mine_all, validate_all, packet, protocol
from artifact_engine.provider import COMMON, REVIEW_KEYS
from artifact_engine.reports import refresh
from artifact_engine.store import Store, write_json


def ref(key,start=0,end=1):return {'unit_id':key,'start':start,'end':end}


def fixture():
    units={k:{'id':k,'canonical_id':k,'artifact_id':k,'locator':{'kind':'cell','cell':'A1'},
              'text':v,'status':'inspected','available_at':'2021-01-01T00:00:00+00:00'} for k,v in [('i','2'),('o','2')]}
    task={'business_case':'invoice-1','role':'accounts_payable','outcome_key':'reconcile invoice',
          'family':'reconcile_and_audit','kind':'reconstructed_analysis','title':'Reconcile invoice',
          'instruction':'Reconcile the source amount against the invoice.', 'value_rationale':'Find a discrepancy',
          'high_value':True,'inputs':[ref('i')],'outputs':[ref('o')],'temporal_cutoff':None,
          'acceptance_criteria':['The total equals the source amount.'], 'checks':[
              {'kind':'numeric_equal','sources':[ref('i')],'target':ref('o'),'tolerance':'0','schema_json':None}],
          'required_stages':['input','output'],'stages':[{'id':'a','name':'input','evidence':[ref('i')]},
            {'id':'b','name':'output','evidence':[ref('o')]}],
          'edges':[{'from':'a','to':'b','basis':'invoice references the source amount','evidence':[ref('i'),ref('o')]}],
          'missing_dependencies':[],'output_format':'json','origin':'unknown','proposer_execution_id':'proposer'}
    return task,units


def semantic(task):
    return {'task_sha256':digest(task),'execution_id':'independent','proposer_execution_id':task['proposer_execution_id'],
            'checks':{k:{'state':'met','reason':'source-grounded review','evidence':task['inputs']+task['outputs']} for k in REVIEW_KEYS},
            'objective_checks':[],'end_to_end':{'state':'met','reason':'linked source to output','evidence':task['outputs']},
            'business_completion_evidence':[],'findings':[]}


class ContractTests(unittest.TestCase):
    def test_actual_bad_number_overrides_passed_declaration(self):
        task,units=fixture();units['i']['text']='999';task['inputs']=[ref('i',0,3)];task['checks'][0]['sources']=task['inputs']
        task['validation']={'passed':True}; result=assess(task,units,semantic(task))
        self.assertEqual(result['state'],'not_ready');self.assertIn('OUTPUT_CHECK_FAILED',result['failures'])
        self.assertEqual(result['checks'][0]['detail']['expected'],'999')

    def test_correct_output_requires_independent_review(self):
        task,units=fixture();self.assertEqual(assess(task,units)['state'],'unknown')
        self.assertEqual(assess(task,units,semantic(task))['state'],'ready')
        review=semantic(task);review['execution_id']='proposer'
        self.assertIn('SELF_REVIEW',assess(task,units,review)['failures'])

    def test_generation_cannot_review_itself(self):
        task,units=fixture();task['generation_execution_id']='independent'
        self.assertIn('SELF_REVIEW',assess(task,units,semantic(task))['failures'])

    def test_same_pair_different_family_collapses(self):
        task,units=fixture();other=copy.deepcopy(task);other['family']='create_or_revise';other['title']='Different wording'
        self.assertEqual(task_identity(task),task_identity(other));self.assertEqual(pair_fingerprint(task,units),pair_fingerprint(other,units))
        other['role']='sales';self.assertEqual(pair_fingerprint(task,units),pair_fingerprint(other,units))

    def test_distinct_outcome_can_share_inputs(self):
        task,units=fixture();other=copy.deepcopy(task);other['outcome_key']='approve purchase';units['p']={**units['o'],'id':'p','canonical_id':'p'};other['outputs']=[ref('p')]
        self.assertNotEqual(task_identity(task),task_identity(other));self.assertNotEqual(pair_fingerprint(task,units),pair_fingerprint(other,units))

    def test_partition_of_same_input_does_not_multiply_identity(self):
        task,units=fixture();units['i']['text']='123456';task['inputs']=[ref('i',0,6)]
        other=copy.deepcopy(task);other['inputs']=[ref('i',0,3),ref('i',3,6)]
        self.assertEqual(pair_fingerprint(task,units),pair_fingerprint(other,units))

    def test_readable_defect_is_useful_audit_input(self):
        task,units=fixture();units['i']['business_defects']=['arithmetic_error']
        self.assertEqual(assess(task,units,semantic(task))['state'],'ready')
        units['i']['blocking']=True;self.assertEqual(assess(task,units,semantic(task))['state'],'not_ready')

    def test_checker_cannot_read_outside_input_span(self):
        task,units=fixture();units['i']['text']='2 999';task['checks'][0]['sources']=[ref('i',2,5)]
        self.assertIn('OUTPUT_CHECK_FAILED',assess(task,units,semantic(task))['failures'])

    def test_equal_text_in_distinct_documents_is_not_leakage(self):
        task,units=fixture();units['i']['text']=units['o']['text']='A legitimate extracted description'
        self.assertFalse(references_overlap([ref('i',0,34)],[ref('o',0,34)],units))

    def test_alias_and_same_page_image_leakage(self):
        task,units=fixture();units['o']['canonical_id']='i';self.assertTrue(references_overlap(task['inputs'],task['outputs'],units))
        units['o']['canonical_id']='o';units['i']['image']=units['o']['image']='page.png'
        self.assertTrue(references_overlap(task['inputs'],task['outputs'],units))

    def test_historical_cutoff_unknown_and_future(self):
        task,units=fixture();task['kind']='historical_employee_task'
        self.assertIn('HISTORICAL_CUTOFF_UNKNOWN',assess(task,units,semantic(task))['unknowns'])
        task['temporal_cutoff']='2020-01-01T00:00:00Z'
        self.assertIn('FUTURE_INPUT',assess(task,units,semantic(task))['failures'])
    def test_timeless_analysis_can_mark_chronology_not_applicable(self):
        task,units=fixture();review=semantic(task);review['checks']['chronology']['state']='not_applicable'
        self.assertEqual(assess(task,units,review)['state'],'ready')
    def test_chronology_cannot_be_waived_for_historical_or_timeline_tasks(self):
        for kind,family in [('historical_employee_task','reconcile_and_audit'),('reconstructed_analysis','reconstruct_timeline')]:
            task,units=fixture();task.update(kind=kind,family=family);review=semantic(task)
            review['checks']['chronology']['state']='not_applicable'
            self.assertIn('CHRONOLOGY_REQUIRED',assess(task,units,review)['failures'])

    def test_checks_are_executed_not_declared(self):
        task,units=fixture();units['j']={**units['i'],'id':'j','text':'{"amount":"wrong"}'}
        check={'kind':'json_schema','target':ref('j',0,len(units['j']['text'])),'schema':{'type':'object','properties':{'amount':{'type':'number'}},'required':['amount']}}
        self.assertEqual(execute_check(check,units)['state'],'failed')
        check['schema']={'$ref':'https://invalid.example/schema'};self.assertEqual(execute_check(check,units)['state'],'unknown')

    def test_graph_disconnected_and_cyclic(self):
        task,units=fixture();task['edges']=[];self.assertEqual(graph_check(task,units)['state'],'failed')
        task,units=fixture();task['edges'].append({'from':'b','to':'a','basis':'cycle','evidence':[ref('i')]})
        with self.assertRaisesRegex(ValueError,'cycle'):graph_check(task,units)

    def test_readiness_capacity_and_unknown_not_blended(self):
        result=score([{'state':'ready'},{'state':'not_ready'},{'state':'unknown'}],True)
        self.assertEqual(result['readiness_score'],50);self.assertEqual(result['capacity'],1);self.assertEqual(result['task_coverage'],66.67)
        self.assertIsNone(score([],False)['capacity']);self.assertIsNone(score([],True)['readiness_score']);self.assertEqual(score([],True)['capacity'],0)

    def test_missing_dependency_prevents_ready(self):
        task,units=fixture();task['missing_dependencies']=['receipt'];result=assess(task,units,semantic(task))
        self.assertEqual(result['state'],'unknown');self.assertFalse(result['end_to_end'])
    def test_independent_fragment_rejection_blocks_capacity(self):
        task,units=fixture();review=semantic(task);review['checks']['task_independence']['state']='failed'
        result=assess(task,units,review);self.assertEqual(result['state'],'not_ready');self.assertIn('TASK_INDEPENDENCE',result['failures'])


class FakeProvider:
    def __init__(self,run):self.run=Path(run);self.calls=[]
    def call(self,phase,payload,images=(),validate=None):
        self.calls.append(phase);execution_id='exec-'+str(len(self.calls))
        if phase=='classify':
            result={'unit_reviews':[{'unit_id':u['id'],'state':'reviewed','reason':'fixture inspection','presentation':'not_applicable'} for u in payload['units']],
                'documents':[{'artifact_id':a['id'],'document_type':'report','summary':'Case invoice-1','roles':['accounts_payable'],
                    'origin':'unknown','ai_involvement':'unknown','entities':[],'findings':[]} for a in payload['artifacts']]}
        elif phase=='mine':result={'tasks':[],'limitations':[],'no_task_reason':'standalone fixture unit'}
        elif phase=='validate':
            result=semantic(payload['task'])
            for k in ('task_sha256','execution_id','proposer_execution_id'):result.pop(k)
        elif phase=='connect':result={'bundles':[],'limitations':[]}
        else:raise AssertionError(phase)
        if validate:validate(result)
        prompt=COMMON+'\nEVIDENCE PACKET:\n'+json.dumps(payload)
        receipt={'execution_id':execution_id,'phase':phase,'prompt_sha256':digest(prompt),'response_sha256':digest(result),'image_sha256':{}}
        folder=self.run/'executions'/execution_id;folder.mkdir(parents=True)
        (folder/'prompt.txt').write_text(prompt);write_json(folder/'response.json',result);write_json(folder/'receipt.json',receipt)
        return result,receipt


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.base=Path(self.temporary.name)
        self.source=self.base/'source';(self.source/'Finance/sub').mkdir(parents=True);(self.source/'Sales').mkdir()
        (self.source/'Finance/sub/input.json').write_text('{"amount":2}')
        (self.source/'Sales/output.json').write_text('{"total":2}')
        self.store=Store(self.base/'run',create=True)
        self.store.set('config',{'root':str(self.source),'folders':['Finance','Sales'],'workers':1,'file_timeout':20,'render':False,'model':'test','reasoning':'max','coverage':'full','ocr_languages':'eng'})
        self.store.set('protocol',protocol());self.store.set('created_at','fixture')
        discover(self.store);extract_all(self.store)
    def tearDown(self):self.store.close();self.temporary.cleanup()
    def add_task(self):
        task,_=fixture();rows=list(self.store.db.execute('SELECT id,data FROM artifacts ORDER BY id'))
        by_path={json.loads(r['data'])['paths'][0]:r['id'] for r in rows}
        input_unit=next(iter(self.store.units([r[0] for r in self.store.db.execute('SELECT id FROM units WHERE artifact_id=?',(by_path['Finance/sub/input.json'],))]).values()))
        output_unit=next(iter(self.store.units([r[0] for r in self.store.db.execute('SELECT id FROM units WHERE artifact_id=?',(by_path['Sales/output.json'],))]).values()))
        replacements={'i':input_unit['id'],'o':output_unit['id']}
        task=json.loads(json.dumps(task).replace('"i"',json.dumps(replacements['i'])).replace('"o"',json.dumps(replacements['o'])))
        key=task_identity(task);units=self.store.units()
        self.store.db.execute('INSERT INTO tasks VALUES (?,?,?,?,?)',(key,pair_fingerprint(task,units),json.dumps(task),None,None));self.store.db.commit()
        return task,key
    def test_native_cache_resume_and_verified_role_export(self):
        count=self.store.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0];extract_all(self.store)
        self.assertEqual(count,self.store.db.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0])
        provider=FakeProvider(self.store.run);classify_all(self.store,provider);mine_all(self.store,provider)
        task,key=self.add_task();validate_all(self.store,provider);self.store.set('status','completed');refresh(self.store);seal(self.store)
        summary=json.loads((self.store.run/'score-summary.json').read_text());folders={r['folder']:r for r in summary['folders']}
        self.assertEqual(folders['.']['verified_capacity'],1);self.assertEqual(folders['Finance']['readiness_score'],0)
        self.assertEqual(folders['Finance']['linked_readiness_score'],100);self.assertEqual(folders['Finance/sub']['direct_files'],1)
        result=export_dataset(self.store.run,self.base/'dataset');self.assertEqual(result['exported_tasks'],1)
        pack=next((self.base/'dataset/accounts_payable').rglob('task.json'))
        self.assertNotIn('outputs',json.loads(pack.read_text()));self.assertTrue((self.base/'dataset/evaluator-only'/key/'acceptance.json').exists())
        self.assertEqual(verify_source(self.store.run)['status'],'passed')
        (self.source/'Finance/sub/input.json').write_text('{"amount":999}')
        self.assertEqual(verify_source(self.store.run)['status'],'failed')
    def test_selected_input_excludes_unselected_answer(self):
        task,key=self.add_task();u=self.store.units()[task['inputs'][0]['unit_id']]
        u['text']='2 HIDDEN ANSWER';self.store.db.execute('UPDATE units SET data=? WHERE id=?',(json.dumps(u),u['id']));self.store.db.commit()
        packet,images=selected_inputs(self.store,task['inputs']);self.assertNotIn('HIDDEN',json.dumps(packet))
        self.assertNotIn('checks',solver_task(task));self.assertEqual(images,[])
    def test_reconstruction_does_not_erase_missing_required_stages(self):
        from artifact_engine.deliverables import create_output
        task,key=self.add_task();task['outputs']=[];task['required_stages'].append('approval')
        task['stages'].append({'id':'approval','name':'approval','evidence':[]})
        solution={'title':'Explicit reconstruction control','sections':[{'heading':'Result','text':'2','evidence':task['inputs']}],'tables':[],'limitations':['No approval supplied.']}
        result=create_output(self.store,task,solution,{'execution_id':'generation-control'})
        self.assertIn('approval',result['required_stages'])
        self.assertEqual(graph_check(result,self.store.units())['state'],'unknown')
    def test_receipt_rejects_modified_reports_and_state(self):
        refresh(self.store);seal(self.store);verify_seal(self.store)
        (self.store.run/'tasks.jsonl').write_text('changed')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):verify_seal(self.store)
    def test_v3_external_export_has_only_allowlisted_counts(self):
        from artifact_health import export_summary
        refresh(self.store);seal(self.store);result=export_summary(self.store.run)
        serialized=json.dumps(result)
        self.assertEqual(result['schema'],'artifact-health-external@3');self.assertNotIn(str(self.source),serialized)
        self.assertNotIn('input.json',serialized);self.assertNotIn('provenance',serialized)
    def test_nonmaterial_office_lock_has_no_task_evidence(self):
        (self.source/'Finance/~$locked.xlsx').write_bytes(b'lock')
        self.store.set('discovery_complete',False);discover(self.store)
        self.assertEqual(self.store.db.execute("SELECT status FROM files WHERE path LIKE '%locked.xlsx'").fetchone()[0],'non_material')
        refresh(self.store);seal(self.store)
        (self.source/'Finance/~$locked.xlsx').unlink()
        self.assertEqual(verify_source(self.store.run)['status'],'passed')
    def test_ocr_limit_cannot_be_reported_as_source_defect(self):
        provider=FakeProvider(self.store.run)
        original=provider.call
        def bad_classification(phase,payload,images=(),validate=None):
            result,receipt=original(phase,payload,images)
            if phase=='classify':
                result['documents'][0]['findings']=[{'kind':'DEFECT','attribution':'inspection_tool','code':'OCR_LIMIT','detail':'our OCR failed','evidence':[]}]
                if validate:validate(result)
            return result,receipt
        provider.call=bad_classification;classify_all(self.store,provider)
        self.assertFalse(self.store.get('classification_complete'))
        self.assertEqual(self.store.db.execute("SELECT state FROM jobs WHERE phase='classify'").fetchone()[0],'blocked')


class NativeTests(unittest.TestCase):
    def test_mail_copy_alias_requires_exact_decoded_headers_and_body(self):
        from email.message import EmailMessage
        from artifact_engine.reconcile import reconcile_aliases
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);store=Store(base/'run',create=True)
            message=EmailMessage();message['Date']='Mon, 01 Jan 2024 10:00:00 +0000';message['From']='a@example.com';message['To']='b@example.com';message['Subject']='Order 42';message['Message-ID']='<42@example.com>';message.set_content('Two cylinders ordered.\n')
            eml=base/'mail.eml';eml.write_bytes(message.as_bytes())
            txt=base/'mail.txt';txt.write_text('\n'.join(k+': '+str(message[k]) for k in ['Date','From','Message-ID','Subject','To'])+'\n\nTwo cylinders ordered.\n')
            for key,path in [('eml',eml),('txt',txt)]:
                data=extract(path,key,base/key,{'render':False});data.update(extension=path.suffix,paths=[],sha256=key)
                store.add_artifact(key,key,path.suffix,data)
            reconcile_aliases(store)
            self.assertEqual(store.db.execute('SELECT canonical_artifact FROM aliases WHERE artifact_id=?',('txt',)).fetchone()[0],'eml')
            native=next(u for u in store.units().values() if u['artifact_id']=='eml');copy_unit=next(u for u in store.units().values() if u['artifact_id']=='txt')
            self.assertEqual(native['canonical_id'],copy_unit['canonical_id']);store.close()

    def test_schema_malformed_is_unknown_not_a_pass(self):
        task,units=fixture();check={'kind':'json_schema','target':ref('o'),'schema':{'type':'not-a-json-type'}}
        self.assertEqual(execute_check(check,units)['state'],'unknown')

    def test_empty_text_drawing_is_rendered_not_blank_defect(self):
        from reportlab.pdfgen.canvas import Canvas
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'drawing.pdf';canvas=Canvas(str(p));canvas.line(20,20,500,500);canvas.save()
            data=extract(p,'drawing',Path(directory)/'review',{'render':True})
            self.assertEqual(data['status'],'inspected');self.assertTrue(data['units'][0]['image'])
            self.assertFalse(any(f['kind']=='DEFECT' for f in data['findings']))
    def test_heic_is_actually_decoded(self):
        from PIL import Image
        from pillow_heif import register_heif_opener
        register_heif_opener()
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'photo.heic';Image.new('RGB',(80,60),'white').save(p)
            data=extract(p,'heic',Path(directory)/'review',{'render':True})
            self.assertEqual(data['status'],'inspected');self.assertEqual(data['units'][0]['width'],80)
    def test_docx_external_link_and_template_do_not_become_corruption(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'template.docx';doc=Document();doc.add_paragraph('Certificate of Analysis template. Result: ____')
            doc.part.relate_to('mailto:quality@example.com','http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink',is_external=True);doc.save(p)
            data=extract(p,'docx',Path(directory)/'review',{'render':False})
            self.assertTrue(any('mailto:' in u['text'] for u in data['units']));self.assertFalse(any(f['kind']=='DEFECT' for f in data['findings']))
    def test_formula_reference_literal_is_not_error(self):
        from openpyxl import Workbook
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'book.xlsx';w=Workbook();w.active['A1']='="literal #REF!"';w.active['A2']='=#REF!';w.save(p)
            data=extract(p,'xlsx',Path(directory)/'review',{'render':False})
            errors=[f for f in data['findings'] if f['code']=='INVALID_FORMULA_REFERENCE'];self.assertEqual(len(errors),1);self.assertIn('A2',errors[0]['detail'])


if __name__=='__main__':unittest.main()
