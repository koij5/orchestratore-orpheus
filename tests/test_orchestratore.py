import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import orchestratore as o


class FakeDrive:
    def __init__(self):
        self.items, self.contents, self.counter = {}, {}, 0
        self.fail_text_after_create = self.fail_folder_after_create = False
        self.fail_move_to = None

    def add(self, fid, name, parent=None, mime=o.FOLDER_MIME, props=None, data=None):
        self.items[fid] = {"id": fid, "name": name, "mimeType": mime,
                           "parents": [parent] if parent else [], "appProperties": dict(props or {})}
        if data is not None: self.contents[fid] = data
        return copy.deepcopy(self.items[fid])

    def get(self, fid): return copy.deepcopy(self.items[fid])
    def list(self, parent): return [copy.deepcopy(f) for f in self.items.values() if parent in f["parents"] and not f.get("trashed")]
    def set_props(self, fid, updates): self.items[fid]["appProperties"].update(updates)
    def move(self, fid, parent):
        if self.fail_move_to == parent:
            self.fail_move_to = None
            raise TimeoutError()
        self.items[fid]["parents"] = [parent]
    def read(self, fid): return self.contents[fid]
    def create_folder(self, parent, name, props):
        self.counter += 1
        obj = self.add("created_" + str(self.counter), name, parent, props=props)
        if self.fail_folder_after_create:
            self.fail_folder_after_create = False
            raise TimeoutError()
        return obj
    def create_text(self, parent, name, data, props):
        self.counter += 1
        obj = self.add("created_" + str(self.counter), name, parent, "text/plain", props, data)
        if self.fail_text_after_create:
            self.fail_text_after_create = False
            raise TimeoutError()
        return obj


class FakeKaggle:
    def __init__(self, state="complete"):
        self.state, self.pushes, self.fail_push = state, [], False
    def kernels_status(self, slug):
        if isinstance(self.state, Exception): raise self.state
        return SimpleNamespace(status=self.state)
    def kernels_push(self, path):
        self.pushes.append(path)
        if self.fail_push: raise TimeoutError()


class FakeServices:
    def __init__(self, drive, kaggle=None): self.d, self.k, self.gemini_calls = drive, kaggle or FakeKaggle(), 0
    def drive(self): return self.d
    def kaggle(self): return self.k
    def gemini(self):
        self.gemini_calls += 1
        raise AssertionError("Gemini non deve essere invocato dai test")


def config(**kwargs):
    return replace(o.Config("incoming", "processing", "raw", "processed", "completed", "errors", "root", "user/kernel", worker_verified=True), **kwargs)


def make_tree(d):
    d.add("root", "root")
    for subject in o.DIRECT_SUBJECTS: d.add("subject_" + subject, subject, "root")
    d.add("italiano", "italiano", "root")
    for subject in ("letteratura", "purgatorio"): d.add("subject_" + subject, subject, "italiano")
    for subject in o.SUBJECTS: d.add("lessons_" + subject, "lezioni", "subject_" + subject)
    return {subject: "lessons_" + subject for subject in o.SUBJECTS}


AUDIO_NAME = "2026-11-02_08-00-00_biologia.mp3"
RAW = "[00:00 - 00:04] Prima riga: è originale.\r\n[00:04 - 00:08] Seconda riga!\n".encode()


def add_pair(d, audio_id="audio1", raw_id="raw1", name=AUDIO_NAME, parent="processing", data=RAW):
    audio = d.add(audio_id, name, parent, "audio/mpeg")
    raw = d.add(raw_id, "trascrizione.txt", "raw", "text/plain",
        {"orpheus_source_id": audio_id, "orpheus_kind": "raw_transcript", "orpheus_original_name": name}, data)
    return audio, raw


