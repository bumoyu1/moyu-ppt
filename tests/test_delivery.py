"""Offline delivery regressions using real image and PPTX artifacts."""

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deck_delivery as delivery


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def image_job(self, page, *, size=(320, 180), color=None, suffix="png", **extra):
        path = self.directory / f"p{page:02d}.{suffix}"
        color = color or ((page * 67) % 256, (page * 103) % 256, (page * 41) % 256)
        Image.new("RGB", size, color).save(path)
        return {"page": page, "output": str(path), "prompt": f"可见文字=第{page}页；位置=顶部\n可见文字=增长12.5%；位置=中部", **extra}

    def manifest(self, jobs, name="manifest.json"):
        path = self.directory / name
        path.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
        return path

    def notes(self, job, **overrides):
        return {str(job["page"]): {"status": "passed", "reviewer": "fixture reviewer", "reviewed_at": "2026-09-09T10:00:00+08:00", "image_sha256": hashlib.sha256(Path(job["output"]).read_bytes()).hexdigest(), "checks": ["text", "layout", "style"], **overrides}}

    def main(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = delivery.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_corrupt_image_is_rejected_before_pptx_can_replace_existing_delivery(self):
        job = self.image_job(1)
        destination = self.directory / "deck.pptx"
        delivery.assemble([job], destination, "16:9")
        previous_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
        Path(job["output"]).write_bytes(b"not an image")
        manifest = self.manifest([job])
        report_path = self.directory / "quality.json"
        code, _, _ = self.main(["--manifest", str(manifest), "--ocr", "none", "--report", str(report_path), "--output-pptx", str(destination)])
        report = json.loads(report_path.read_text())
        self.assertEqual(code, 1)
        self.assertFalse(report["structural_ok"])
        self.assertNotIn("pptx", report)
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), previous_hash)

    def test_jpeg_header_without_complete_pixel_data_is_not_a_valid_slide(self):
        job = self.image_job(1, suffix="jpg")
        path = Path(job["output"])
        path.write_bytes(path.read_bytes()[:-20])
        # This regression matters because JPEG.verify() alone accepts it.
        with Image.open(path) as image:
            image.verify()
        report = delivery.inspect_jobs([job], "16:9", ocr="none")
        self.assertFalse(report["structural_ok"])
        self.assertTrue(report["pages"][0]["errors"])

    def test_out_of_order_manifests_recover_page_order_and_final_image_hashes(self):
        jobs = [self.image_job(i) for i in (1, 2, 3)]
        first = self.manifest([jobs[2], jobs[0]], "anchors.json")
        second = self.manifest([jobs[1]], "pages.json")
        ordered = delivery.load_jobs([first, second])
        self.assertEqual([job["page"] for job in ordered], [1, 2, 3])
        output = self.directory / "ordered.pptx"
        delivery.assemble([jobs[2], jobs[0], jobs[1]], output, "16:9")
        reopened = Presentation(output)
        actual = [hashlib.sha256(slide.shapes[0].image.blob).hexdigest() for slide in reopened.slides]
        expected = [hashlib.sha256(Path(job["output"]).read_bytes()).hexdigest() for job in jobs]
        self.assertEqual(actual, expected)

    def test_duplicate_page_and_duplicate_file_are_both_blocked(self):
        first, second = self.image_job(1), self.image_job(2)
        for jobs in ([first, {**second, "page": 1}], [first, {**second, "output": first["output"]}]):
            with self.subTest(jobs=jobs), self.assertRaisesRegex(ValueError, "Duplicate"):
                delivery.load_jobs([self.manifest(jobs)])

    def test_gap_and_missing_final_page_block_delivery(self):
        first, third = self.image_job(1), self.image_job(3)
        with self.assertRaisesRegex(ValueError, "complete and contiguous"):
            delivery.load_jobs([self.manifest([first, third])])
        manifest = self.manifest([first])
        output = self.directory / "incomplete.pptx"
        with self.assertRaises(SystemExit) as error, contextlib.redirect_stderr(io.StringIO()):
            delivery.main(["--manifest", str(manifest), "--expected-pages", "2", "--ocr", "none", "--output-pptx", str(output)])
        self.assertEqual(error.exception.code, 2)
        self.assertFalse(output.exists())

    def test_fractional_boolean_and_empty_page_identity_are_not_silently_coerced(self):
        job = self.image_job(1)
        for page in (1.7, True, 0, "", "1.0"):
            with self.subTest(page=page), self.assertRaises(ValueError):
                delivery.load_jobs([self.manifest([{**job, "page": page}])])
        with self.assertRaises(ValueError):
            delivery.load_jobs([self.manifest([])])

    def test_wrong_slide_ratio_is_reported_and_cannot_be_stretched_into_pptx(self):
        job = self.image_job(1, size=(200, 200))
        report = delivery.inspect_jobs([job], "16:9", ocr="none")
        self.assertFalse(report["structural_ok"])
        self.assertIn("aspect_ratio_mismatch", report["pages"][0]["errors"])
        with self.assertRaisesRegex(ValueError, "Cannot assemble"):
            delivery.assemble([job], self.directory / "wrong-ratio.pptx", "16:9")

    def test_per_page_ratio_overrides_do_not_allow_mixed_shapes_in_one_deck(self):
        jobs = [self.image_job(1, aspect_ratio="16:9"), self.image_job(2, size=(300, 400), aspect_ratio="3:4")]
        report = delivery.inspect_jobs(jobs, "16:9", ocr="none")
        self.assertFalse(report["structural_ok"])
        self.assertIn("deck_aspect_ratio_mismatch", report["pages"][1]["errors"])
        self.assertNotIn("aspect_ratio_mismatch", report["pages"][1]["errors"])
        with self.assertRaises(ValueError):
            delivery.assemble(jobs, self.directory / "mixed.pptx", "16:9")

    def test_nan_or_infinite_ratio_is_not_a_way_to_skip_ratio_validation(self):
        for value in ("nan:9", "inf:1", "16:0", "-1:9"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                delivery.ratio(value)

    def test_unavailable_or_skipped_ocr_does_not_claim_visual_review(self):
        job = self.image_job(1)
        for mode in ("auto", "none"):
            with self.subTest(mode=mode), mock.patch.object(delivery, "local_ocr", return_value={"status": "unavailable", "reason": "fixture adapter unavailable"}) as ocr:
                report = delivery.inspect_jobs([job], "16:9", ocr=mode)
            self.assertTrue(report["structural_ok"])
            self.assertFalse(report["review_complete"])
            self.assertEqual(report["pages"][0]["visual_review"]["status"], "pending")
            self.assertIn("text_check_requires_visual_review", report["pages"][0]["warnings"])
            if mode == "none":
                ocr.assert_not_called()

    def test_matching_ocr_is_only_text_evidence_not_a_full_visual_pass(self):
        job = self.image_job(1)
        observed = {"status": "completed", "lines": [{"text": "第1页 增长12.5%"}], "qr_payloads": []}
        with mock.patch.object(delivery, "local_ocr", return_value=observed):
            report = delivery.inspect_jobs([job], "16:9")
        self.assertTrue(report["structural_ok"])
        self.assertEqual(report["pages"][0]["text_review_candidates"], [])
        self.assertFalse(report["review_complete"])

    def test_ocr_number_or_punctuation_difference_is_preserved_for_review(self):
        job = self.image_job(1)
        observed = {"status": "completed", "lines": [{"text": "第1页 增长125%"}], "qr_payloads": []}
        with mock.patch.object(delivery, "local_ocr", return_value=observed):
            report = delivery.inspect_jobs([job], "16:9")
        self.assertIn("增长12.5%", report["pages"][0]["text_review_candidates"])
        self.assertIn("ocr_difference_requires_visual_review", report["pages"][0]["warnings"])
        self.assertFalse(report["review_complete"])

    def test_spaced_visible_copy_accepted_by_preflight_is_actually_checked(self):
        job = self.image_job(1)
        job["prompt"] = ("可见文字清单\n"
                         "  可见文字 = 季度收入 ； 位置 = 标题\n"
                         "\t可见文字\t=\t增长12.5% ;\t位置\t=\t中部\n"
                         "可见文字 = 数据截至六月\n")
        with mock.patch.object(delivery, "local_ocr", return_value={"status": "completed", "lines": [{"text": "季度成本 增长125%"}], "qr_payloads": []}):
            report = delivery.inspect_jobs([job], "16:9")
        expected = ["季度收入", "增长12.5%", "数据截至六月"]
        self.assertEqual(report["pages"][0]["expected_text"], expected)
        self.assertEqual(report["pages"][0]["text_review_candidates"], expected)
        self.assertIn("ocr_difference_requires_visual_review", report["pages"][0]["warnings"])

    def test_valid_human_notes_apply_to_exact_image_without_needing_ocr(self):
        job = self.image_job(1)
        report = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=self.notes(job))
        self.assertTrue(report["review_complete"])
        self.assertEqual(report["pages"][0]["ocr"]["status"], "not_run")
        self.assertEqual(report["pages"][0]["visual_review"]["reviewer"], "fixture reviewer")

    def test_regenerated_image_invalidates_old_review_notes(self):
        job = self.image_job(1)
        notes = self.notes(job)
        self.image_job(1, color="green")
        report = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=notes)
        self.assertTrue(report["structural_ok"])
        self.assertFalse(report["review_complete"])
        self.assertEqual(report["pages"][0]["visual_review"]["status"], "pending")

    def test_token_or_partial_review_notes_do_not_claim_completion(self):
        job = self.image_job(1)
        for update in ({"checks": "yes"}, {"checks": ["layout"]}, {"reviewer": " "}, {"reviewed_at": "sometime"}, {"image_sha256": ""}):
            with self.subTest(update=update):
                report = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=self.notes(job, **update))
                self.assertFalse(report["review_complete"])

    def test_brand_and_qr_slides_need_corresponding_review_evidence(self):
        job = self.image_job(1, qr_required=True, references=[{"role": "brand", "path": "fixture-logo.png"}])
        partial = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=self.notes(job))
        self.assertFalse(partial["review_complete"])
        checked_only = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=self.notes(job, checks=["text", "layout", "style", "brand", "qr"]))
        self.assertTrue(checked_only["structural_ok"])
        self.assertFalse(checked_only["review_complete"])
        self.assertIn("qr_scan_missing_evidence", checked_only["pages"][0]["warnings"])
        complete = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=self.notes(job, checks=["text", "layout", "style", "brand", "qr"], qr_evidence={"method": "fixture offline QR decoder", "payload": "actual decoded content"}))
        self.assertTrue(complete["review_complete"])
        self.assertIsNone(complete["pages"][0]["qr_verification"]["target_match"])

    def test_qr_payload_alone_also_requires_decoding_evidence(self):
        job = self.image_job(1, qr_payload="https://expected.example/")
        for mode in ("none", "auto"):
            with self.subTest(mode=mode), mock.patch.object(delivery, "local_ocr", return_value={"status": "unavailable"}):
                report = delivery.inspect_jobs([job], "16:9", ocr=mode, review_notes=self.notes(job, checks=["text", "layout", "style", "qr"]))
            self.assertTrue(report["structural_ok"])
            self.assertFalse(report["review_complete"])
            self.assertEqual(report["pages"][0]["qr_verification"]["status"], "pending")

    def test_completed_tool_with_matching_decoded_qr_can_complete_review(self):
        job = self.image_job(1, qr_required=True, qr_payload="https://expected.example/?id=7")
        observed = {"status": "completed", "lines": [{"text": "第1页 增长12.5%"}], "qr_payloads": [job["qr_payload"]]}
        with mock.patch.object(delivery, "local_ocr", return_value=observed):
            report = delivery.inspect_jobs([job], "16:9", review_notes=self.notes(job, checks=["text", "layout", "style", "qr"]))
        self.assertTrue(report["review_complete"])
        verification = report["pages"][0]["qr_verification"]
        self.assertTrue(verification["target_match"])
        self.assertEqual(verification["evidence"][0]["source"], "local_ocr")

    def test_malformed_or_empty_tool_payload_is_not_decode_evidence(self):
        job = self.image_job(1, qr_payload="https://expected.example/")
        for payloads in (job["qr_payload"], {"payload": job["qr_payload"]}, [], [" "], [None], [job["qr_payload"], None]):
            with self.subTest(payloads=payloads), mock.patch.object(delivery, "local_ocr", return_value={"status": "completed", "lines": [], "qr_payloads": payloads}):
                report = delivery.inspect_jobs([job], "16:9", review_notes=self.notes(job, checks=["text", "layout", "style", "qr"]))
            self.assertTrue(report["structural_ok"])
            self.assertFalse(report["review_complete"])
            self.assertEqual(report["pages"][0]["qr_verification"]["status"], "pending")

    def test_manual_decode_matches_target_when_ocr_is_skipped_or_misses_qr(self):
        job = self.image_job(1, qr_payload="https://expected.example/?id=7")
        notes = self.notes(job, checks=["text", "layout", "style", "qr"], qr_evidence={"method": "fixture phone scanner", "payload": job["qr_payload"]})
        for mode in ("none", "auto"):
            with self.subTest(mode=mode), mock.patch.object(delivery, "local_ocr", return_value={"status": "completed", "lines": [], "qr_payloads": []}):
                report = delivery.inspect_jobs([job], "16:9", ocr=mode, review_notes=notes)
            self.assertTrue(report["review_complete"])
            self.assertEqual(report["pages"][0]["qr_verification"]["evidence"][0]["source"], "review_notes")
            if mode == "none":
                self.assertEqual(report["pages"][0]["ocr"]["status"], "not_run")

    def test_manual_qr_record_needs_both_method_and_actual_payload(self):
        job = self.image_job(1, qr_payload="https://expected.example/")
        for evidence in ({"method": "phone scan"}, {"payload": job["qr_payload"]}, {"method": " ", "payload": job["qr_payload"]}, {"method": "phone scan", "payload": ""}, "passed"):
            with self.subTest(evidence=evidence):
                report = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=self.notes(job, checks=["text", "layout", "style", "qr"], qr_evidence=evidence))
            self.assertTrue(report["structural_ok"])
            self.assertFalse(report["review_complete"])

    def test_manual_decoded_target_mismatch_is_not_normalized_away(self):
        job = self.image_job(1, qr_payload="https://expected.example/?id=7")
        notes = self.notes(job, checks=["text", "layout", "style", "qr"], qr_evidence={"method": "fixture phone scanner", "payload": "https://expected.example/?id=8"})
        report = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=notes)
        self.assertFalse(report["structural_ok"])
        self.assertFalse(report["review_complete"])
        self.assertIn("qr_payload_mismatch", report["pages"][0]["errors"])

    def test_old_image_hash_cannot_supply_manual_qr_evidence_for_new_image(self):
        job = self.image_job(1, qr_payload="https://expected.example/")
        notes = self.notes(job, checks=["text", "layout", "style", "qr"], qr_evidence={"method": "fixture phone scanner", "payload": job["qr_payload"]})
        self.image_job(1, color="green")
        report = delivery.inspect_jobs([job], "16:9", ocr="none", review_notes=notes)
        self.assertTrue(report["structural_ok"])
        self.assertFalse(report["review_complete"])
        self.assertEqual(report["pages"][0]["qr_verification"]["status"], "pending")

    def test_conflicting_decoder_evidence_requires_resolution(self):
        job = self.image_job(1, qr_payload="https://expected.example/")
        notes = self.notes(job, checks=["text", "layout", "style", "qr"], qr_evidence={"method": "fixture phone scanner", "payload": job["qr_payload"]})
        with mock.patch.object(delivery, "local_ocr", return_value={"status": "completed", "lines": [], "qr_payloads": ["https://wrong.example/"]}):
            report = delivery.inspect_jobs([job], "16:9", review_notes=notes)
        self.assertFalse(report["structural_ok"])
        self.assertFalse(report["review_complete"])
        self.assertEqual(report["pages"][0]["qr_verification"]["status"], "failed")

    def test_decoded_wrong_qr_blocks_delivery_even_with_passed_notes(self):
        job = self.image_job(1, qr_required=True, qr_payload="https://expected.example/")
        observed = {"status": "completed", "lines": [], "qr_payloads": ["https://wrong.example/"]}
        with mock.patch.object(delivery, "local_ocr", return_value=observed):
            report = delivery.inspect_jobs([job], "16:9", review_notes=self.notes(job, checks=["text", "layout", "style", "qr"]))
        self.assertFalse(report["structural_ok"])
        self.assertFalse(report["review_complete"])
        self.assertIn("qr_payload_mismatch", report["pages"][0]["errors"])

    def test_reopened_pptx_has_exactly_one_full_bleed_image_and_no_text_per_page(self):
        jobs = [self.image_job(i) for i in (1, 2)]
        output = self.directory / "deck.pptx"
        delivery.assemble(jobs, output, "16:9")
        presentation = Presentation(output)
        self.assertEqual(len(presentation.slides), 2)
        self.assertAlmostEqual(presentation.slide_width / presentation.slide_height, 16 / 9, places=6)
        for slide in presentation.slides:
            self.assertEqual(len(slide.shapes), 1)
            picture = slide.shapes[0]
            self.assertEqual(picture.shape_type, MSO_SHAPE_TYPE.PICTURE)
            self.assertFalse(picture.has_text_frame)
            self.assertEqual((picture.left, picture.top), (0, 0))
            self.assertEqual((picture.width, picture.height), (presentation.slide_width, presentation.slide_height))
            self.assertEqual((picture.crop_left, picture.crop_top, picture.crop_right, picture.crop_bottom), (0, 0, 0, 0))

    def test_failed_pptx_save_preserves_previous_artifact_and_cleans_temporary_file(self):
        job = self.image_job(1)
        destination = self.directory / "deck.pptx"
        destination.write_bytes(b"previous delivery fixture")
        before = set(self.directory.iterdir())
        with mock.patch("pptx.presentation.Presentation.save", side_effect=OSError("fixture disk failure")):
            with self.assertRaises(OSError):
                delivery.assemble([job], destination, "16:9")
        self.assertEqual(destination.read_bytes(), b"previous delivery fixture")
        self.assertEqual(set(self.directory.iterdir()), before)

    def test_output_paths_cannot_overwrite_source_images_or_each_other(self):
        job = self.image_job(1)
        original = Path(job["output"]).read_bytes()
        with self.assertRaisesRegex(ValueError, "overwrite"):
            delivery.assemble([job], job["output"], "16:9")
        manifest = self.manifest([job])
        path = str(self.directory / "same.pptx")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            delivery.main(["--manifest", str(manifest), "--report", path, "--output-pptx", path])
        self.assertEqual(Path(job["output"]).read_bytes(), original)
        self.assertFalse(Path(path).exists())


if __name__ == "__main__":
    unittest.main()
