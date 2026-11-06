"""Drive -> Kaggle transcript + classifier sidecar -> routed lesson files."""
from __future__ import annotations
import hashlib, io, json, logging, os, re, time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
LOG = logging.getLogger('orchestratore')
FOLDER_MIME = 'application/vnd.google-apps.folder'
SUBJECTS = ('biologia', 'chimica', 'fisica', 'inglese', 'letteratura', 'purgatorio')
DRIVE_KW = {'supportsAllDrives': True}
LIST_KW = {'supportsAllDrives': True, 'includeItemsFromAllDrives': True, 'corpora': 'allDrives'}
FIELDS = 'id,name,mimeType,parents,trashed,appProperties,createdTime'
LINE_RE = re.compile('^\\[(\\d{2,}:\\d{2}(?::\\d{2})?) - (\\d{2,}:\\d{2}(?::\\d{2})?)\\] (.*)$')
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.flac', '.ogg', '.opus', '.aac', '.webm', '.mp4', '.amr', '.aiff'}
SOURCE_MODEL = 'MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7'

class ReviewRequired(Exception):
    pass

class WaitRequired(Exception):
    pass

@dataclass(frozen=True)
class Config:
    incoming: str
    processing: str
    transcripts: str
    processed: str
    completed: str
    errors: str
    root: str
    kernel_slug: str
    kernel_dir: str = 'kaggle_kernel'
    subject_ids: str = ''
    worker_verified: bool = False
    max_attempts: int = 3
    retry_after: int = 1800

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env

        def req(k):
            v = e.get(k, '').strip()
            if not v:
                raise ValueError('Configurazione mancante: ' + k)
            return v
        n = int(e.get('MAX_ATTEMPTS', '3'))
        d = int(e.get('RETRY_AFTER_SEC', '1800'))
        if n < 1 or d < 1:
            raise ValueError('MAX_ATTEMPTS e RETRY_AFTER_SEC devono essere positivi')
        c = cls(req('FOLDER_AUDIO_INCOMING'), req('FOLDER_PROCESSING_KAGGLE'), req('FOLDER_TRASCRIZIONI'), req('FOLDER_TRASCRIZIONI_ELABORATE'), req('FOLDER_AUDIO_COMPLETATI'), req('FOLDER_ERRORI'), req('FOLDER_RADICE_MATERIE'), req('KAGGLE_KERNEL_SLUG'), e.get('KAGGLE_KERNEL_DIR', 'kaggle_kernel'), e.get('MATERIE_FOLDER_IDS', ''), e.get('ORPHEUS_WORKER_CONTRACT_VERIFIED') == '1', n, d)
        if len({c.incoming, c.processing, c.transcripts, c.processed, c.completed, c.errors, c.root}) != 7:
            raise ValueError('Le cartelle pipeline devono avere ID distinti')
        return c

