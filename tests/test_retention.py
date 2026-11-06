import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from scripts import retention as r


NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
PLAN = "a" * 64


def file(fid, mime, parent=None, props=None, created="2026-05-31T12:00:00Z"):
    value = {"id": fid, "mimeType": mime, "trashed": False, "version": "7",
             "createdTime": created, "capabilities": {"canTrash": True}}
    if parent is not None:
        value["parents"] = [parent]
    if props is not None:
        value["appProperties"] = props
    return value


def fixture():
    common = {"orpheus_source_id": "audio-1", "orpheus_segment_key": "1:23", "orpheus_plan_hash": PLAN}
    return {
        "lessons": file("lessons", r.FOLDER),
        "completed": file("completed", r.FOLDER),
        "lesson-folder": file("lesson-folder", r.FOLDER, "lessons", {**common, "orpheus_kind": "lesson_folder"}),
        "text-1": file("text-1", "text/plain", "lesson-folder", {**common, "orpheus_kind": "lesson_txt"}),
        "audio-1": file("audio-1", "audio/mpeg", "completed", {
            "orpheus_kind": "completed_audio", "orpheus_source_id": "audio-1",
            "orpheus_routing_complete": "true", "orpheus_review_status": "approved",
            "orpheus_results_verified": "true"}),
    }


class Request:
    def __init__(self, callback):
        self.callback = callback
        self.callbacks = []
        self.headers = {}
        self.retries = None

    def add_response_callback(self, callback):
        self.callbacks.append(callback)

    def execute(self, num_retries=0):
        self.retries = num_retries
        payload, headers = self.callback(self)
        for callback in self.callbacks:
            callback(headers)
        return payload


class FakeFiles:
    def __init__(self, items=None, page_size=100):
        self.items = copy.deepcopy(fixture() if items is None else items)
        self.page_size = page_size
        self.calls = []
        self.updates = []
        self.get_counts = {}
        self.on_get = None
        self.on_list = None
        self.etags = {}
        self.incomplete = False
        self.race_on_update = False

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        match = re.fullmatch(r"'([A-Za-z0-9_-]+)' in parents and trashed = false", kwargs["q"])
        if not match:
            raise AssertionError("query non limitata alla cartella")
        parent = match.group(1)
        def respond(_):
            if self.on_list:
                override = self.on_list(parent, kwargs)
                if override is not None:
                    return override, {}
            children = [copy.deepcopy(v) for v in self.items.values()
                        if v.get("parents") == [parent] and v.get("trashed") is False]
            pos = int(kwargs["pageToken"] or 0)
            result = {"files": children[pos:pos + self.page_size], "incompleteSearch": self.incomplete}
            if pos + self.page_size < len(children):
                result["nextPageToken"] = str(pos + self.page_size)
            return result, {}
        return Request(respond)

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        fid = kwargs["fileId"]
        def respond(_):
            self.get_counts[fid] = self.get_counts.get(fid, 0) + 1
            if self.on_get:
                self.on_get(fid, self.get_counts[fid], self)
            value = copy.deepcopy(self.items[fid])
            version = value.get("version")
            default_etag = '"v' + version + '"' if isinstance(version, str) else None
            etag = self.etags.get(fid, default_etag)
            return value, {} if etag is None else {"etag": etag}
        return Request(respond)

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        def respond(req):
            # Il fake pretende un unico update reversibile, senza retry né move.
            if (set(kwargs) != {"fileId", "body", "fields", "supportsAllDrives"}
                    or kwargs["body"] != {"trashed": True} or req.retries != 0):
                raise AssertionError("update non sicuro")
            fid = kwargs["fileId"]
            etag = self.etags.get(fid, '"v' + self.items[fid]["version"] + '"')
            if self.race_on_update or req.headers.get("If-Match") != etag:
                raise RuntimeError("precondition failed")
            self.updates.append((copy.deepcopy(kwargs), copy.deepcopy(req.headers)))
            self.items[fid]["trashed"] = True
            return {"id": fid, "trashed": True}, {}
        return Request(respond)

    def delete(self, **kwargs):
        raise AssertionError("La cancellazione permanente non deve mai essere chiamata")


class FakeService:
    def __init__(self, items=None, page_size=100):
        self.api = FakeFiles(items, page_size)

    def files(self):
        return self.api


def run(service, apply=False, lessons=None, completed="completed"):
    out = io.StringIO()
    result = r.run(service, ["lessons"] if lessons is None else lessons, completed,
                   apply=apply, exclusive=apply, now=NOW, out=out)
    return result, out.getvalue()


