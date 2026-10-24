import asyncio
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


TEMPLATES = (Path(__file__).resolve().parents[1] / "plugins" /
             "infermatrix-copilot" / "skills" / "explainer-video" / "templates")


def load_template(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VideoSegmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # 导入临时副本，避免普通 pytest 把 .pyc 写入待分发的模板。
        shutil.copytree(TEMPLATES, self.root, dirs_exist_ok=True)
        (self.root / "build").mkdir()
        (self.root / "narration.json").write_text(json.dumps({"output": "out.mp4"}), encoding="utf-8")
        self.timeline = {"total": 0.4, "scenes": [
            {"id": "s1_title", "start": 0, "len": 0.2, "lines": [
                {"file": "title.mp3", "start": 0, "dur": 0.1, "cap": "Title"}]},
            {"id": "s2_points", "start": 0.2, "len": 0.2, "lines": [
                {"file": "points.mp3", "start": 0.2, "dur": 0.1, "cap": "Points"}]},
        ]}
        self.save_timeline()
        self.renderer = load_template(self.root / "render.py")

    def save_timeline(self):
        (self.root / "build" / "timeline.json").write_text(json.dumps(self.timeline), encoding="utf-8")

    def test_import_uses_temporary_template_copy(self):
        self.assertEqual(Path(self.renderer.__file__).parent, self.root)
        self.assertEqual(Path(self.renderer.__cached__).parent.parent, self.root)

    def test_duplicate_scene_ids_fail_before_writing_audio(self):
        tts = load_template(self.root / "tts.py")
        spec = json.loads((TEMPLATES / "narration.json").read_text(encoding="utf-8"))
        spec["scenes"][1]["id"] = spec["scenes"][0]["id"]
        (self.root / "narration.json").write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "scene IDs must be unique"):
            asyncio.run(tts.main(self.root))
        self.assertFalse((self.root / "build" / "audio").exists())

    def test_fingerprint_tracks_inputs_but_ignores_music_only_edits(self):
        original = self.renderer.Project(self.root).fingerprint
        self.assertEqual(original, self.renderer.Project(self.root).fingerprint)
        narration = self.root / "narration.json"
        narration.write_text(json.dumps({"output": "out.mp4", "music": "new.mp3"}), encoding="utf-8")
        self.assertEqual(original, self.renderer.Project(self.root).fingerprint)

        for relative in ["scenes/s1_title.js", "index.html"]:
            with self.subTest(stage_file=relative):
                path = self.root / "stage" / relative
                previous = path.read_bytes()
                path.write_bytes(previous.replace(b"--bg: #04060c", b"--bg: #123456")
                                 if relative == "index.html" else previous.replace(b"first claim", b"new claim"))
                self.assertNotEqual(original, self.renderer.Project(self.root).fingerprint)
                path.write_bytes(previous)

        # SVG 素材字节也参与缓存判断。
        asset = self.root / "stage" / "icon.svg"
        asset.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
        with_asset = self.renderer.Project(self.root).fingerprint
        asset.write_text('<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>', encoding="utf-8")
        self.assertNotEqual(with_asset, self.renderer.Project(self.root).fingerprint)
        asset.unlink()

        self.timeline["scenes"][0]["lines"][0]["cap"] = "Changed caption"
        self.save_timeline()
        self.assertNotEqual(original, self.renderer.Project(self.root).fingerprint)
        for name, value in [("FPS", 24), ("SEG", 60), ("SCALE", 1.5),
                            ("ENCODE", [*self.renderer.ENCODE, "-threads", "1"])]:
            with self.subTest(setting=name):
                before = self.renderer.Project(self.root).fingerprint
                previous = getattr(self.renderer, name)
                try:
                    setattr(self.renderer, name, value)
                    self.assertNotEqual(before, self.renderer.Project(self.root).fingerprint)
                finally:
                    setattr(self.renderer, name, previous)

    @unittest.skipUnless(importlib.util.find_spec("playwright") and shutil.which("ffmpeg"),
                         "optional media check requires Playwright/Chromium and FFmpeg")
    def test_real_render_resume_invalidation_and_final_guard(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as engine:
            if not Path(engine.chromium.executable_path).is_file():
                self.skipTest("Chromium is absent; run python -m playwright install chromium")
        renderer = self.renderer
        project = renderer.Project(self.root)
        renderer.render_seg(project, 0)
        segment = project.segs / "seg_00.mp4"
        signature = segment.with_suffix(".sha256")
        stamp = segment.stat().st_mtime_ns
        renderer.render_seg(renderer.Project(self.root), 0)
        self.assertEqual(stamp, segment.stat().st_mtime_ns)

        # 无签名的历史分段必须拒绝合成，并在重新渲染后恢复。
        signature.unlink()
        with self.assertRaisesRegex(RuntimeError, "stale or unverified"):
            renderer.concat(renderer.Project(self.root))
        renderer.render_seg(renderer.Project(self.root), 0)
        self.assertNotEqual(stamp, segment.stat().st_mtime_ns)
        stamp = segment.stat().st_mtime_ns

        html = self.root / "stage" / "index.html"
        html.write_text(html.read_text(encoding="utf-8").replace("--bg: #04060c", "--bg: #123456"), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "stale or unverified"):
            renderer.concat(renderer.Project(self.root))
        renderer.render_seg(renderer.Project(self.root), 0)
        self.assertNotEqual(stamp, segment.stat().st_mtime_ns)
        silent = renderer.concat(renderer.Project(self.root))
        subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(silent), "-f", "null", "-"], check=True)

        self.timeline["scenes"][0]["lines"][0]["cap"] = "New caption"
        self.save_timeline()
        with self.assertRaisesRegex(RuntimeError, "stale or unverified"):
            renderer.concat(renderer.Project(self.root))
        self.timeline["scenes"][0]["lines"][0]["cap"] = "Title"
        self.save_timeline()
        renderer.FPS = 24
        with self.assertRaisesRegex(RuntimeError, "stale or unverified"):
            renderer.concat(renderer.Project(self.root))
        self.timeline["total"] = 1
        self.timeline["scenes"][0]["len"] = 0.5
        self.timeline["scenes"][1].update(start=0.5, len=0.5)
        self.timeline["scenes"][1]["lines"][0]["start"] = 0.5
        self.save_timeline()
        with sync_playwright() as engine:
            browser, page = renderer.Project(self.root).open_page(engine, 1)
            try:
                page.evaluate("renderAt(0.5)")
                self.assertEqual(page.locator("#tc").inner_text(), "00:00:00:12")
                page.evaluate("renderAt(0.999)")
                self.assertEqual(page.locator("#tc").inner_text(), "00:00:00:23")
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
