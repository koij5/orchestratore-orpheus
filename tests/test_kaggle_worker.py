import io
import json
import logging
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from kaggle_kernel import trascrivi as w


class Request:
    def __init__(self, callback):
        self.callback = callback
        self.retries = None

    def execute(self, num_retries=0):
        self.retries = num_retries
        return self.callback()


class FakeFiles:
    def __init__(self, audio=(), transcripts=(), page_size=1):
        self.audio = list(audio)
        self.transcripts = list(transcripts)
        self.page_size = page_size
        self.list_calls = []
        self.create_calls = []
        self.incomplete = False

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        def respond():
            query = kwargs['q']
            # Questo fake restituisce tutti i transcript: il worker deve verificare
            # anche lato client che source_id e kind corrispondano esattamente.
            pool = self.transcripts if 'appProperties has' in query else self.audio
            pos = int(kwargs['pageToken'] or 0)
            page = pool[pos:pos + self.page_size]
            result = {'files': page, 'incompleteSearch': self.incomplete}
            if pos + self.page_size < len(pool):
                result['nextPageToken'] = str(pos + self.page_size)
            return result
        return Request(respond)

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        self.create_request = Request(lambda: {'id': 'created-id'})
        return self.create_request


class FakeDrive:
    def __init__(self, audio=(), transcripts=(), page_size=1):
        self.api = FakeFiles(audio, transcripts, page_size)

    def files(self):
        return self.api


def config(**kw):
    values = dict(credentials_path='/private/credentials.json',
                  processing_folder_id='processing', transcripts_folder_id='output')
    values.update(kw)
    return w.Config(**values)


def audio(source='id-1', name='lezione.mp3', mime='audio/mpeg'):
    return {'id': source, 'name': name, 'mimeType': mime}


def transcript(source='id-1', kind=w.KIND):
    return {'id': 'remote', 'appProperties': {w.SOURCE_KEY: source, w.KIND_KEY: kind}}