class RetentionTests(unittest.TestCase):
    def test_default_is_dry_run_and_double_proof(self):
        service = FakeService(page_size=1)
        result, output = run(service)
        self.assertEqual((result.candidates, result.trashed, result.changed), (2, 0, 0))
        self.assertIn("DRY RUN", output)
        self.assertIn("lesson_txt id=text-1", output)
        self.assertIn("completed_audio id=audio-1", output)
        self.assertEqual(service.api.updates, [])
        self.assertGreaterEqual(service.api.get_counts["text-1"], 2)
        self.assertGreaterEqual(service.api.get_counts["audio-1"], 2)
        self.assertTrue(all(x.get("trashed") is False for x in service.api.items.values()))

    def test_apply_needs_exclusive_and_does_not_initialize_credentials(self):
        service = FakeService()
        with self.assertRaises(r.SafetyError):
            r.run(service, ["lessons"], "completed", apply=True, now=NOW, out=io.StringIO())
        self.assertEqual(service.api.calls, [])
        with patch.object(r, "create_service") as factory, patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(r.main(["--lesson-root", "lessons", "--apply"]), 2)
        factory.assert_not_called()

    def test_apply_only_two_precisely_conditioned_reversible_updates(self):
        service = FakeService()
        result, _ = run(service, apply=True)
        self.assertEqual((result.candidates, result.trashed, result.changed), (2, 2, 0))
        self.assertEqual({u[0]["fileId"] for u in service.api.updates}, {"text-1", "audio-1"})
        self.assertTrue(all(u[0]["body"] == {"trashed": True} for u in service.api.updates))
        self.assertTrue(all(u[1]["If-Match"] == '"v7"' for u in service.api.updates))
        self.assertFalse(service.api.items["lesson-folder"]["trashed"])
        self.assertFalse(service.api.items["lessons"]["trashed"])
        self.assertFalse(service.api.items["completed"]["trashed"])
        self.assertNotIn("delete", [call[0] for call in service.api.calls])

    def test_current_orchestrator_kinds_and_missing_audio_markers_are_noop(self):
        items = fixture()
        items["lesson-folder"]["appProperties"]["orpheus_kind"] = "lesson"
        items["text-1"]["appProperties"]["orpheus_kind"] = "lesson_text"
        items["audio-1"]["appProperties"] = {"orpheus_launch_state": "launched"}
        service = FakeService(items)
        result, output = run(service, apply=True)
        self.assertEqual(result.candidates, 0)
        self.assertEqual(service.api.updates, [])
        self.assertIn("revisione approvata", output)

    def test_every_required_audio_marker_and_type_fails_closed(self):
        props = fixture()["audio-1"]["appProperties"]
        for key, value in list(props.items()):
            for replacement in (None, "invalid"):
                with self.subTest(key=key, replacement=replacement):
                    items = fixture()
                    if replacement is None:
                        del items["audio-1"]["appProperties"][key]
                    else:
                        items["audio-1"]["appProperties"][key] = replacement
                    service = FakeService(items)
                    result, _ = run(service, apply=True, lessons=[])
                    self.assertEqual(result.trashed, 0)
                    self.assertEqual(service.api.updates, [])
        for mime in (r.FOLDER, "text/plain", "application/octet-stream", "application/vnd.google-apps.shortcut"):
            items = fixture(); items["audio-1"]["mimeType"] = mime
            service = FakeService(items)
            self.assertEqual(run(service, apply=True, lessons=[])[0].trashed, 0)
        items = fixture(); items["audio-1"]["appProperties"]["orpheus_status"] = "review"
        service = FakeService(items)
        self.assertEqual(run(service, apply=True, lessons=[])[0].trashed, 0)

    def test_each_lesson_marker_is_mandatory_and_must_match(self):
        keys = ("orpheus_kind", "orpheus_source_id", "orpheus_segment_key", "orpheus_plan_hash")
        for fid in ("lesson-folder", "text-1"):
            for key in keys:
                for mutation in ("missing", "other"):
                    with self.subTest(fid=fid, key=key, mutation=mutation):
                        items = fixture()
                        if mutation == "missing":
                            del items[fid]["appProperties"][key]
                        elif key == "orpheus_plan_hash":
                            items[fid]["appProperties"][key] = "b" * 64
                        elif key == "orpheus_segment_key":
                            items[fid]["appProperties"][key] = "1:24"
                        else:
                            items[fid]["appProperties"][key] = "other"
                        service = FakeService(items)
                        self.assertEqual(run(service, apply=True, completed=None)[0].trashed, 0)
                        self.assertEqual(service.api.updates, [])
        for bad in ("1:0", "-1:2", "01:2", "no", "0:" + "9" * 50):
            items = fixture()
            items["lesson-folder"]["appProperties"]["orpheus_segment_key"] = bad
            items["text-1"]["appProperties"]["orpheus_segment_key"] = bad
            self.assertEqual(run(FakeService(items), completed=None)[0].candidates, 0)

    def test_created_time_30_days_exact_and_missing_invalid_or_future(self):
        cases = [
            ("2026-05-31T12:00:00Z", True),
            ("2026-05-31T14:00:00+02:00", True),
            ("2026-05-31T12:00:00.000001Z", False),
            ("2026-06-01T12:00:00Z", False),
            ("2026-07-01T12:00:00Z", False),
            ("2026-05-31T12:00:00", False),
            ("2026-05-99T12:00:00Z", False),
            ("not-a-date", False), (None, False),
        ]
        for created, allowed in cases:
            with self.subTest(created=created):
                items = fixture()
                items["text-1"]["createdTime"] = created
                items["text-1"]["name"] = "nome_inattendibile_1900.txt"
                service = FakeService(items)
                result, output = run(service, apply=True, completed=None)
                self.assertEqual(result.trashed, int(allowed))
                self.assertNotIn("nome_inattendibile", output)

    def test_non_lesson_raw_unmarked_wrong_mime_and_review_are_excluded(self):
        items = fixture()
        common = copy.deepcopy(items["text-1"]["appProperties"])
        items["raw"] = file("raw", "text/plain", "lesson-folder", {**common, "orpheus_kind": "raw_transcript"})
        items["unmarked"] = file("unmarked", "text/plain", "lesson-folder")
        items["shortcut"] = file("shortcut", "application/vnd.google-apps.shortcut", "lesson-folder", common)
        items["folder-child"] = file("folder-child", r.FOLDER, "lesson-folder", common)
        items["review"] = file("review", "text/plain", "lesson-folder", {**common, "orpheus_status": "review"})
        items["unknown"] = file("unknown", "text/plain", "lesson-folder", {**common, "orpheus_status": "failed"})
        items["missing-cap"] = file("missing-cap", "text/plain", "lesson-folder", common)
        del items["missing-cap"]["capabilities"]
        service = FakeService(items)
        result, _ = run(service, apply=True, completed=None)
        self.assertEqual(result.trashed, 1)
        self.assertEqual(service.api.updates[0][0]["fileId"], "text-1")

    def test_location_and_generated_folder_restrictions(self):
        items = fixture()
        common = copy.deepcopy(items["text-1"]["appProperties"])
        items["raw-folder"] = file("raw-folder", r.FOLDER, "lessons", {"orpheus_kind": "raw_transcript"})
        items["inside-raw"] = file("inside-raw", "text/plain", "raw-folder", common)
        items["outside"] = file("outside", "text/plain", "another-folder", common)
        items["direct"] = file("direct", "text/plain", "lessons", common)
        items["other-audio"] = file("other-audio", "audio/mpeg", "elsewhere", items["audio-1"]["appProperties"])
        service = FakeService(items)
        result, _ = run(service, apply=True)
        self.assertEqual(result.trashed, 2)
        self.assertEqual({u[0]["fileId"] for u in service.api.updates}, {"text-1", "audio-1"})
        queries = [kw["q"] for method, kw in service.api.calls if method == "list"]
        self.assertEqual(set(queries), {"'lessons' in parents and trashed = false", "'lesson-folder' in parents and trashed = false", "'completed' in parents and trashed = false"})

    def test_second_proof_detects_changes_in_file_folder_and_root(self):
        for fid in ("text-1", "lesson-folder", "lessons"):
            with self.subTest(fid=fid):
                service = FakeService()
                # The root has an initial get during discovery; other items don't.
                change_at = 3 if fid == "lessons" else 2
                def change(current, count, api):
                    if current == fid and count == change_at:
                        api.items[fid]["version"] = "8"
                service.api.on_get = change
                result, _ = run(service, apply=True, completed=None)
                self.assertEqual((result.trashed, result.changed), (0, 1))
                self.assertEqual(service.api.updates, [])

    def test_etag_absent_weak_or_changed_means_no_update(self):
        for etag in (None, 'W/"v7"', "*", "unquoted", '"bad\nheader"'):
            with self.subTest(etag=etag):
                service = FakeService()
                service.api.etags["text-1"] = etag
                result, _ = run(service, apply=True, completed=None)
                self.assertEqual((result.trashed, result.changed), (0, 1))
                self.assertEqual(service.api.updates, [])
        service = FakeService()
        def change_etag(fid, count, api):
            if fid == "text-1" and count == 2:
                api.etags[fid] = '"different"'
        service.api.on_get = change_etag
        self.assertEqual(run(service, apply=True, completed=None)[0].trashed, 0)

    def test_precondition_race_has_no_retry(self):
        service = FakeService(); service.api.race_on_update = True
        with self.assertRaises(RuntimeError):
            run(service, apply=True, completed=None)
        self.assertEqual(service.api.updates, [])
        self.assertEqual(len([c for c in service.api.calls if c[0] == "update"]), 1)

    def test_pagination_scoped_and_incomplete_search_never_updates(self):
        items = fixture()
        items["another"] = file("another", "text/plain", "lesson-folder")
        service = FakeService(items, page_size=1)
        run(service, apply=True)
        lists = [kw for method, kw in service.api.calls if method == "list"]
        self.assertIn("1", [kw["pageToken"] for kw in lists])
        self.assertTrue(all(kw["supportsAllDrives"] and kw["includeItemsFromAllDrives"] for kw in lists))
        self.assertTrue(all(kw["corpora"] == "allDrives" for kw in lists))
        service = FakeService(items, page_size=1)
        def partial(parent, args):
            if parent == "completed":
                return {"files": [], "incompleteSearch": True}
        service.api.on_list = partial
        with self.assertRaises(r.SafetyError):
            run(service, apply=True)
        self.assertEqual(service.api.updates, [])

    def test_repeated_page_token_duplicate_id_and_invalid_response_abort_before_updates(self):
        for response in ({"files": [], "nextPageToken": "same"},
                         {"files": [fixture()["lesson-folder"], fixture()["lesson-folder"]]},
                         {"files": None}, {"files": [], "incompleteSearch": "unknown"}):
            with self.subTest(response=response):
                service = FakeService()
                service.api.on_list = lambda parent, args: response if parent == "lessons" else None
                with self.assertRaises(r.SafetyError):
                    run(service, apply=True)
                self.assertEqual(service.api.updates, [])

    def test_invalid_id_and_ambiguous_configuration_fail_before_api(self):
        for roots, completed in [([], None), (["root"], None), (["lessons' or true"], None),
                                 (["lessons", "lessons"], None), (["lessons"], "lessons"),
                                 ([""], None)]:
            service = FakeService()
            with self.assertRaises(r.SafetyError):
                r.run(service, roots, completed, now=NOW, out=io.StringIO())
            self.assertEqual(service.api.calls, [])

    def test_root_and_folder_metadata_must_be_complete(self):
        for fid, key in [("lessons", "version"), ("lessons", "trashed"),
                         ("lesson-folder", "version"), ("lesson-folder", "trashed"),
                         ("text-1", "version"), ("text-1", "trashed")]:
            with self.subTest(fid=fid, key=key):
                items = fixture(); del items[fid][key]
                service = FakeService(items)
                try:
                    result, _ = run(service, apply=True, completed=None)
                    self.assertEqual(result.trashed, 0)
                except r.SafetyError:
                    pass
                self.assertEqual(service.api.updates, [])

    def test_no_log_of_exception_filename_transcript_or_credentials(self):
        secret = "FAKE_SECRET_PRIVATE_TRANSCRIPT"
        items = fixture(); items["text-1"]["name"] = secret
        service = FakeService(items)
        _, output = run(service)
        self.assertNotIn(secret, output)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(r, "create_service", side_effect=RuntimeError(secret)), patch("sys.stdout", out), patch("sys.stderr", err):
            self.assertEqual(r.main(["--lesson-root", "lessons"]), 1)
        self.assertNotIn(secret, out.getvalue() + err.getvalue())

    def test_authorized_user_json_file_and_env_are_supported_offline(self):
        info = {"type": "authorized_user", "refresh_token": "FAKE_SECRET", "client_id": "fake", "client_secret": "fake"}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "auth.json"; path.write_text(json.dumps(info))
            with patch("google.oauth2.credentials.Credentials.from_authorized_user_info", return_value="fake-creds") as creds, \
                 patch("googleapiclient.discovery.build", return_value="fake-service") as build:
                self.assertEqual(r.create_service(str(path)), "fake-service")
                creds.assert_called_once_with(info, scopes=[r.SCOPE])
                build.assert_called_once_with("drive", "v3", credentials="fake-creds", cache_discovery=False)
            with patch.dict("os.environ", {"GOOGLE_AUTHORIZED_USER_JSON": json.dumps(info)}), \
                 patch("google.oauth2.credentials.Credentials.from_authorized_user_info", return_value="fake-creds"), \
                 patch("googleapiclient.discovery.build", return_value="fake-service"):
                self.assertEqual(r.create_service(None), "fake-service")
            path.write_text('{"type":"service_account"}')
            with self.assertRaises(r.SafetyError):
                r.create_service(str(path))


if __name__ == "__main__":
    unittest.main()
