"""Captain images reach the worker's thread through T3 attachments, checked before any upload."""
import json

from boss.attachments import MAX_BYTES
from boss.common import BossError, Redactor
from boss.mcp import MCP
from tests import test_boss as harness
from tests.support import HermeticTestCase


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20
GIF = b"GIF89a" + b"\x00" * 20
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 20


class AttachmentTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown
    cli = harness.CLITest.cli
    spawn = harness.CLITest.spawn

    def image(self, name, data):
        path = self.home / name
        path.write_bytes(data)
        return str(path)

    def calls(self, name):
        return [a for n, a in self.fake.calls if n == name]

    def events(self, task_id):
        lines = (self.store.directory(task_id) / "events.log").read_text().splitlines()
        return [json.loads(line) for line in lines]

    def test_send_uploads_each_image_then_attaches_them_to_the_boss_message(self):
        meta = self.spawn()
        # Hermes cache names can lie about the type: the bytes decide.
        paths = [self.image("shot.png", PNG), self.image("photo.jpg", JPEG),
                 self.image("anim.gif", GIF), self.image("upload.bin", WEBP)]
        argv = ["send", meta["id"], "the tie should sit right of the trophies"]
        for path in paths:
            argv += ["--image", path]
        code, output = self.cli(*argv)
        self.assertEqual(code, 0, output)
        self.assertIn("sent with 4 images", output)
        prepared = [a["upload"] for a in self.calls("t3_attachment_prepare_upload")]
        self.assertEqual(prepared, [
            {"type": "image", "name": "shot.png", "mimeType": "image/png", "sizeBytes": len(PNG)},
            {"type": "image", "name": "photo.jpg", "mimeType": "image/jpeg", "sizeBytes": len(JPEG)},
            {"type": "image", "name": "anim.gif", "mimeType": "image/gif", "sizeBytes": len(GIF)},
            {"type": "image", "name": "upload.bin", "mimeType": "image/webp", "sizeBytes": len(WEBP)}])
        self.assertEqual([(path, headers["Authorization"], headers["Content-Type"], data)
                          for path, headers, data in self.fake.uploads],
                         [(f"/api/attachments/upload/pending-{n}", "Bearer " + harness.TOKEN, mime, data)
                          for n, mime, data in ((1, "image/png", PNG), (2, "image/jpeg", JPEG),
                                                (3, "image/gif", GIF), (4, "image/webp", WEBP))])
        [send] = self.calls("t3_thread_send_attachments")
        self.assertEqual(send["threadId"], meta["thread_id"])
        self.assertEqual(send["message"], "[boss] the tie should sit right of the trophies")
        self.assertEqual([(a["type"], a["id"], a["name"]) for a in send["attachments"]],
                         [("image", f"pending-{n}", name) for n, name in
                          ((1, "shot.png"), (2, "photo.jpg"), (3, "anim.gif"), (4, "upload.bin"))])
        self.assertEqual(self.calls("t3_thread_send"), [])
        events = self.events(meta["id"])
        intent = next(e for e in events if e["event"] == "send_intent")
        self.assertEqual(intent["details"]["images"][0], {"name": "shot.png", "mimeType": "image/png", "sizeBytes": len(PNG)})
        self.assertEqual(events[-1]["event"], "sent")
        self.assertNotIn("data", json.dumps(intent["details"]["images"]))

    def test_send_without_images_keeps_the_retry_keyed_text_send(self):
        meta = self.spawn()
        self.assertEqual(self.cli("send", meta["id"], "carry on")[0], 0)
        self.assertEqual(len(self.calls("t3_thread_send")), 1)
        self.assertEqual(self.calls("t3_attachment_prepare_upload") + self.calls("t3_thread_send_attachments"), [])

    def test_bad_images_are_refused_before_anything_uploads(self):
        meta = self.spawn()
        big = self.home / "huge.png"
        with big.open("wb") as handle:
            handle.write(PNG)
            handle.truncate(MAX_BYTES + 1)
        cases = {
            "too big": [str(big)],
            "not an image": [self.image("notes.png", b"just text, not a picture")],
            "svg": [self.image("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")],
            "empty": [self.image("empty.png", b"")],
            "missing": [str(self.home / "nope.png")],
            "directory": [str(self.home)],
            "nine": [self.image(f"s{n}.png", PNG) for n in range(9)],
        }
        before = len(self.fake.calls)
        for label, paths in cases.items():
            with self.subTest(label):
                argv = ["send", meta["id"], "look"]
                for path in paths:
                    argv += ["--image", path]
                code, output = self.cli(*argv)
                self.assertEqual(code, 1, output)
        self.assertEqual(self.fake.calls[before:], [])
        self.assertEqual(self.fake.uploads, [])
        self.assertNotIn("send_intent", [e["event"] for e in self.events(meta["id"])])

    def test_exactly_eight_images_and_ten_megabytes_are_allowed(self):
        meta = self.spawn()
        edge = self.home / "edge.png"
        edge.write_bytes(PNG + b"\x00" * (MAX_BYTES - len(PNG)))
        argv = ["send", meta["id"], "eight"] + ["--image", str(edge)] + \
            sum((["--image", self.image(f"s{n}.png", PNG)] for n in range(7)), [])
        self.assertEqual(self.cli(*argv)[0], 0)
        [send] = self.calls("t3_thread_send_attachments")
        self.assertEqual(len(send["attachments"]), 8)
        self.assertEqual(send["attachments"][0]["sizeBytes"], MAX_BYTES)

    def test_failed_upload_discards_pending_uploads_and_sends_nothing(self):
        meta = self.spawn()
        self.fake.upload_status = 500
        code, output = self.cli("send", meta["id"], "look", "--image", self.image("a.png", PNG),
                                "--image", self.image("b.png", PNG))
        self.assertEqual(code, 3, output)
        self.assertNotIn(harness.TOKEN, output)
        self.assertEqual([a["attachmentId"] for a in self.calls("t3_attachment_discard")], ["pending-1"])
        self.assertEqual(self.calls("t3_thread_send_attachments"), [])

    def test_spawn_launches_with_the_images_on_the_brief(self):
        meta = self.spawn("--image", self.image("bug.png", PNG))
        [launch] = self.calls("t3_thread_launch")
        self.assertEqual(launch["attachments"], [{"type": "image", "id": "pending-1", "name": "bug.png",
                                                  "mimeType": "image/png", "sizeBytes": len(PNG)}])
        self.assertEqual(len(self.fake.uploads), 1)
        intent = next(e for e in self.events(meta["id"]) if e["event"] == "launch_intent")
        self.assertEqual(intent["details"]["images"], [{"name": "bug.png", "mimeType": "image/png", "sizeBytes": len(PNG)}])
        plain = self.spawn()
        self.assertNotIn("attachments", self.calls("t3_thread_launch")[-1])
        self.assertNotIn("images", self.events(plain["id"])[0]["details"])

    def test_spawn_with_a_bad_image_launches_nothing_and_leaves_no_task(self):
        code, output = self.cli("spawn", "repo", "--title", "Fix", "--image", self.image("x.png", b"nope"))
        self.assertEqual(code, 1, output)
        self.assertEqual(self.fake.calls, [])
        self.fake.upload_status = 500
        code, output = self.cli("spawn", "repo", "--title", "Fix", "--image", self.image("y.png", PNG))
        self.assertEqual(code, 3, output)
        self.assertEqual(self.calls("t3_thread_launch"), [])
        self.assertEqual(list(self.store.tasks()), [])


class UploadTargetTest(HermeticTestCase):
    def test_bearer_is_only_sent_to_a_path_on_the_mcp_origin(self):
        client = MCP("http://127.0.0.1:9/mcp", "secret-token-value", Redactor())
        for target in ("https://elsewhere.example/upload", "//elsewhere.example/upload", "upload/x"):
            with self.subTest(target), self.assertRaises(BossError) as caught:
                client.upload(target, PNG, "image/png")
            self.assertEqual(caught.exception.code, "protocol")