class ConfigurationTests(unittest.TestCase):
    def test_config_defaults_and_environment_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.json'
            p.write_text(json.dumps({'credentials_path': '/private/key.json',
                                     'processing_folder_id': 'p',
                                     'transcripts_folder_id': 't'}))
            with patch.dict(os.environ, {w.CONFIG_ENV: str(p)}):
                cfg = w.load_config()
            self.assertEqual(cfg.language, 'it')
            self.assertTrue(cfg.require_gpu)
            self.assertTrue(cfg.bootstrap_dependencies)
            self.assertEqual(cfg.device, 'cuda')
            self.assertIsNone(cfg.shared_drive_id)

    def test_config_rejects_bad_and_unknown_values(self):
        valid = {'credentials_path': '/private/key.json',
                 'processing_folder_id': 'p', 'transcripts_folder_id': 't'}
        cases = [
            {'credentials_path': 'relative.json'}, {'processing_folder_id': ''},
            {'transcripts_folder_id': 'p'}, {'require_gpu': 'false'},
            {'bootstrap_dependencies': 1}, {'shared_drive_id': ''},
            {'language': 123}, {'device': 'gpu'}, {'device': 'cpu'},
            {'secret_in_wrong_place': 'never log'},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'config.json'
            for case in cases:
                with self.subTest(case=case):
                    p.write_text(json.dumps({**valid, **case}))
                    with self.assertRaises(ValueError):
                        w.load_config(str(p))

    def test_gpu_required_and_cpu_fallback(self):
        with self.assertRaises(RuntimeError):
            w.select_device(config(), False)
        with self.assertRaises(RuntimeError):
            w.select_device(config(device='cuda', require_gpu=False), False)
        self.assertEqual(w.select_device(config(device='auto', require_gpu=False), False), 'cpu')
        self.assertEqual(w.select_device(config(device='auto', require_gpu=True), True), 'cuda')
        self.assertEqual(w.select_device(config(device='cpu', require_gpu=False), True), 'cpu')

    def test_bootstrap_is_explicit_and_has_no_secrets_in_arguments(self):
        def importer(name):
            if name == 'whisper':
                raise ImportError('not installed')
            return object()
        with patch.object(w.importlib, 'import_module', side_effect=importer):
            with self.assertRaises(RuntimeError):
                w.ensure_dependencies(False)
        with patch.object(w.importlib, 'import_module', side_effect=[object(), object(), ImportError(), object(), object(), object()]):
            with patch.object(w.subprocess, 'run') as install:
                w.ensure_dependencies(True)
        args, kwargs = install.call_args
        self.assertIn('openai-whisper==20240930', args[0])
        self.assertEqual(kwargs['stdout'], w.subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], w.subprocess.DEVNULL)
        self.assertTrue(kwargs['check'])

    def test_authorized_user_and_service_account_credentials(self):
        google = types.ModuleType('google'); google.__path__ = []
        oauth = types.ModuleType('google.oauth2'); oauth.__path__ = []
        user = types.ModuleType('google.oauth2.credentials')
        sa = types.ModuleType('google.oauth2.service_account')
        user.Credentials = MagicMock(); sa.Credentials = MagicMock()
        oauth.credentials = user; oauth.service_account = sa; google.oauth2 = oauth
        gap = types.ModuleType('googleapiclient'); gap.__path__ = []
        discovery = types.ModuleType('googleapiclient.discovery')
        discovery.build = MagicMock(return_value='fake-drive')
        gap.discovery = discovery
        modules = {'google': google, 'google.oauth2': oauth, 'google.oauth2.credentials': user,
                   'google.oauth2.service_account': sa, 'googleapiclient': gap,
                   'googleapiclient.discovery': discovery}
        with tempfile.TemporaryDirectory() as d, patch.dict('sys.modules', modules):
            cred = Path(d) / 'credentials.json'
            cred.write_text('{"type":"authorized_user", "refresh_token":"FAKE_SECRET"}')
            self.assertEqual(w.create_drive(config(credentials_path=str(cred))), 'fake-drive')
            user.Credentials.from_authorized_user_file.assert_called_once_with(str(cred), scopes=[w.SCOPE])
            cred.write_text('{"type":"service_account"}')
            self.assertEqual(w.create_drive(config(credentials_path=str(cred))), 'fake-drive')
            sa.Credentials.from_service_account_file.assert_called_once_with(str(cred), scopes=[w.SCOPE])
            cred.write_text('{"type":"other"}')
            with self.assertRaises(ValueError):
                w.create_drive(config(credentials_path=str(cred)))
        self.assertFalse(discovery.build.call_args.kwargs['cache_discovery'])


