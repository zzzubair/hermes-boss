"""boss note: any session hands the owner a note through the inbox, journaled with who sent it."""
import contextlib
import io
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from boss.inbox import LINE_CHARS, NOTE, Inbox
from boss.state import Store
from tests import test_boss as harness
from tests.support import HermeticTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
DISCORD = {"HERMES_SESSION_KEY": "agent:discord:dm", "HERMES_SESSION_PLATFORM": "discord",
           "HERMES_SESSION_CHAT_TYPE": "dm"}


class NoteTest(HermeticTestCase):
    setUp = harness.CLITest.setUp
    tearDown = harness.CLITest.tearDown

    def cli(self, *args, env=None):
        """(exit code, stdout, stderr) run as the session described by `env`; none is a human shell."""
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env or {}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            for name in ("HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_TYPE",
                         "HERMES_SESSION_CHAT_NAME"):
                if name not in (env or {}):
                    os.environ.pop(name, None)
            from boss.cli import main
            code = main(list(args))
        return code, output.getvalue(), errors.getvalue()

    def own(self):
        code, output, _ = self.cli("own", "--label", "Hermes app", env={"HERMES_SESSION_KEY": "agent:app"})
        self.assertEqual(code, 0, output)
        self.take()  # the claim's own inbox state is not under test

    def take(self):
        return Inbox(self.home).take(Store(self.home, self.redact).load, "watch", "Hermes app")

    def notes(self):
        path = self.home / "captain.jsonl"
        return [r for r in map(json.loads, path.read_text().splitlines()) if r["event"] == "note"] if path.exists() else []

    def test_a_session_that_does_not_own_boss_hands_the_owner_a_note(self):
        self.own()
        code, output, errors = self.cli("note", "--project", "clashlens", "The leaderboard is cut off on phones",
                                        env=DISCORD)
        self.assertEqual(code, 0, output)
        self.assertEqual(errors, "")  # no owner warning or refusal
        self.assertIn("Note left for Hermes app; it arrives with its next boss watch or tool call.", output)
        self.assertEqual(self.take(),
                         f"[boss inbox]\nNote from Discord DM (clashlens): The leaderboard is cut off on phones\n{NOTE}")
        [record] = self.notes()
        self.assertEqual((record["sender"], record["project"], record["text"]),
                         ("Discord DM", "clashlens", "The leaderboard is cut off on phones"))
        self.assertIsNone(self.take())  # delivered once

    def test_sender_label_comes_from_the_chat_or_from(self):
        self.own()
        channel = {**DISCORD, "HERMES_SESSION_CHAT_TYPE": "group", "HERMES_SESSION_CHAT_NAME": "clash-lens"}
        self.assertEqual(self.cli("note", "From the channel", env=channel)[0], 0)
        self.assertEqual(self.cli("note", "From my phone", "--from", "Zubair's phone", env=DISCORD)[0], 0)
        self.assertEqual(self.cli("note", "From a terminal")[0], 0)
        self.assertEqual(self.take().splitlines()[1:4], ["Note from Discord clash-lens: From the channel",
                                                         "Note from Zubair's phone: From my phone",
                                                         "Note from a shell: From a terminal"])
        self.assertEqual([r["sender"] for r in self.notes()], ["Discord clash-lens", "Zubair's phone", "a shell"])

    def test_with_nobody_owning_boss_the_note_waits_for_the_next_owner(self):
        code, output, _ = self.cli("note", "Check the deploy", env=DISCORD)
        self.assertEqual(code, 0, output)
        self.assertIn("Note held. Nobody has boss open; it arrives when a session runs /boss.", output)
        self.assertEqual(len(Inbox(self.home).open_items()), 1)
        self.cli("own", "--label", "Hermes app", env={"HERMES_SESSION_KEY": "agent:app"})
        self.assertIn("Note from Discord DM: Check the deploy", self.take())

    def test_secret_looking_text_is_refused_and_nothing_is_kept(self):
        self.own()
        for args in (("note", f"use {harness.TOKEN} to log in"), ("note", "hi", "--project", harness.TOKEN),
                     ("note", "hi", "--from", harness.TOKEN)):
            code, output, _ = self.cli(*args, env=DISCORD)
            self.assertEqual(code, 2, output)
            self.assertIn("note contains a token or secret", output)
            self.assertNotIn(harness.TOKEN, output)
        self.assertEqual(self.notes(), [])
        self.assertEqual(Inbox(self.home).open_items(), [])
        self.assertFalse((self.home / "notes").exists())

    def test_empty_or_oversized_text_is_a_usage_error(self):
        for args in (("note", " "), ("note", "x" * 20001), ("note", "hi", "--project", " "), ("note", "hi", "--from", "")):
            self.assertEqual(self.cli(*args)[0], 1, args)
        self.assertEqual(self.notes(), [])

    def test_images_are_kept_with_the_note_and_named_in_its_line(self):
        self.own()
        shots = [self.home / "a.png", self.home / "b.png"]
        for shot in shots:
            shot.write_bytes(PNG)
        code, output, _ = self.cli("note", "--project", "clashlens", "Look at these", "--image", str(shots[0]),
                                   "--image", str(shots[1]), env=DISCORD)
        self.assertEqual(code, 0, output)
        [record] = self.notes()
        saved = Path(record["saved"])
        self.assertEqual(saved.parent, self.home / "notes")
        self.assertEqual(sorted(p.name for p in saved.iterdir()), ["image-1.png", "image-2.png", "note.txt"])
        self.assertEqual((saved / "image-2.png").read_bytes(), PNG)
        self.assertEqual((record["images"], (saved / "note.txt").read_text()), (2, "Look at these\n"))
        self.assertIn(f"Note from Discord DM (clashlens): Look at these (2 images and the full note in {saved}/)",
                      self.take())

    def test_a_bad_image_sends_nothing(self):
        self.own()
        (self.home / "notes.txt").write_text("not an image")
        for path in (self.home / "missing.png", self.home / "notes.txt"):
            code, output, _ = self.cli("note", "Look", "--image", str(path), env=DISCORD)
            self.assertEqual(code, 1, output)
        self.assertEqual(self.notes(), [])
        self.assertEqual(Inbox(self.home).open_items(), [])

    def test_a_long_note_keeps_a_short_line_and_the_full_text_on_disk(self):
        self.own()
        text = "word " * 300
        self.assertEqual(self.cli("note", text, env=DISCORD)[0], 0)
        line = self.take().splitlines()[1]
        self.assertLessEqual(len(line), LINE_CHARS)
        self.assertTrue(line.startswith("Note from Discord DM: word word"))
        saved = Path(self.notes()[0]["saved"])
        self.assertTrue(line.endswith(f"(full note in {saved}/note.txt)"), line)
        self.assertEqual((saved / "note.txt").read_text(), text.strip() + "\n")

    def test_the_note_shows_in_the_away_timeline(self):
        self.own()
        app = {"HERMES_SESSION_KEY": "agent:app"}
        self.assertEqual(self.cli("away", "bed", env=app)[0], 0)
        self.assertEqual(self.cli("note", "--project", "clashlens", "Ship the fix tonight", env=DISCORD)[0], 0)
        code, output, _ = self.cli("back", "--full", env=app)
        self.assertEqual(code, 0, output)
        self.assertRegex(output, r"\d\d:\d\d note from Discord DM \(clashlens\): Ship the fix tonight")

    def test_a_busy_ledger_makes_the_note_wait_not_fail(self):
        self.own()
        store, holding, release = Store(self.home, self.redact), threading.Event(), threading.Event()

        def hold():
            with store.locked():
                holding.set()
                release.wait(5)
        worker = threading.Thread(target=hold)
        worker.start()
        holding.wait(5)
        threading.Timer(0.3, release.set).start()
        started = time.monotonic()
        code, output, _ = self.cli("note", "While you were busy", env=DISCORD)
        worker.join()
        self.assertEqual(code, 0, output)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertIn("Note from Discord DM: While you were busy", self.take())
