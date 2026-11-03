import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from email.message import EmailMessage

from scripts.measure import probe


class ContentSignalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow optional')
    def test_pixels_are_not_photo_quality_or_authenticity(self):
        from PIL import Image
        p=self.root/'tiny.png';Image.new('RGB',(32,16)).save(p)
        r=probe(p)
        self.assertEqual(r['metrics']['width_pixels'],32)
        self.assertIsNone(r['metrics']['visual_quality'])
        self.assertIsNone(r['metrics']['subject_accuracy'])

    def test_email_declaration_does_not_authenticate_sender(self):
        m=EmailMessage();m['From']='Fixture <fixture@example.test>';m['Date']='Mon, 02 Nov 2026 12:00:00 +0000'
        m.set_content('AI-generated fixture.');p=self.root/'m.eml';p.write_bytes(m.as_bytes())
        r=probe(p);self.assertEqual(r['metrics']['origin_signals']['declaration_count'],1)
        self.assertIsNone(r['metrics']['document_metadata']['authenticated_creator'])

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Pillow optional')
    def test_multiframe_metrics_describe_primary_image_not_last_auxiliary_frame(self):
        from PIL import Image
        p=self.root/'two.gif';first=Image.new('RGB',(32,16),'red');second=Image.new('RGB',(32,16),'blue')
        first.save(p,save_all=True,append_images=[second])
        with Image.open(p) as image: expected_mode=image.mode
        r=probe(p)['metrics']
        self.assertEqual(r['frames'],2)
        self.assertEqual(r['mode'],expected_mode)
        self.assertEqual(len(r['frame_details']),2)

    def test_docx_includes_notes_metadata_and_shape_counts(self):
        p=self.root/'notes.docx'
        with zipfile.ZipFile(p,'w') as z:
            z.writestr('[Content_Types].xml','<Types/>')
            z.writestr('word/document.xml','<document><p><t>AI-assisted fixture</t></p><tbl/><drawing/></document>')
            z.writestr('word/footnotes.xml','<footnotes><p><t>Important footnote</t></p></footnotes>')
            z.writestr('docProps/core.xml','<coreProperties><creator>Unverified claim</creator><created>2026-11-03T00:00:00Z</created><modified>2026-11-02T00:00:00Z</modified></coreProperties>')
        r=probe(p)['metrics']
        self.assertEqual(r['tables'],1);self.assertEqual(r['drawings'],1)
        self.assertIn('word/footnotes.xml',r['review_units']['labels'])
        self.assertEqual(r['document_metadata']['metadata_date_order'],'modified_before_created')
        self.assertEqual(r['origin_signals']['declaration_count'],1)
        self.assertIsNone(r['document_metadata']['authenticated_creator'])

    def test_json_syntax_is_not_semantic_fidelity(self):
        p=self.root/'extract.json';p.write_text('{"invoice": "fixture", "total": 20}')
        r=probe(p)['metrics']['json_shape']
        self.assertEqual(r['top_level_keys'],['invoice','total'])
        self.assertEqual(r['source_fidelity'],'not_checked')

    def test_heic_missing_reader_is_not_corruption(self):
        p=self.root/'fixture.heic';p.write_bytes(b'fixture')
        self.assertEqual(probe(p)['status'],'unavailable')