class FormattingTests(unittest.TestCase):
    def test_timestamp_hours_and_text_not_trimmed(self):
        result = {'segments': [
            {'start': 3605.9, 'end': 3672.1, 'text': '  Testo originale  '},
            {'start': 360000, 'end': 360001, 'text': '\nSeconda riga\n'},
        ]}
        self.assertEqual(w.format_transcript(result),
                         '[01:00:05 - 01:01:12]   Testo originale  \n'
                         '[100:00:00 - 100:00:01] \nSeconda riga\n\n')

    def test_invalid_and_empty_whisper_results_are_not_success(self):
        cases = [None, {}, {'segments': []}, {'segments': [{'start': 1, 'end': 2, 'text': ' '}]},
                 {'segments': [{'start': -1, 'end': 2, 'text': 'x'}]},
                 {'segments': [{'start': 3, 'end': 2, 'text': 'x'}]},
                 {'segments': [{'start': float('nan'), 'end': 2, 'text': 'x'}]},
                 {'segments': [{'start': True, 'end': 2, 'text': 'x'}]},
                 {'segments': [{'start': 1, 'end': 2, 'text': None}]}]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ValueError):
                w.format_transcript(result)

    def test_audio_allowlist_requires_extension_and_mime(self):
        self.assertTrue(w.is_audio(audio(name='Lezione.MP3')))
        self.assertTrue(w.is_audio(audio(name='lezione.m4a', mime='application/octet-stream')))
        for name, mime in [('lezione.pdf', 'audio/mpeg'), ('lezione.mp3', 'application/pdf'),
                           ('lezione.mp4', 'video/mp4'), ('lezione', 'audio/mpeg'),
                           ('lezione.mp3', 'application/vnd.google-apps.shortcut')]:
            with self.subTest(name=name, mime=mime):
                self.assertFalse(w.is_audio(audio(name=name, mime=mime)))

    def test_path_traversal_and_app_property_byte_limits(self):
        for name in ('../segreto.mp3', 'x\\segreto.mp3', 'bad\n.mp3', '.mp3'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                w.validate_audio(audio(name=name))
        # Limit is bytes, not Unicode characters; never silently truncate the name.
        with self.assertRaises(ValueError):
            w.validate_audio(audio(name=('à' * 60) + '.mp3'))
        self.assertEqual(w.validate_audio(audio(name='lezione.mp3'))[1], 'lezione.mp3')


class DriveTests(unittest.TestCase):
    def test_pagination_includes_shared_drives_and_configured_corpus(self):
        drive = FakeDrive(audio=[audio('1'), audio('2'), audio('3')])
        found = w.list_drive_files(drive, "'processing' in parents", None)
        self.assertEqual([x['id'] for x in found], ['1', '2', '3'])
        self.assertEqual([c['pageToken'] for c in drive.api.list_calls], [None, '1', '2'])
        self.assertTrue(all(c['supportsAllDrives'] and c['includeItemsFromAllDrives'] for c in drive.api.list_calls))
        self.assertTrue(all(c['corpora'] == 'allDrives' for c in drive.api.list_calls))
        w.list_drive_files(drive, 'query', 'shared-id')
        self.assertEqual(drive.api.list_calls[-1]['corpora'], 'drive')
        self.assertEqual(drive.api.list_calls[-1]['driveId'], 'shared-id')

    def test_incomplete_search_fails_closed(self):
        drive = FakeDrive(); drive.api.incomplete = True
        with self.assertRaises(RuntimeError):
            w.existing_transcript(drive, 'id-1', None)

    def test_download_media_supports_shared_drives_and_multiple_chunks(self):
        http = types.ModuleType('googleapiclient.http')
        class Download:
            def __init__(self, stream, request):
                self.stream, self.request, self.count = stream, request, 0
            def next_chunk(self, num_retries):
                self.assert_retries = num_retries
                self.stream.write(b'chunk')
                self.count += 1
                return None, self.count == 2
        http.MediaIoBaseDownload = Download
        pkg = types.ModuleType('googleapiclient'); pkg.__path__ = []; pkg.http = http
        drive = MagicMock()
        with tempfile.TemporaryDirectory() as d, patch.dict(
                'sys.modules', {'googleapiclient': pkg, 'googleapiclient.http': http}):
            path = Path(d) / 'local.mp3'
            w.download_audio(drive, 'source-id', str(path))
            self.assertEqual(path.read_bytes(), b'chunkchunk')
        drive.files().get_media.assert_called_once_with(fileId='source-id', supportsAllDrives=True)

    def test_duplicate_lookup_global_paged_and_escaped(self):
        drive = FakeDrive(transcripts=[transcript('other'), transcript("a'b\\c")])
        self.assertTrue(w.existing_transcript(drive, "a'b\\c", None))
        query = drive.api.list_calls[0]['q']
        self.assertNotIn('in parents', query)
        self.assertIn("a\\'b\\\\c", query)
        self.assertEqual(len(drive.api.list_calls), 2)
        self.assertFalse(w.existing_transcript(drive, 'absent', None))
        self.assertFalse(w.existing_transcript(FakeDrive(transcripts=[transcript('id-1', 'other')]), 'id-1', None))

    def test_upload_metadata_and_no_automatic_create_retry(self):
        http = types.ModuleType('googleapiclient.http')
        class Media:
            def __init__(self, stream, **kwargs):
                self.contents = stream.read()
                self.kwargs = kwargs
        http.MediaIoBaseUpload = Media
        pkg = types.ModuleType('googleapiclient'); pkg.__path__ = []; pkg.http = http
        drive = FakeDrive()
        with patch.dict('sys.modules', {'googleapiclient': pkg, 'googleapiclient.http': http}):
            remote = w.upload_transcript(drive, config(), 'source-id', 'lezione.mp3', 'caffè')
        call = drive.api.create_calls[0]
        self.assertEqual(remote, 'created-id')
        self.assertEqual(call['body']['name'], 'lezione.txt')
        self.assertEqual(call['body']['parents'], ['output'])
        self.assertEqual(call['body']['appProperties'], {
            'orpheus_source_id': 'source-id', 'orpheus_kind': 'raw_transcript',
            'orpheus_original_name': 'lezione.mp3'})
        self.assertEqual(call['media_body'].contents, 'caffè'.encode())
        self.assertTrue(call['supportsAllDrives'])
        self.assertEqual(drive.api.create_request.retries, 0)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.model = MagicMock()
        self.model.device = 'cuda:0'
        self.model.transcribe.return_value = {'segments': [{'start': 0, 'end': 3, 'text': ' testo '}]}
        self.download_paths = []
        self.uploads = []

    def download(self, drive, file_id, path):
        self.download_paths.append(path)
        self.assertEqual(Path(path).parent.parent, Path(self.temp.name))
        Path(path).write_bytes(b'fake')

    def upload(self, drive, cfg, file_id, original_name, text):
        self.uploads.append((file_id, original_name, text))
        drive.api.transcripts.append(transcript(file_id))
        return 'fake-upload-id'

    def run_worker(self, drive, downloader=None, uploader=None, factory=None):
        return w.run_worker(drive, config(), factory or (lambda: self.model),
                            working_dir=self.temp.name, downloader=downloader or self.download,
                            uploader=uploader or self.upload)

    def test_process_once_preserves_text_and_temp_cleanup(self):
        drive = FakeDrive(audio=[audio(), audio('ignored', 'fake.pdf', 'application/pdf')])
        self.assertEqual(self.run_worker(drive), 0)
        self.assertEqual(self.uploads, [('id-1', 'lezione.mp3', '[00:00:00 - 00:00:03]  testo \n')])
        self.assertTrue(self.model.transcribe.call_args.kwargs['fp16'])
        self.assertFalse(Path(self.download_paths[0]).exists())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        # Transcript spostato o archiviato: lookup globale tramite source ID.
        self.assertEqual(self.run_worker(drive), 0)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(len(self.download_paths), 1)

    def test_same_name_different_source_ids_are_not_deduplicated(self):
        drive = FakeDrive(audio=[audio('source-1'), audio('source-2')])
        self.assertEqual(self.run_worker(drive), 0)
        self.assertEqual([x[0] for x in self.uploads], ['source-1', 'source-2'])

    def test_invalid_name_cannot_escape_or_replace_unrelated_file(self):
        sentinel = Path(self.temp.name) / 'segreto.mp3'
        sentinel.write_text('do not touch')
        drive = FakeDrive(audio=[audio(name='../segreto.mp3')])
        self.assertEqual(self.run_worker(drive), 1)
        self.assertEqual(sentinel.read_text(), 'do not touch')
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.download_paths, [])

    def test_failed_audio_does_not_upload_and_other_audio_can_succeed(self):
        drive = FakeDrive(audio=[audio('bad'), audio('good')])
        def failing_download(drive, fid, path):
            if fid == 'bad':
                Path(path).write_text('partial')
                raise RuntimeError('FAKE_SECRET_IN_EXCEPTION')
            self.download(drive, fid, path)
        with self.assertLogs(w.LOG, level='INFO') as logs:
            status = self.run_worker(drive, downloader=failing_download)
        self.assertEqual(status, 1)
        self.assertEqual([x[0] for x in self.uploads], ['good'])
        self.assertNotIn('FAKE_SECRET_IN_EXCEPTION', '\n'.join(logs.output))
        self.assertIn('download', '\n'.join(logs.output))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_failed_upload_returns_error_without_success_log_and_cleans_up(self):
        drive = FakeDrive(audio=[audio()])
        def fail(*args):
            raise RuntimeError('FAKE_SECRET_IN_EXCEPTION')
        with self.assertLogs(w.LOG, level='INFO') as logs:
            self.assertEqual(self.run_worker(drive, uploader=fail), 1)
        content = '\n'.join(logs.output)
        self.assertNotIn('Transcript grezzo caricato', content)
        self.assertNotIn('FAKE_SECRET_IN_EXCEPTION', content)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertEqual(len(drive.api.audio), 1)

    def test_existing_only_kind_and_id_skip_model(self):
        drive = FakeDrive(audio=[audio()], transcripts=[transcript('id-1')])
        factory = MagicMock(side_effect=AssertionError('model should not load'))
        self.assertEqual(self.run_worker(drive, factory=factory), 0)
        factory.assert_not_called()

    def test_duplicate_lookup_error_prevents_download_and_upload(self):
        drive = FakeDrive(audio=[audio()])
        original = drive.api.list
        def broken_lookup(**kwargs):
            if 'appProperties has' in kwargs['q']:
                raise RuntimeError('FAKE_SECRET')
            return original(**kwargs)
        drive.api.list = broken_lookup
        with self.assertLogs(w.LOG, level='INFO') as logs:
            self.assertEqual(self.run_worker(drive), 1)
        self.assertIn('verifica duplicati', '\n'.join(logs.output))
        self.assertNotIn('FAKE_SECRET', '\n'.join(logs.output))
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.download_paths, [])

    def test_bad_result_does_not_upload_and_does_not_delete_input(self):
        self.model.transcribe.return_value = {'segments': []}
        drive = FakeDrive(audio=[audio()])
        self.assertEqual(self.run_worker(drive), 1)
        self.assertEqual(self.uploads, [])
        self.assertEqual(len(drive.api.audio), 1)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_log_file_does_not_log_exception_or_filename(self):
        w.configure_logging(self.temp.name)
        drive = FakeDrive(audio=[audio(name='PRIVATE_FILENAME.mp3')])
        try:
            def fail(*args):
                raise RuntimeError('FAKE_SECRET_IN_EXCEPTION')
            self.assertEqual(self.run_worker(drive, downloader=fail), 1)
            for handler in w.LOG.handlers:
                handler.flush()
            contents = (Path(self.temp.name) / w.LOG_NAME).read_text()
            self.assertNotIn('FAKE_SECRET_IN_EXCEPTION', contents)
            self.assertNotIn('PRIVATE_FILENAME', contents)
        finally:
            for handler in w.LOG.handlers[:]:
                w.LOG.removeHandler(handler)
                handler.close()


class MetadataTests(unittest.TestCase):
    def test_metadata_script_and_security_flags(self):
        root = Path(__file__).resolve().parents[1]
        metadata = json.loads((root / 'kaggle_kernel/kernel-metadata.json').read_text())
        self.assertEqual(metadata['code_file'], 'trascrivi.py')
        self.assertEqual(metadata['kernel_type'], 'script')
        self.assertEqual(metadata['language'], 'python')
        self.assertTrue(metadata['is_private'])
        self.assertTrue(metadata['enable_gpu'])
        self.assertTrue(metadata['enable_internet'])
        self.assertEqual(metadata['dataset_sources'], ['replace-kaggle-username/orpheus-private-config'])


if __name__ == '__main__':
    unittest.main()