class TestValidation(unittest.TestCase):
    def test_date_and_subject_require_unambiguous_filename_suffix(self):
        self.assertEqual(o.recording_start(AUDIO_NAME), datetime(2026, 11, 2, 8))
        self.assertEqual(o.subject_from_name(AUDIO_NAME), "biologia")
        self.assertEqual(o.subject_from_name("2026-11-06_08-00_letteratura.m4a"), "letteratura")
        for name in ("2026-11-02.mp3", "lezione.mp3", "2026-99-02_08-00_biologia.mp3",
                     "2026-11-02_08-00.mp3", "2026-11-02_08-00_biologia_chimica.mp3",
                     "2026-11-02_08-00_italiano.mp3", "2026-11-02_08-00_biologia_giorno.mp3"):
            with self.subTest(name=name), self.assertRaises(o.ReviewRequired): o.subject_from_name(name)

    def test_timestamp_transcript_exact_bytes_and_invalid_data(self):
        lines, times = o.transcript_lines(RAW)
        self.assertEqual("".join(lines).encode(), RAW)
        self.assertEqual(times, [(0, 4), (4, 8)])
        for content in (b"", b"ordinary prose", b"[00:00 - 00:01] \n", b"[00:61 - 01:01] x\n", b"[00:02 - 00:01] x\n", b"\xff"):
            with self.subTest(content=content), self.assertRaises(o.ReviewRequired): o.transcript_lines(content)

    def test_audio_filter(self):
        self.assertTrue(o.is_audio({"name": "x.mp3", "mimeType": "audio/mpeg"}))
        self.assertTrue(o.is_audio({"name": "x.m4a", "mimeType": "application/octet-stream"}))
        self.assertFalse(o.is_audio({"name": "x.txt", "mimeType": "text/plain"}))

    def test_plan_encoding_corruption(self):
        plan = {"segments": [{"start_line": 0, "end_line": 1, "materia": "biologia", "argomento": "biologia"}]}
        encoded = o.encode_plan(plan)
        self.assertEqual(o.decode_plan(encoded), plan)
        encoded["orpheus_plan_hash"] = "0" * 64
        with self.assertRaises(o.ReviewRequired): o.decode_plan(encoded)


