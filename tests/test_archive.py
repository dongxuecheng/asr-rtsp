import base64
from contextlib import closing
import datetime as dt
import io
import json
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import wave

import requests

from rtsp_asr_save_audio import (normalized_segments, parse_args, parse_extra_form,
                                 worker, put_task)
from rtsp_stream import redact, read_exact_audio_chunk, StreamStalledError
from speech import FRAME_BYTES, FRAME_MS, SpeechChunk, SpeechSegmenter
from storage import Store
from web import create_app


class Detector:
    def is_speech(self, pcm, sample_rate):
        return pcm != b"\x00" * FRAME_BYTES


class SpeechTests(unittest.TestCase):
    def run_frames(self, flags, segmenter=None):
        segmenter = segmenter or SpeechSegmenter(detector=Detector(), threshold=0)
        origin = dt.datetime(2026, 10, 26, 12)
        chunks = []
        for index, flag in enumerate(flags):
            pcm = (b"\x00\x10" if flag else b"\x00\x00") * (FRAME_BYTES//2)
            chunks.extend(segmenter.feed(pcm, origin+dt.timedelta(milliseconds=index*FRAME_MS)))
        chunks.extend(segmenter.flush())
        return chunks

    def test_real_vad_silence(self):
        self.assertEqual(self.run_frames([False]*100, SpeechSegmenter()), [])

    def test_silence_and_clicks_are_discarded(self):
        self.assertEqual(self.run_frames([False]*30+[True]*3+[False]*50), [])

    def test_long_silence_is_excluded_and_timeline_preserved(self):
        chunks = self.run_frames([False]*100+[True]*30+[False]*100+[True]*30+[False]*100)
        self.assertEqual(len(chunks), 2)
        self.assertLess(sum(c.duration for c in chunks), 4)
        # Silent gaps stay in the recording timestamps, not concatenated audio.
        self.assertAlmostEqual((chunks[1].started_at-chunks[0].started_at).total_seconds(), 3.9)
        self.assertGreater(chunks[0].started_at.second, 1)

    def test_max_duration_splits_without_dropping_voiced_samples(self):
        chunks = self.run_frames([True]*100, SpeechSegmenter(max_seconds=1, detector=Detector(), threshold=0))
        self.assertEqual(sum(len(c.pcm) for c in chunks), 100*FRAME_BYTES)
        self.assertTrue(all(c.duration <= 1 for c in chunks))
        for previous, current in zip(chunks, chunks[1:]):
            self.assertEqual(current.started_at, previous.started_at+dt.timedelta(seconds=previous.duration))

    def test_shutdown_flush_and_short_speech(self):
        self.assertEqual(len(self.run_frames([True]*12)), 1)
        self.assertEqual(self.run_frames([True]*7), [])


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / ".qa" / "tests"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        assert Path(self.temp.name).resolve().parent == test_root.resolve()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.start = dt.datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        self.day = self.start.date().isoformat()
        self.pcm = b"\x10\x00"*16000
        self.client = create_app({"TESTING":True,"DATA_DIR":self.temp.name,
                                  "WEB_USERNAME":"","WEB_PASSWORD":""}).test_client()

    def add(self, text="你好，测试归档", start=None):
        start = start or self.start
        return self.store.save(start, self.pcm, [{"start":0.1,"end":0.9,"speaker":"speaker_0","text":text}])

    def legacy(self, status, text=""):
        record_id = self.add("旧记录")
        row = self.store.record(self.day, record_id)
        with closing(sqlite3.connect(Path(self.temp.name)/self.day/"records.sqlite3")) as conn, conn:
            conn.execute("UPDATE records SET status=?, text=?, segments='[]' WHERE id=?", (status,text,record_id))
        raw = Path(self.temp.name)/self.day/"raw_json"
        raw.mkdir(exist_ok=True)
        (raw/f"{record_id}.json").write_text('{}')
        return row

    def test_valid_text_and_audio_are_saved_together(self):
        record_id = self.add()
        row = self.store.record(self.day, record_id)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["text"], "你好，测试归档")
        with wave.open(str(Path(self.temp.name)/self.day/"audio"/row["audio_file"]), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000,1,2))
            self.assertEqual(wav.readframes(16000), self.pcm)

    def test_blank_text_is_not_saved(self):
        for text in ("", " \n\t\r\u3000"):
            self.assertIsNone(self.store.save(self.start,self.pcm,[{"text":text}],payload={"text":text}))
        self.assertFalse(list(Path(self.temp.name).rglob("*.wav")))
        self.assertFalse(list(Path(self.temp.name).rglob("*.json")))
        self.assertEqual(self.store.days(),[])

    def test_query_pagination_export_and_range_audio(self):
        record_id = self.add("关键词 <script>alert('x')</script>")
        self.add("另一条记录")
        response = self.client.get(f"/api/records?date={self.day}&limit=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["summary"]["count"], 2)
        self.assertEqual(len(response.json["items"]), 1)
        self.assertNotIn("audio_file", response.json["items"][0])
        found = self.client.get("/api/records", query_string={"date":self.day,"q":"关键词"})
        self.assertEqual(found.json["summary"]["count"],1)
        self.assertEqual(self.client.get("/api/records", query_string={"date":self.day,"q":"%"}).json["summary"]["count"], 0)
        exported = self.client.get(f"/api/export?date={self.day}")
        self.assertIn("关键词", exported.get_data(as_text=True))
        self.assertIn("attachment", exported.headers["Content-Disposition"])
        audio = self.client.get(f"/api/audio/{self.day}/{record_id}", headers={"Range":"bytes=0-43"})
        self.assertEqual(audio.status_code,206)
        self.assertEqual(audio.data[:4],b"RIFF")
        audio.close()

    def test_restart_does_not_overwrite_files(self):
        a, b = self.add(), self.add()
        self.assertNotEqual(a,b)
        self.assertEqual(len(list((Path(self.temp.name)/self.day/"audio").glob("*.wav"))),2)

    def test_legacy_invalid_records_are_hidden_and_purged(self):
        rows = [self.legacy("failed","失败旧文本"), self.legacy("empty"),
                self.legacy("pending"), self.legacy("done"," \n\t")]
        records = self.store.records(self.day)
        self.assertEqual(records["summary"]["count"],0)
        self.assertEqual(records["items"],[])
        self.assertEqual(self.client.get("/api/days").json["days"],[])
        self.assertEqual(self.client.get(f"/api/records?date={self.day}").json["items"],[])
        exported = self.client.get(f"/api/export?date={self.day}").get_data(as_text=True)
        self.assertNotIn("失败旧文本",exported)
        self.assertNotIn("09:00:00",exported)
        for row in rows:
            self.assertEqual(self.client.get(f"/api/audio/{self.day}/{row['id']}").status_code,404)
            self.assertNotIn(row["id"],exported)
            self.assertIsNone(self.store.record(self.day,row["id"]))
        self.assertEqual(self.store.purge_unrecognized(),4)
        self.assertEqual(self.store._read(self.day,"SELECT id FROM records"),[])
        self.assertFalse(list(Path(self.temp.name).rglob("*.wav")))
        self.assertFalse(list(Path(self.temp.name).rglob("*.json")))

    def test_purge_keeps_successful_records_and_runs_with_retention_off(self):
        valid = self.add("保留有效内容")
        self.legacy("failed")
        store = Store(self.temp.name,retention_days=0)
        self.assertEqual(store.cleanup(),[])
        self.assertEqual(store.purge_unrecognized(),1)
        self.assertEqual(store.days()[0]["count"],1)
        self.assertEqual(store.records(self.day)["summary"]["duration"],1)
        self.assertEqual(store.records(self.day)["summary"]["characters"],len("保留有效内容"))
        response = self.client.get(f"/api/audio/{self.day}/{valid}")
        self.assertEqual(response.status_code,200)
        response.close()

    def test_failed_file_deletion_stays_hidden_and_is_retried(self):
        row = self.legacy("failed")
        with patch("storage.Path.unlink",side_effect=PermissionError("busy")), self.assertLogs(level="ERROR"):
            self.assertEqual(self.store.purge_unrecognized(),0)
        self.assertIsNone(self.store.record(self.day,row["id"]))
        self.assertEqual(self.store.days(),[])
        self.assertEqual(self.store.purge_unrecognized(),1)
        self.assertFalse(list(Path(self.temp.name).rglob("*.wav")))

    def test_expiration_boundary_and_complete_day_deletion(self):
        permanent = Store(self.temp.name,retention_days=0)
        expired_time = self.start-dt.timedelta(days=7)
        keep_time = self.start-dt.timedelta(days=6)
        expired_day = expired_time.date().isoformat()
        permanent.save(expired_time,self.pcm,[{"text":"old"}],payload={"text":"old"})
        permanent.save(keep_time,self.pcm,[{"text":"keep"}])
        runtime = Path(self.temp.name)/expired_day/"runtime"
        runtime.mkdir()
        (runtime/"old.log").write_text("old")
        unrelated = Path(self.temp.name)/"notes"
        unrelated.mkdir()
        self.assertEqual(permanent.cleanup(),[])
        self.assertEqual(self.store.cleanup(),[expired_day])
        self.assertFalse((Path(self.temp.name)/expired_day).exists())
        self.assertTrue((Path(self.temp.name)/keep_time.date().isoformat()).exists())
        self.assertTrue(unrelated.exists())
        # A late ASR result must not recreate a deleted archive day.
        self.assertIsNone(self.store.save(expired_time,self.pcm,[{"text":"late"}]))
        self.assertFalse((Path(self.temp.name)/expired_day).exists())

    def test_date_changes_at_midnight(self):
        today = self.start.replace(hour=0,minute=0,second=1)
        previous = today-dt.timedelta(seconds=2)
        self.add(start=previous)
        self.add(start=today)
        self.assertEqual(len(self.store.days()),2)

    def test_validation_and_no_path_traversal(self):
        for value in ("../../other", "2026-02-30", "2026-1-1"):
            self.assertEqual(self.client.get("/api/records",query_string={"date":value}).status_code,400)
        for query in ("page=0","page=x","limit=999","limit=-1"):
            self.assertEqual(self.client.get(f"/api/records?{query}").status_code,400)
        self.assertEqual(self.client.get(f"/api/audio/{self.day}/anything").status_code,404)

    def test_auth_protects_records_audio_export_and_static(self):
        client = create_app({"TESTING":True,"DATA_DIR":self.temp.name,"WEB_USERNAME":"admin","WEB_PASSWORD":"secret"}).test_client()
        for route in ("/","/api/days","/api/records","/api/export","/static/app.js"):
            self.assertEqual(client.get(route).status_code,401)
        self.assertEqual(client.get("/healthz").status_code,200)
        header = "Basic " + base64.b64encode(b"admin:secret").decode()
        self.assertEqual(client.get("/api/days",headers={"Authorization":header}).status_code,200)

    def test_worker_posts_wav_and_saves_text(self):
        args = parse_args(["rtsp://example/stream","--output-dir",self.temp.name,"--retry-count","0","--save-json"])
        tasks = queue.Queue()
        tasks.put((1,SpeechChunk(self.start,self.pcm)))
        tasks.put(None)
        class Response:
            status_code=200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def json(self): return {"text":"通过模拟 ASR"}
        class Session:
            def post(inner,*args,**kwargs):
                self.assertFalse(list(Path(self.temp.name).rglob("*.wav")))
                self.assertEqual(self.store.days(),[])
                self.assertEqual(kwargs["files"]["file"][1].read(4),b"RIFF")
                self.assertEqual(kwargs["data"]["response_format"],"verbose_json")
                return Response()
            def close(self): pass
        with patch("rtsp_asr_save_audio.new_http_session",return_value=Session()):
            worker(1,tasks,args,{})
        records = self.store.records(self.day)["items"]
        self.assertEqual(records[0]["text"],"通过模拟 ASR")
        self.assertEqual(records[0]["status"],"done")
        self.assertEqual(len(list((Path(self.temp.name)/self.day/"raw_json").glob("*.json"))),1)

    def test_worker_failure_does_not_save_audio_or_records(self):
        args = parse_args(["rtsp://example/stream","--output-dir",self.temp.name,"--retry-count","0"])
        tasks = queue.Queue()
        tasks.put((1,SpeechChunk(self.start,self.pcm)))
        tasks.put(None)
        class Session:
            def post(self,*args,**kwargs): raise requests.ConnectionError("offline")
            def close(self): pass
        with patch("rtsp_asr_save_audio.new_http_session",return_value=Session()):
            worker(1,tasks,args,{})
        self.assertEqual(self.store.records(self.day)["items"],[])
        self.assertFalse(list(Path(self.temp.name).rglob("*.wav")))

    def test_worker_empty_or_invalid_responses_are_not_saved(self):
        args = parse_args(["rtsp://example/stream","--output-dir",self.temp.name,"--retry-count","0","--save-json"])
        cases = [(200,p) for p in ({}, {"text":" \n\t\u3000"}, {"segments":[{"text":""}]}, {"data":{"text":""}}, [])]
        cases.append((503,{"text":"错误响应中的文本不能保存"}))
        for status,payload in cases:
            with self.subTest(status=status,payload=payload):
                tasks = queue.Queue()
                tasks.put((1,SpeechChunk(self.start,self.pcm)))
                tasks.put(None)
                response = Mock()
                response.status_code = status
                response.text = "service unavailable"
                response.json.return_value = payload
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                session = Mock()
                session.post.return_value = response
                with patch("rtsp_asr_save_audio.new_http_session",return_value=session):
                    worker(1,tasks,args,{})
                self.assertEqual(self.store.records(self.day)["items"],[])
                self.assertFalse(list(Path(self.temp.name).rglob("*.wav")))
                self.assertFalse(list(Path(self.temp.name).rglob("*.json")))

    def test_retry_success_saves_only_the_successful_result(self):
        args = parse_args(["rtsp://example/stream","--output-dir",self.temp.name,"--retry-count","1"])
        tasks = queue.Queue()
        tasks.put((1,SpeechChunk(self.start,self.pcm)))
        tasks.put(None)
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"text":"重试成功"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.post.side_effect = [requests.Timeout("retry"),response]
        with patch("rtsp_asr_save_audio.new_http_session",return_value=session), patch("rtsp_asr_save_audio.time.sleep"):
            worker(1,tasks,args,{})
        self.assertEqual(session.post.call_count,2)
        self.assertEqual(self.store.records(self.day)["items"][0]["text"],"重试成功")
        self.assertEqual(len(list(Path(self.temp.name).rglob("*.wav"))),1)


class UtilityTests(unittest.TestCase):
    def test_watchdog_detects_hung_ffmpeg(self):
        process = Mock()
        process.poll.return_value = None
        with patch("rtsp_stream.os.name","posix"), patch("rtsp_stream.time.monotonic",side_effect=[0,0,31]), patch("rtsp_stream.select.select",return_value=([],[],[])):
            with self.assertRaises(StreamStalledError):
                read_exact_audio_chunk(process,960,30,0,0,queue.Queue())

    def test_read_watchdog_obeys_shutdown(self):
        event = threading.Event()
        event.set()
        with patch("rtsp_stream.os.name","posix"):
            with self.assertRaises(InterruptedError):
                read_exact_audio_chunk(Mock(),960,30,0,0,queue.Queue(),event)

    def test_text_fallback_for_empty_segments(self):
        self.assertEqual(normalized_segments({"segments":[{"text":""}],"text":"fallback"},1)[0]["text"],"fallback")
        segments = normalized_segments({"data":{"segments":[{"text":"hi","start":1000,"end":2000,"speaker_id":0}]}},3)
        self.assertEqual((segments[0]["start"], segments[0]["end"], segments[0]["speaker"]),(1,2,"0"))

    def test_parameter_and_boolean_validation(self):
        self.assertEqual(parse_extra_form('{"diarize":false}'),{"diarize":"false"})
        for arguments in (["--chunk","nan"],["--vad-mode","4"],["--prefix","../x"],["--retention-days","-1"]):
            with self.assertRaises(SystemExit), patch("sys.stderr",new=io.StringIO()):
                parse_args(["rtsp://example/stream",*arguments])

    def test_drop_oldest_queue_accounting(self):
        tasks = queue.Queue(maxsize=1)
        tasks.put("old")
        put_task(tasks,"new")
        self.assertEqual(tasks.get(),"new")
        tasks.task_done()
        self.assertEqual(tasks.unfinished_tasks,0)

    def test_redacts_rtsp_credentials(self):
        self.assertNotIn("secret",redact("ffmpeg rtsp://admin:secret@camera/path failed"))


if __name__ == "__main__":
    unittest.main()