class Drive:

    def __init__(self, service):
        self.service = service

    @classmethod
    def from_env(cls):
        from googleapiclient.discovery import build
        scopes = ['https://www.googleapis.com/auth/drive']
        if os.environ.get('GOOGLE_AUTHORIZED_USER_JSON'):
            from google.oauth2.credentials import Credentials
            info = json.loads(os.environ['GOOGLE_AUTHORIZED_USER_JSON'])
            if info.get('type') != 'authorized_user':
                raise ValueError('Credenziali authorized_user non valide')
            creds = Credentials.from_authorized_user_info(info, scopes=scopes)
        elif os.environ.get('GOOGLE_SERVICE_ACCOUNT_KEY'):
            from google.oauth2 import service_account
            info = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_KEY'])
            if info.get('type') != 'service_account':
                raise ValueError('Credenziali service_account non valide')
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        else:
            raise ValueError('Credenziali Drive mancanti')
        return cls(build('drive', 'v3', credentials=creds, cache_discovery=False))

    def list(self, parent):
        p = parent.replace('\\', '\\\\').replace("'", "\\'")
        out = []
        token = None
        seen_tokens = set()
        while True:
            r = self.service.files().list(q=f"'{p}' in parents and trashed=false", fields=f'nextPageToken,incompleteSearch,files({FIELDS})', pageSize=200, pageToken=token, **LIST_KW).execute(num_retries=4)
            if r.get('incompleteSearch'):
                raise ReviewRequired('Ricerca Drive incompleta')
            out.extend(r.get('files', []))
            token = r.get('nextPageToken')
            if not token:
                return out
            if token in seen_tokens:
                raise ReviewRequired('Token di paginazione Drive ripetuto')
            seen_tokens.add(token)

    def get(self, fid):
        return self.service.files().get(fileId=fid, fields=FIELDS, **DRIVE_KW).execute(num_retries=4)

    def set_props(self, fid, updates):
        p = self.get(fid).get('appProperties') or {}
        self.service.files().update(fileId=fid, body={'appProperties': {**p, **updates}}, fields='id', **DRIVE_KW).execute(num_retries=4)

    def move(self, fid, parent):
        x = self.get(fid)
        old = x.get('parents') or []
        if old == [parent]:
            return
        if not old or parent in old:
            raise ReviewRequired('Parent Drive ambiguo')
        self.service.files().update(fileId=fid, addParents=parent, removeParents=','.join(old), fields='id', **DRIVE_KW).execute(num_retries=4)
        if self.get(fid).get('parents') != [parent]:
            raise ReviewRequired('Spostamento non verificato')

    def read(self, fid):
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, self.service.files().get_media(fileId=fid, **DRIVE_KW))
        done = False
        while not done:
            _, done = dl.next_chunk(num_retries=4)
        return buf.getvalue()

    def create_folder(self, parent, name, p):
        return self.service.files().create(body={'name': name, 'parents': [parent], 'mimeType': FOLDER_MIME, 'appProperties': p}, fields=FIELDS, **DRIVE_KW).execute(num_retries=0)

    def create_text(self, parent, name, data, p):
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype='text/plain', resumable=False)
        return self.service.files().create(body={'name': name, 'parents': [parent], 'mimeType': 'text/plain', 'appProperties': p}, media_body=media, fields=FIELDS, **DRIVE_KW).execute(num_retries=0)

class Services:

    def __init__(self, cfg):
        self.cfg = cfg
        self._drive = self._kaggle = None

    def drive(self):
        if self._drive is None:
            self._drive = Drive.from_env()
        return self._drive

    def kaggle(self):
        if self._kaggle is None:
            from kaggle.api.kaggle_api_extended import KaggleApi
            self._kaggle = KaggleApi()
            self._kaggle.authenticate()
        return self._kaggle

def props(x):
    return x.get('appProperties') or {}

def is_audio(x):
    m = (x.get('mimeType') or '').lower()
    e = os.path.splitext(x.get('name', ''))[1].lower()
    return m.startswith('audio/') or (m in ('application/octet-stream', 'video/mp4', 'video/webm') and e in AUDIO_EXTENSIONS)

def digest(x):
    return hashlib.sha256(x).hexdigest()

