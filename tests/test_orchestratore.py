import copy, json, unittest
from dataclasses import replace
from types import SimpleNamespace
import orchestratore as o

class FakeDrive:

    def __init__(self):
        self.items = {}
        self.contents = {}
        self.n = 0

    def add(self, fid, name, parent=None, mime=o.FOLDER_MIME, props=None, data=None, createdTime=None):
        x = {'id': fid, 'name': name, 'mimeType': mime, 'parents': [parent] if parent else [], 'appProperties': dict(props or {})}
        if createdTime is not None:
            x['createdTime'] = createdTime
        self.items[fid] = x
        if data is not None:
            self.contents[fid] = data
        return copy.deepcopy(x)

    def get(self, f):
        return copy.deepcopy(self.items[f])

    def list(self, p):
        return [copy.deepcopy(x) for x in self.items.values() if p in x['parents'] and (not x.get('trashed'))]

    def set_props(self, f, p):
        self.items[f]['appProperties'].update(p)

    def move(self, f, p):
        self.items[f]['parents'] = [p]

    def read(self, f):
        return self.contents[f]

    def create_folder(self, p, n, props):
        self.n += 1
        return self.add('new' + str(self.n), n, p, props=props)

    def create_text(self, p, n, data, props):
        self.n += 1
        return self.add('new' + str(self.n), n, p, 'text/plain', props, data)

class FakeKaggle:

    def __init__(self, state='complete'):
        self.state = state
        self.pushes = []

    def kernels_status(self, s):
        if isinstance(self.state, Exception):
            raise self.state
        return SimpleNamespace(status=self.state)

    def kernels_push(self, p):
        self.pushes.append(p)

class Services:

    def __init__(self, d, k):
        self.d = d
        self.k = k

    def drive(self):
        return self.d

    def kaggle(self):
        return self.k

def cfg(**kw):
    return replace(o.Config('incoming', 'processing', 'raw', 'processed', 'completed', 'errors', 'root', 'owner/kernel', worker_verified=True), **kw)

def tree(d):
    d.add('science', 'scienze', 'root')
    d.add('sub_biologia', 'biologia', 'science')
    d.add('sub_chimica', 'chimica', 'science')
    for s in ('fisica', 'inglese'):
        d.add('sub_' + s, s, 'root')
    d.add('it', 'italiano', 'root')
    for s in ('letteratura', 'purgatorio'):
        d.add('sub_' + s, s, 'it')
    for s in o.SUBJECTS:
        d.add('less_' + s, 'lezioni', 'sub_' + s)
        d.add('topics_' + s, 'argomenti', 'sub_' + s)
        d.add('book_' + s, 'libro', 'sub_' + s)
    return {s: 'less_' + s for s in o.SUBJECTS}
LINES = ['[00:00 - 00:04] Riga originale uno.\r\n', '[00:04 - 00:08] Riga originale due.\n', '[00:08 - 00:12] Riga originale tre.\n']
RAW = ''.join(LINES).encode()

def seg(a, b, m, status='classified', conf=0.9, margin=0.3):
    return {'start_line': a, 'end_line': b, 'materia': m, 'confidence': conf, 'runner_up_margin': margin, 'status': status}

def install(d, sid='audio', segs=None, overrides=None, duplicate=1, created='2026-11-06T23:30:00Z'):
    name = 'registrazione variabile senza materia.m4a'
    a = d.add(sid, name, 'processing', 'audio/mp4', createdTime=created)
    d.set_props(sid, {'orpheus_original_name': name})
    r = d.add('raw_' + sid, 'master.txt', 'raw', 'text/plain', {'orpheus_source_id': sid, 'orpheus_kind': 'raw_transcript', 'orpheus_original_name': name}, RAW)
    p = {'source_id': sid, 'raw_id': r['id'], 'raw_sha256': o.digest(RAW), 'source_model': o.SOURCE_MODEL, 'version': 1, 'window_seconds': 60, 'thresholds': {'min_score': 0.55, 'min_margin': 0.15}, 'segments': segs or [seg(1, 2, 'biologia'), seg(3, 3, 'chimica')]}
    if overrides:
        p.update(overrides)
    data = o.canonical(p)
    props = {'orpheus_source_id': sid, 'orpheus_kind': 'subject_plan', 'orpheus_raw_id': r['id'], 'orpheus_raw_sha256': o.digest(RAW), 'orpheus_plan_sha256': o.digest(data), 'orpheus_classifier_version': '1', 'orpheus_original_name': name}
    for i in range(duplicate):
        d.add('plan_' + sid + str(i), 'plan.json', 'raw', 'application/json', props, data)
    return (a, r, p)