class TestMapping(unittest.TestCase):
    def setUp(self):
        self.d = FakeDrive()
        self.expected = make_tree(self.d)

    def test_exact_tree_and_no_arte_or_italiano_generic(self):
        self.assertEqual(o.build_subject_map(self.d, config()), self.expected)

    def test_similar_or_ambiguous_folders_are_review(self):
        self.d.add("decoy", "biologia extra", "root")
        self.assertEqual(o.build_subject_map(self.d, config()), self.expected)
        self.d.add("ambig", "BIOLOGIA", "root")
        with self.assertRaises(o.ReviewRequired): o.build_subject_map(self.d, config())

    def test_explicit_subject_map_and_missing_lessons(self):
        m = {s: "subject_" + s for s in o.SUBJECTS}
        self.assertEqual(o.build_subject_map(self.d, config(subject_ids=json.dumps(m))), self.expected)
        del self.d.items["lessons_biologia"]
        with self.assertRaises(o.ReviewRequired): o.build_subject_map(self.d, config())
        self.assertNotIn("lessons_biologia", self.d.items)


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.d = FakeDrive()
        self.targets = make_tree(self.d)
        self.k = FakeKaggle()
        self.svc = FakeServices(self.d, self.k)
        self.p = o.Pipeline(config(), self.svc, now=lambda: 10000)

    def process(self, audio, raw): return self.p.process_transcript(audio, raw, self.targets)
    def lesson_texts(self): return [f for f in self.d.items.values() if o.props(f).get("orpheus_kind") == "lesson_txt"]
    def lesson_folders(self): return [f for f in self.d.items.values() if o.props(f).get("orpheus_kind") == "lesson_folder"]

    def test_full_transcript_routed_by_filename_without_classifier(self):
        audio, raw = add_pair(self.d)
        with patch.object(o, "ask_gemini", side_effect=AssertionError("Gemini proibito")) as call:
            self.process(audio, raw)
            call.assert_not_called()
        self.assertEqual(self.svc.gemini_calls, 0)
        self.assertEqual(len(self.lesson_texts()), 1)
        text = self.lesson_texts()[0]
        self.assertEqual(self.d.read(text["id"]), RAW)
        self.assertEqual(self.d.get(audio["id"])["parents"], ["completed"])
        self.assertEqual(self.d.get(raw["id"])["parents"], ["processed"])
        props = o.props(self.d.get(audio["id"]))
        self.assertEqual(props["orpheus_kind"], "completed_audio")
        self.assertEqual(props["orpheus_routing_complete"], "true")
        self.assertEqual(o.props(text)["orpheus_segment_key"], "0:1")
        self.assertEqual(o.props(self.lesson_folders()[0])["orpheus_kind"], "lesson_folder")

    def test_missing_or_daylong_subject_leaves_review_no_completion(self):
        for i, name in enumerate(("2026-11-02_08-00-00_audio.mp3", "2026-11-02_08-00-00_biologia_chimica.mp3", "lezione.mp3")):
            audio = self.d.add("a" + str(i), name, "processing", "audio/mpeg")
            raw = self.d.add("r" + str(i), "raw.txt", "raw", "text/plain",
                {"orpheus_source_id": audio["id"], "orpheus_kind": "raw_transcript", "orpheus_original_name": name}, RAW)
            with self.subTest(name=name), self.assertRaises(o.ReviewRequired): self.process(audio, raw)
            self.assertEqual(self.d.get(audio["id"])["parents"], ["processing"])

    def test_idempotence_after_folder_creation_timeout(self):
        audio, raw = add_pair(self.d)
        self.d.fail_folder_after_create = True
        with self.assertRaises(TimeoutError): self.process(audio, raw)
        self.process(self.d.get(audio["id"]), self.d.get(raw["id"]))
        self.assertEqual(len(self.lesson_folders()), 1)
        self.assertEqual(len(self.lesson_texts()), 1)

    def test_idempotence_after_text_upload_timeout(self):
        audio, raw = add_pair(self.d)
        self.d.fail_text_after_create = True
        with self.assertRaises(TimeoutError): self.process(audio, raw)
        self.process(self.d.get(audio["id"]), self.d.get(raw["id"]))
        self.assertEqual(len(self.lesson_folders()), 1)
        self.assertEqual(len(self.lesson_texts()), 1)

    def test_different_drive_ids_same_filename_are_distinct(self):
        a1, r1 = add_pair(self.d, "a1", "r1")
        a2, r2 = add_pair(self.d, "a2", "r2")
        self.process(a1, r1)
        self.process(a2, r2)
        self.assertEqual({o.props(f)["orpheus_source_id"] for f in self.lesson_folders()}, {"a1", "a2"})
        self.assertEqual(len(self.lesson_texts()), 2)
        for f in self.lesson_texts(): self.assertEqual(self.d.read(f["id"]), RAW)

    def test_modified_transcript_or_raw_metadata_never_completes(self):
        audio, raw = add_pair(self.d)
        self.d.fail_text_after_create = True
        with self.assertRaises(TimeoutError): self.process(audio, raw)
        self.d.contents[raw["id"]] = b"changed"
        with self.assertRaises(o.ReviewRequired): self.process(self.d.get(audio["id"]), self.d.get(raw["id"]))
        self.assertEqual(self.d.get(audio["id"])["parents"], ["processing"])

    def test_duplicate_raw_or_duplicate_folder_review(self):
        audio, raw = add_pair(self.d)
        self.d.add("raw2", "same.txt", "processed", "text/plain",
            {"orpheus_source_id": audio["id"], "orpheus_kind": "raw_transcript", "orpheus_original_name": AUDIO_NAME}, RAW)
        with self.assertRaises(o.ReviewRequired): self.p._find_raw(audio["id"])
        self.d.items.pop("raw2")
        self.d.fail_move_to = "processed"
        with self.assertRaises(TimeoutError): self.process(audio, raw)
        folder = self.lesson_folders()[0]
        self.d.add("duplicate", folder["name"], folder["parents"][0], props=o.props(folder))
        with self.assertRaises(o.ReviewRequired): self.process(self.d.get(audio["id"]), self.d.get(raw["id"]))

    def test_unknown_kernel_never_pushes_or_retries(self):
        self.k.state = RuntimeError("404")
        self.d.add("a", AUDIO_NAME, "incoming", "audio/mpeg")
        self.p.cycle()
        self.assertEqual(self.k.pushes, [])
        self.d.move("a", "processing")
        self.d.set_props("a", {"orpheus_launch_state": "launched", "orpheus_seen_running": "1", "orpheus_attempts": "1", "orpheus_started_at": "1"})
        self.p.cycle()
        self.assertEqual(self.k.pushes, [])

    def test_retries_same_audio_only_after_observed_running_and_backoff(self):
        audio = self.d.add("a", AUDIO_NAME, "processing", "audio/mpeg",
            {"orpheus_launch_state": "launched", "orpheus_seen_running": "0", "orpheus_attempts": "1", "orpheus_started_at": "9000"})
        self.p._retry_or_wait(audio)
        self.assertFalse(self.k.pushes)
        self.k.state = "running"
        self.p._retry_or_wait(audio)
        self.k.state = "failed"
        self.p._retry_or_wait(audio)
        self.assertFalse(self.k.pushes)
        self.p.now = lambda: 11000
        self.p._retry_or_wait(audio)
        self.assertEqual(self.k.pushes, ["kaggle_kernel"])

    def test_uncertain_push_not_rolled_back_or_repeated(self):
        self.k.fail_push = True
        self.d.add("a", AUDIO_NAME, "incoming", "audio/mpeg")
        self.p.cycle()
        self.assertEqual(self.d.get("a")["parents"], ["processing"])
        self.assertEqual(o.props(self.d.get("a"))["orpheus_status"], "review")
        self.p.cycle()
        self.assertEqual(len(self.k.pushes), 1)

    def test_worker_gate_no_push_when_unverified_and_skip_non_audio(self):
        self.d.add("txt", "x.txt", "incoming", "text/plain")
        self.d.add("audio", AUDIO_NAME, "incoming", "audio/mpeg")
        o.Pipeline(config(worker_verified=False), self.svc).cycle()
        self.assertFalse(self.k.pushes)
        self.p.cycle()
        self.assertEqual(self.k.pushes, ["kaggle_kernel"])
        self.assertEqual(self.d.get("txt")["parents"], ["incoming"])

    def test_no_date_or_subject_stays_in_incoming_for_review(self):
        self.d.add("a", "lezione.mp3", "incoming", "audio/mpeg")
        self.p.cycle()
        self.assertEqual(self.d.get("a")["parents"], ["incoming"])
        self.assertEqual(o.props(self.d.get("a"))["orpheus_status"], "review")
        self.assertFalse(self.k.pushes)

    def test_classifier_flags_do_not_enable_single_lesson_gemini(self):
        audio, raw = add_pair(self.d)
        p = o.Pipeline(config(classifier_mode="gemini", allow_gemini=True), self.svc)
        with patch.object(o, "ask_gemini", side_effect=AssertionError("not called")) as call:
            p.process_transcript(audio, raw, self.targets)
            call.assert_not_called()
        self.assertEqual(self.svc.gemini_calls, 0)