def canonical(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

def slugify(s):
    return re.sub('[^a-z0-9àèéìòù]+', '_', s.lower().strip()).strip('_')[:40].strip('_') or 'materia'

def one_folder(items, name):
    a = [x for x in items if x.get('mimeType') == FOLDER_MIME and x.get('name', '').casefold() == name.casefold() and (not x.get('trashed'))]
    if len(a) != 1:
        raise ReviewRequired('Cartella mancante o ambigua')
    return a[0]['id']

def build_subject_map(d, c):
    root = d.list(c.root)
    science = one_folder(root, 'scienze')
    italian = one_folder(root, 'italiano')
    expected_parents = {'biologia': science, 'chimica': science, 'fisica': c.root,
                        'inglese': c.root, 'letteratura': italian, 'purgatorio': italian}
    if c.subject_ids:
        try:
            m = json.loads(c.subject_ids)
        except Exception as e:
            raise ReviewRequired('Mapping non valido') from e
        if not isinstance(m, dict) or set(m) != set(SUBJECTS) or any((not isinstance(v, str) or not v.strip() for v in m.values())) or (len(set(m.values())) != 6):
            raise ReviewRequired('Mapping non valido')
    else:
        m = {'biologia': one_folder(d.list(science), 'biologia'),
             'chimica': one_folder(d.list(science), 'chimica'),
             'fisica': one_folder(root, 'fisica'),
             'inglese': one_folder(root, 'inglese'),
             'letteratura': one_folder(d.list(italian), 'letteratura'),
             'purgatorio': one_folder(d.list(italian), 'purgatorio')}
    out = {}
    for s, fid in m.items():
        f = d.get(fid)
        if (f.get('mimeType') != FOLDER_MIME or f.get('trashed')
                or f.get('name', '').casefold() != s
                or f.get('parents') != [expected_parents[s]]):
            raise ReviewRequired('Cartella materia non valida')
        children = d.list(fid)
        out[s] = one_folder(children, 'lezioni')
        one_folder(children, 'argomenti')
        one_folder(children, 'libro')
    if len(set(out.values())) != 6:
        raise ReviewRequired('Cartelle lezioni duplicate')
    return out

def timestamp_seconds(v):
    p = list(map(int, v.split(':')))
    if len(p) == 2 and p[1] < 60:
        return p[0] * 60 + p[1]
    if len(p) == 3 and p[1] < 60 and (p[2] < 60):
        return p[0] * 3600 + p[1] * 60 + p[2]
    raise ValueError()

def transcript_lines(raw):
    try:
        t = raw.decode('utf-8')
    except Exception as e:
        raise ReviewRequired('Trascrizione non UTF-8') from e
    lines = t.splitlines(keepends=True)
    if not lines or not t.strip():
        raise ReviewRequired('Trascrizione vuota')
    last = -1
    current_interval = None
    has_text = False
    for line in lines:
        m = LINE_RE.fullmatch(line.rstrip('\r\n'))
        if not m:
            if current_interval is None:
                raise ReviewRequired('Riga transcript non valida')
            has_text = has_text or bool(line.strip())
            continue
        try:
            a, b = (timestamp_seconds(m.group(1)), timestamp_seconds(m.group(2)))
        except ValueError as e:
            raise ReviewRequired('Timestamp non valido') from e
        if a < last or b < a:
            raise ReviewRequired('Timestamp non ordinato')
        last = a
        current_interval = (a, b)
        has_text = has_text or bool(m.group(3).strip())
    if not has_text:
        raise ReviewRequired('Trascrizione senza testo riconosciuto')
    return lines

def valid01(v):
    return type(v) in (int, float) and 0 <= v <= 1

def validate_plan(plan, lines, source, raw, raw_hash, side):
    p = props(side)
    expected = {'orpheus_source_id': source, 'orpheus_kind': 'subject_plan', 'orpheus_raw_id': raw['id'], 'orpheus_raw_sha256': raw_hash, 'orpheus_plan_sha256': digest(canonical(plan)), 'orpheus_classifier_version': '1', 'orpheus_original_name': props(raw).get('orpheus_original_name')}
    if any((p.get(k) != v for k, v in expected.items())):
        raise ReviewRequired('Provenienza/hash sidecar non valida')
    if not isinstance(plan, dict) or set(plan) != {'source_id', 'raw_id', 'raw_sha256', 'source_model', 'version', 'window_seconds', 'thresholds', 'segments'} or plan.get('source_id') != source or (plan.get('raw_id') != raw['id']) or (plan.get('raw_sha256') != raw_hash) or (plan.get('source_model') != SOURCE_MODEL) or (plan.get('version') != 1) or (plan.get('window_seconds') != 60) or (plan.get('thresholds') != {'min_score': 0.55, 'min_margin': 0.15}) or (not isinstance(plan.get('segments'), list)) or (not plan['segments']):
        raise ReviewRequired('Contratto piano non valido')
    next_line = 1
    for s in plan['segments']:
        if not isinstance(s, dict) or set(s) != {'start_line', 'end_line', 'materia', 'confidence', 'runner_up_margin', 'status'}:
            raise ReviewRequired('Schema segmento errato')
        a, b = (s['start_line'], s['end_line'])
        if type(a) is not int or type(b) is not int or a != next_line or (b < a) or (b > len(lines)):
            raise ReviewRequired('Copertura righe incompleta/overlap')
        next_line = b + 1
        mat = s['materia']
        st = s['status']
        conf = s['confidence']
        margin = s['runner_up_margin']
        if st not in ('classified', 'unsupported', 'uncertain') or (mat is not None and mat not in (*SUBJECTS, 'unsupported')) or (not valid01(conf)) or (not valid01(margin)):
            raise ReviewRequired('Valori segmento invalidi')
        if st == 'classified' and (mat not in SUBJECTS or conf < 0.55 or margin < 0.15):
            raise ReviewRequired('Classificazione sotto soglia')
        if st == 'unsupported' and (mat != 'unsupported' or conf < 0.55 or margin < 0.15):
            raise ReviewRequired('Unsupported non valido')
    if next_line != len(lines) + 1:
        raise ReviewRequired('Piano non copre il master')
    return plan['segments']

def kernel_state(k, slug):
    try:
        v = str(getattr(k.kernels_status(slug), 'status', '')).strip().lower().rsplit('.', 1)[-1]
    except Exception:
        return 'unknown'
    if v in {'running', 'queued', 'pending'}:
        return 'running'
    if v in {'failed', 'error', 'cancelled', 'canceled'}:
        return 'failed'
    if v in {'complete', 'completed'}:
        return 'complete'
    return 'unknown'

def int_prop(p, k, default=0):
    try:
        v = int(p.get(k, str(default)))
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default

class Pipeline:

    def __init__(self, cfg, services, now=time.time):
        self.cfg, self.services, self.now = (cfg, services, now)
        self.drive = services.drive()
        self.errors = 0

    def _mark_review(self, fid):
        self.drive.set_props(fid, {'orpheus_status': 'review'})
        LOG.warning('Revisione richiesta; file conservati.')

    def _find_kind(self, source, kind):
        found = []
        for folder in (self.cfg.transcripts, self.cfg.processed):
            found.extend((x for x in self.drive.list(folder) if props(x).get('orpheus_source_id') == source and props(x).get('orpheus_kind') == kind))
        if len(found) > 1:
            raise ReviewRequired('Più candidati per sorgente')
        return found[0] if found else None

    def _find_raw(self, s):
        return self._find_kind(s, 'raw_transcript')

    def _check_raw(self, a, r):
        original = props(a).get('orpheus_original_name', a.get('name'))
        p = props(r)
        if p.get('orpheus_kind') != 'raw_transcript' or p.get('orpheus_source_id') != a['id'] or p.get('orpheus_original_name') != original or (a.get('name') != original) or (not r.get('name', '').lower().endswith('.txt')) or (r.get('mimeType') == FOLDER_MIME):
            raise ReviewRequired('Metadati master non validi')

    def _date(self, a):
        try:
            d = datetime.fromisoformat(a['createdTime'].replace('Z', '+00:00'))
            if d.tzinfo is None:
                raise ValueError()
            return d.astimezone(ZoneInfo('Europe/Rome')).date().isoformat()
        except (KeyError, ValueError, TypeError, AttributeError) as e:
            raise ReviewRequired('createdTime mancante/non valido') from e

    def _plan(self, a, r, data, lines):
        side = self._find_kind(a['id'], 'subject_plan')
        if side is None:
            raise ReviewRequired('Sidecar assente')
        allowed_parents = {self.cfg.transcripts, self.cfg.processed}
        raw_parents = r.get('parents') or []
        side_parents = side.get('parents') or []
        if (len(raw_parents) != 1 or raw_parents[0] not in allowed_parents
                or len(side_parents) != 1 or side_parents[0] not in allowed_parents
                or side.get('mimeType') != 'application/json' or side.get('trashed')):
            raise ReviewRequired('Parent/MIME sidecar errato')
        rawhash = digest(data)
        payload = self.drive.read(side['id'])
        try:
            plan = json.loads(payload.decode('utf-8'))
            if canonical(plan) != payload:
                raise ValueError()
        except Exception as e:
            raise ReviewRequired('JSON piano non canonico') from e
        return (validate_plan(plan, lines, a['id'], r, rawhash, side), digest(payload))

    def _route(self, sid, segs, phash, lines, mapping, date):
        route = [s for s in segs if s['status'] == 'classified']
        inv = {s: self.drive.list(f) for s, f in mapping.items()}
        expected = {f'{s['start_line']}:{s['end_line']}': s['materia'] for s in route}
        for subject, files in inv.items():
            for f in files:
                p = props(f)
                if p.get('orpheus_source_id') == sid and p.get('orpheus_kind') == 'lesson_folder' and (p.get('orpheus_segment_key') not in expected or expected[p['orpheus_segment_key']] != subject):
                    raise ReviewRequired('Output preesistente inatteso')
        for s in route:
            subject = s['materia']
            key = f'{s['start_line']}:{s['end_line']}'
            found = [f for f in inv[subject] if props(f).get('orpheus_source_id') == sid and props(f).get('orpheus_segment_key') == key and (props(f).get('orpheus_kind') == 'lesson_folder')]
            if len(found) > 1:
                raise ReviewRequired('Cartelle duplicate')
            ident = {'orpheus_source_id': sid, 'orpheus_segment_key': key, 'orpheus_plan_hash': phash, 'orpheus_kind': 'lesson_folder'}
            suffix = f'_{subject}_{date}'
            if found:
                folder = found[0]
                if any((props(folder).get(k) != v for k, v in ident.items())):
                    raise ReviewRequired('Cartella non corrispondente')
                m = re.fullmatch('(\\d+)' + re.escape(suffix), folder.get('name', ''))
                if (not m or folder.get('mimeType') != FOLDER_MIME
                        or folder.get('parents') != [mapping[subject]] or folder.get('trashed')):
                    raise ReviewRequired('Nome cartella incoerente')
                number = int(m.group(1))
            else:
                nums = [int(m.group(1)) for f in inv[subject] if f.get('mimeType') == FOLDER_MIME and (m := re.match('^(\\d+)_', f.get('name', '')))]
                number = max(nums, default=0) + 1
                folder = self.drive.create_folder(mapping[subject], f'{number}{suffix}', ident)
                checked_folder = self.drive.get(folder['id'])
                if (checked_folder.get('mimeType') != FOLDER_MIME
                        or checked_folder.get('parents') != [mapping[subject]]
                        or checked_folder.get('name') != f'{number}{suffix}'
                        or checked_folder.get('trashed')
                        or any(props(checked_folder).get(k) != v for k, v in ident.items())):
                    raise ReviewRequired('Cartella lezione non verificata')
                folder = checked_folder
                inv[subject].append(folder)
            fidname = f'lezione{number}.txt'
            fileprops = {**ident, 'orpheus_kind': 'lesson_txt'}
            content = ''.join(lines[s['start_line'] - 1:s['end_line']]).encode('utf-8')
            children = self.drive.list(folder['id'])
            matches = [f for f in children if props(f).get('orpheus_source_id') == sid and props(f).get('orpheus_segment_key') == key]
            if len(matches) > 1:
                raise ReviewRequired('File lezione duplicati')
            if matches:
                f = matches[0]
            else:
                if any((c.get('name') == fidname for c in children)):
                    raise ReviewRequired('Nome lezione occupato')
                f = self.drive.create_text(folder['id'], fidname, content, fileprops)
            check = self.drive.get(f['id'])
            if check.get('parents') != [folder['id']] or check.get('name') != fidname or check.get('mimeType') != 'text/plain' or any((props(check).get(k) != v for k, v in fileprops.items())) or (self.drive.read(f['id']) != content):
                raise ReviewRequired('Lezione non verificata')
        final = {s: self.drive.list(folder) for s, folder in mapping.items()}
        for subject, files in final.items():
            actual = {p.get('orpheus_segment_key') for f in files
                      if (p := props(f)).get('orpheus_source_id') == sid
                      and p.get('orpheus_kind') == 'lesson_folder'}
            expected_for_subject = {key for key, target in expected.items() if target == subject}
            if actual != expected_for_subject:
                raise ReviewRequired('Verifica finale delle cartelle non riuscita')

    def process_transcript(self, a, r, mapping):
        self._check_raw(a, r)
        if props(a).get('orpheus_status') == 'review' or props(r).get('orpheus_status') == 'review':
            raise WaitRequired()
        date = self._date(a)
        data = self.drive.read(r['id'])
        lines = transcript_lines(data)
        segments, phash = self._plan(a, r, data, lines)
        if any((s['status'] == 'uncertain' or s['materia'] is None for s in segments)):
            raise ReviewRequired('Segmento incerto: routing hold')
        self._route(a['id'], segments, phash, lines, mapping, date)
        self.drive.set_props(r['id'], {'orpheus_route_done': '1'})
        if props(self.drive.get(r['id'])).get('orpheus_route_done') != '1':
            raise ReviewRequired('Marcatore di routing non persistito')
        self.drive.move(r['id'], self.cfg.processed)
        marker = {'orpheus_kind': 'completed_audio', 'orpheus_source_id': a['id'], 'orpheus_routing_complete': 'true'}
        self.drive.set_props(a['id'], marker)
        if any((props(self.drive.get(a['id'])).get(k) != v for k, v in marker.items())):
            raise ReviewRequired('Marcatore audio non persistito')
        self.drive.move(a['id'], self.cfg.completed)

    def _launch(self, a, attempt):
        self.drive.set_props(a['id'], {'orpheus_attempts': str(attempt), 'orpheus_started_at': str(int(self.now())), 'orpheus_launch_state': 'intent', 'orpheus_seen_running': '0', 'orpheus_original_name': props(a).get('orpheus_original_name', a['name'])})
        if self.drive.get(a['id']).get('parents') != [self.cfg.processing]:
            self.drive.move(a['id'], self.cfg.processing)
        try:
            self.services.kaggle().kernels_push(self.cfg.kernel_dir)
        except Exception as e:
            self.drive.set_props(a['id'], {'orpheus_launch_state': 'uncertain'})
            raise ReviewRequired('Push Kaggle incerta') from e
        self.drive.set_props(a['id'], {'orpheus_launch_state': 'launched'})

    def _retry_or_wait(self, a):
        p = props(self.drive.get(a['id']))
        state = kernel_state(self.services.kaggle(), self.cfg.kernel_slug)
        if state == 'unknown':
            return
        if state == 'running':
            if p.get('orpheus_launch_state') == 'launched':
                self.drive.set_props(a['id'], {'orpheus_seen_running': '1'})
            return
        if p.get('orpheus_launch_state') != 'launched' or p.get('orpheus_seen_running') != '1':
            return
        n = int_prop(p, 'orpheus_attempts')
        started = int_prop(p, 'orpheus_started_at')
        if not n or not started:
            raise ReviewRequired('Stato avvio incompleto')
        if int(self.now()) - started < self.cfg.retry_after * 2 ** min(n - 1, 6):
            return
        if n >= self.cfg.max_attempts:
            raise ReviewRequired('Tentativi esauriti')
        if self.cfg.worker_verified:
            self._launch(a, n + 1)

    def cycle(self):
        cfg = self.cfg
        for a in [*self.drive.list(cfg.processing), *self.drive.list(cfg.incoming)]:
            if not is_audio(a):
                continue
            try:
                raw = self._find_raw(a['id'])
                if raw:
                    self._check_raw(a, raw)
                    side = self._find_kind(a['id'], 'subject_plan')
                    if side is None:
                        state = kernel_state(self.services.kaggle(), cfg.kernel_slug)
                        if state in ('running', 'unknown'):
                            continue
                        raise ReviewRequired('Piano assente dopo completamento kernel')
                    self.process_transcript(a, raw, build_subject_map(self.drive, cfg))
            except WaitRequired:
                pass
            except ReviewRequired:
                self._mark_review(a['id'])
            except Exception as e:
                self.errors += 1
                LOG.error('Elaborazione (%s)', type(e).__name__)
        processing = self.drive.list(cfg.processing)
        if processing:
            if len(processing) != 1 or not is_audio(processing[0]):
                return self.errors
            a = processing[0]
            try:
                if self._find_raw(a['id']) is None:
                    self._retry_or_wait(a)
            except ReviewRequired:
                self._mark_review(a['id'])
            except Exception as e:
                self.errors += 1
                LOG.error('Recupero (%s)', type(e).__name__)
            return self.errors
        if not cfg.worker_verified:
            return self.errors
        try:
            if kernel_state(self.services.kaggle(), cfg.kernel_slug) not in ('complete', 'failed'):
                return self.errors
            for a in self.drive.list(cfg.incoming):
                if not is_audio(a) or props(a).get('orpheus_status') == 'review' or self._find_raw(a['id']):
                    continue
                if int_prop(props(a), 'orpheus_attempts') >= cfg.max_attempts:
                    self._mark_review(a['id'])
                    continue
                self._launch(a, int_prop(props(a), 'orpheus_attempts') + 1)
                break
        except Exception as e:
            self.errors += 1
            LOG.error('Avvio (%s)', type(e).__name__)
        return self.errors

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        cfg = Config.from_env()
        return 1 if Pipeline(cfg, Services(cfg)).cycle() else 0
    except Exception as e:
        LOG.error('Ciclo (%s)', type(e).__name__)
        return 1
if __name__ == '__main__':
    raise SystemExit(main())
