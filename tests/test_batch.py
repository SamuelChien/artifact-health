import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import measure as scanner
from scripts.batch import measure_folders, refresh_batch
from scripts.evidence import validate_measurement


class BatchTests(unittest.TestCase):
    def test_parallel_scan_matches_serial_bytes_and_observations(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); source=base/'source'; source.mkdir()
            for n in range(8): (source/f'{n}.txt').write_text('fixture '+str(n))
            (source/'bad.json').write_text('{bad json')
            one, code1=scanner.measure(source,base/'one',workers=1)
            many, code2=scanner.measure(source,base/'many',workers=3)
            self.assertEqual(code1,1); self.assertEqual(code1,code2)
            self.assertEqual(one['source_fingerprint'],many['source_fingerprint'])
            self.assertEqual((base/'one/inventory.jsonl').read_bytes(),(base/'many/inventory.jsonl').read_bytes())
            self.assertEqual((base/'one/findings.jsonl').read_bytes(),(base/'many/findings.jsonl').read_bytes())
            self.assertFalse(list((base/'many').glob('.probe-*')))

    def test_timeout_does_not_become_a_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); source=base/'source'; source.mkdir()
            (source/'a.txt').write_text('fixture')
            measurement,code=scanner.measure(source,base/'out',workers=2,file_timeout=0.000001)
            self.assertEqual(code,2)
            self.assertEqual(measurement['status'],'partial')
            validate_measurement(base/'out')

    def test_batch_builds_independent_bundles_without_inventing_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary); source=base/'source'; source.mkdir()
            for name in ['Finance','Invoices']:
                (source/name).mkdir(); (source/name/'record.txt').write_text('fixture')
            output=source/'artifact-out'
            result=measure_folders(source,output,['Finance','Invoices'],workers=2)
            self.assertEqual(result['scored_folders'],0)
            self.assertEqual(result['folders'],2)
            for name in ['Finance','Invoices']:
                validate_measurement(output/name)
                self.assertTrue((output/name/'report.html').is_file())
            summary=json.loads((output/'score-summary.json').read_text())
            self.assertTrue(all(row['artifact_health_score'] is None for row in summary['folders']))
            self.assertEqual(refresh_batch(output)['folders'],2)
            self.assertIn('Finance/report.html',(output/'index.html').read_text())


    def test_contextual_defect_is_visible_in_folder_scorecard(self):
        from scripts.grade import grade
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / 'source'
            (source / 'Finance').mkdir(parents=True)
            (source / 'Finance/record.txt').write_text('Quantity 2 at 10; incorrect total 30.')
            output = base / 'reports'
            measure_folders(source, output, ['Finance'], workers=1)
            run = output / 'Finance'
            measurement, receipt, inventory, _ = validate_measurement(run)
            evidence = [{'path': 'record.txt', 'sha256': inventory[0]['sha256'],
                         'locator': 'total field', 'mode': 'text',
                         'units': {'kind': 'file', 'ranges': [[1, 1]]}}]
            rubric = json.loads(scanner.RUBRIC_PATH.read_text())
            review = {key: measurement[key] for key in ['source_fingerprint', 'inventory_sha256', 'findings_sha256']}
            review.update(rubric_version=rubric['version'], measurement_run_id=receipt['run_id'],
                          measurement_sha256=receipt['reports']['measurement.json'],
                          reviewer={'kind': 'agent', 'name': 'fixture', 'model': 'fixture'},
                          selection='Synthetic regression fixture.',
                          dimensions={key: {'rating': 2, 'scope': 'sample', 'reviewed_paths': ['record.txt'],
                                            'evidence': evidence, 'reason': 'Fixture discrepancy.'} for key in rubric['dimensions']},
                          findings=[{'kind': 'DEFECT', 'severity': 'major', 'status': 'confirmed',
                                     'code': 'INCORRECT_TOTAL', 'detail': '2 x 10 is 20, not 30. Reconcile total.',
                                     'evidence': evidence}])
            review_path = base / 'review.json'
            review_path.write_text(json.dumps(review))
            grade(run, review_path, output / 'grades/Finance')
            result = refresh_batch(output)
            row = json.loads((output / 'score-summary.json').read_text())['folders'][0]
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(row['confirmed_findings'], 1)
            self.assertEqual(row['mechanical_defects'], 0)
            self.assertEqual(row['contextual_defects'], 1)
            self.assertIn('INCORRECT_TOTAL', (run / 'report.html').read_text())
            self.assertIn('Reconcile total', (run / 'report.html').read_text())


if __name__=='__main__': unittest.main()