class TestPipeline(unittest.TestCase):

    def setUp(self):
        self.d = FakeDrive()
        self.targets = tree(self.d)
        self.k = FakeKaggle()
        self.p = o.Pipeline(cfg(), Services(self.d, self.k), now=lambda: 10000)

    def test_arbitrary_filename_date_from_createdtime_and_matter_spans(self):
        a, r, _ = install(self.d)
        self.p.process_transcript(a, r, self.targets)
        folders = [x for x in self.d.items.values() if o.props(x).get('orpheus_kind') == 'lesson_folder']
        self.assertEqual({x['name'] for x in folders}, {'1_biologia_2026-11-07', '1_chimica_2026-11-07'})
        files = [x for x in self.d.items.values() if o.props(x).get('orpheus_kind') == 'lesson_txt']
        self.assertEqual({self.d.read(x['id']) for x in files}, {''.join(LINES[:2]).encode(), LINES[2].encode()})
        self.assertEqual(self.d.read(r['id']), RAW)
        self.assertEqual(self.d.get(a['id'])['parents'], ['completed'])
        self.assertEqual(self.d.get(r['id'])['parents'], ['processed'])

    def test_unsupported_skipped(self):
        a, r, _ = install(self.d, segs=[seg(1, 1, 'biologia'), seg(2, 2, 'unsupported', 'unsupported'), seg(3, 3, 'chimica')])
        self.p.process_transcript(a, r, self.targets)
        self.assertEqual({o.props(x).get('orpheus_segment_key') for x in self.d.items.values() if o.props(x).get('orpheus_kind') == 'lesson_folder'}, {'1:1', '3:3'})

    def test_uncertain_holds_all_routing_and_completion(self):
        a, r, _ = install(self.d, segs=[seg(1, 2, None, 'uncertain', 0.4, 0.02), seg(3, 3, 'chimica')])
        with self.assertRaises(o.ReviewRequired):
            self.p.process_transcript(a, r, self.targets)
        self.assertFalse(any((o.props(x).get('orpheus_kind') == 'lesson_folder' for x in self.d.items.values())))
        self.assertEqual(self.d.get(a['id'])['parents'], ['processing'])

    def test_missing_plan_waits_while_running_or_unknown_then_reviews_when_complete(self):
        a, r, _ = install(self.d)
        self.d.items.pop('plan_audio0')
        self.d.contents.pop('plan_audio0')
        self.k.state = 'running'
        self.p.cycle()
        self.assertNotEqual(o.props(self.d.get(a['id'])).get('orpheus_status'), 'review')
        self.k.state = RuntimeError('unknown')
        self.p.cycle()
        self.assertNotEqual(o.props(self.d.get(a['id'])).get('orpheus_status'), 'review')
        self.k.state = 'complete'
        self.p.cycle()
        self.assertEqual(o.props(self.d.get(a['id'])).get('orpheus_status'), 'review')

    def test_duplicate_sidecar_review(self):
        a, r, _ = install(self.d, duplicate=2)
        with self.assertRaises(o.ReviewRequired):
            self.p.process_transcript(a, r, self.targets)

    def test_invalid_createdtime_fails_closed(self):
        a, r, _ = install(self.d, created='not a date')
        with self.assertRaises(o.ReviewRequired):
            self.p.process_transcript(a, r, self.targets)

    def test_mismatched_plan_contract_and_coverage_review(self):
        for i, change in enumerate(({'raw_id': 'wrong'}, {'raw_sha256': '0' * 64}, {'version': 2}, {'source_model': 'wrong'}, {'window_seconds': 30}, {'segments': [seg(1, 1, 'biologia'), seg(3, 3, 'chimica')]})):
            d = FakeDrive()
            targets = tree(d)
            a, r, _ = install(d, 'x' + str(i), segs=[seg(1, 2, 'biologia'), seg(3, 3, 'chimica')], overrides=change)
            with self.subTest(change=change), self.assertRaises(o.ReviewRequired):
                o.Pipeline(cfg(), Services(d, FakeKaggle())).process_transcript(a, r, targets)

    def test_sidecar_hash_and_raw_hash_mismatch(self):
        a, r, _ = install(self.d)
        self.d.items['plan_audio0']['appProperties']['orpheus_plan_sha256'] = '0' * 64
        with self.assertRaises(o.ReviewRequired):
            self.p.process_transcript(a, r, self.targets)
        self.d.items['plan_audio0']['appProperties']['orpheus_plan_sha256'] = o.digest(self.d.read('plan_audio0'))
        self.d.contents[r['id']] = b'[00:00 - 00:01] changed\n'
        with self.assertRaises(o.ReviewRequired):
            self.p.process_transcript(a, r, self.targets)

    def test_missing_createdtime(self):
        a, r, _ = install(self.d)
        self.d.items[a['id']].pop('createdTime')
        with self.assertRaises(o.ReviewRequired):
            self.p.process_transcript(self.d.get(a['id']), r, self.targets)

class TestOther(unittest.TestCase):

    def test_config_has_no_gemini_settings(self):
        e = dict(FOLDER_AUDIO_INCOMING='a', FOLDER_PROCESSING_KAGGLE='b', FOLDER_TRASCRIZIONI='c', FOLDER_TRASCRIZIONI_ELABORATE='d', FOLDER_AUDIO_COMPLETATI='e', FOLDER_ERRORI='f', FOLDER_RADICE_MATERIE='g', KAGGLE_KERNEL_SLUG='h')
        c = o.Config.from_env(e)
        self.assertFalse(hasattr(c, 'model'))
        self.assertFalse(hasattr(c, 'classifier_mode'))
        self.assertFalse(hasattr(c, 'allow_gemini'))

    def test_master_byte_preservation(self):
        self.assertEqual(''.join(o.transcript_lines(RAW)).encode(), RAW)

    def test_master_accepts_leading_spaces_and_internal_newlines(self):
        raw = b'[00:00:00 - 00:00:03]  prima frase \ncontinuazione\n\n[00:00:04 - 00:00:06] finale\n'
        self.assertEqual(''.join(o.transcript_lines(raw)).encode(), raw)
        with self.assertRaises(o.ReviewRequired):
            o.transcript_lines(b'[00:00 - 00:01]   \n')

    def test_folder_mapping(self):
        d = FakeDrive()
        expected = tree(d)
        self.assertEqual(o.build_subject_map(d, cfg()), expected)

    def test_folder_mapping_rejects_wrong_parent_or_missing_existing_folder(self):
        d = FakeDrive()
        tree(d)
        d.items['sub_biologia']['parents'] = ['root']
        with self.assertRaises(o.ReviewRequired):
            o.build_subject_map(d, cfg())

        d = FakeDrive()
        tree(d)
        d.items.pop('book_biologia')
        with self.assertRaises(o.ReviewRequired):
            o.build_subject_map(d, cfg())
if __name__ == '__main__':
    unittest.main()