class TestDrive(unittest.TestCase):
    def test_pagination_and_shared_drive_flags(self):
        calls = []
        class Req:
            def __init__(self, result): self.result = result
            def execute(self, **kwargs): return self.result
        class Files:
            def list(self, **kw):
                calls.append(kw)
                return Req({"files": [{"id": str(len(calls))}], **({"nextPageToken": "next"} if len(calls) == 1 else {})})
        fs = o.Drive(SimpleNamespace(files=lambda: Files())).list("p")
        self.assertEqual([x["id"] for x in fs], ["1", "2"])
        self.assertTrue(all(c["supportsAllDrives"] and c["includeItemsFromAllDrives"] for c in calls))

    def test_environment_defaults_fail_closed(self):
        env = {"FOLDER_AUDIO_INCOMING": "a", "FOLDER_PROCESSING_KAGGLE": "b", "FOLDER_TRASCRIZIONI": "c",
            "FOLDER_TRASCRIZIONI_ELABORATE": "d", "FOLDER_AUDIO_COMPLETATI": "e", "FOLDER_ERRORI": "f",
            "FOLDER_RADICE_MATERIE": "g", "KAGGLE_KERNEL_SLUG": "h"}
        cfg = o.Config.from_env(env)
        self.assertFalse(cfg.worker_verified)
        self.assertFalse(cfg.allow_gemini)
        self.assertEqual(cfg.classifier_mode, "")


if __name__ == "__main__": unittest.main()
