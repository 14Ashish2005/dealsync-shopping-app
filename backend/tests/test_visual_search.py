import ast
import logging
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Literal
from unittest.mock import patch

from PIL import Image
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from backend.visual_search.analyzer import (
    InvalidVisionOutputError,
    VisionAnalyzer,
    VisionProviderError,
)
from backend.visual_search.image_processing import (
    MAX_UPLOAD_SIZE_BYTES,
    ImageCleanupError,
    ImageValidationError,
    NormalizedImage,
    cleanup_temporary_image,
    normalize_image_bytes,
)
from backend.visual_search.models import VisualAnalysis


def load_app_image_helpers():
    app_path = Path(__file__).resolve().parents[2] / "app.py"
    module = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
    helper_names = {
        "_managed_temp_image_path",
        "_remove_managed_temp_image",
        "_cleanup_pending_image",
        "_discard_pending_image",
        "_create_pending_image",
    }
    definitions: list[ast.stmt] = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in helper_names:
            definitions.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "TEMP_IMAGE_PREFIX"
            for target in node.targets
        ):
            definitions.append(node)
    namespace = {
        "Path": Path,
        "os": os,
        "tempfile": tempfile,
        "logger": logging.Logger(
            "test-app-image-cleanup",
            level=logging.CRITICAL + 1,
        ),
    }
    exec(
        compile(ast.Module(body=definitions, type_ignores=[]), str(app_path), "exec"),
        namespace,
    )
    return namespace


def make_image(
    image_format="JPEG",
    size=(8, 6),
    *,
    exif=None,
    save_all=False,
    append_images=None,
):
    image = Image.new("RGB", size, (20, 80, 140))
    output = BytesIO()
    options = {
        "format": image_format,
        "save_all": save_all,
        "append_images": append_images or [],
    }
    if exif is not None:
        options["exif"] = exif
    image.save(output, **options)
    return output.getvalue()


class FakeRunnable:
    def __init__(self, result: object | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.messages: list[HumanMessage] | None = None

    def invoke(self, input: list[HumanMessage]) -> object:
        self.messages = input
        if self.error is not None:
            raise self.error
        return self.result


class FakeVisionModel:
    def __init__(self, runnable: FakeRunnable):
        self.runnable = runnable
        self.schema: type[VisualAnalysis] | None = None
        self.method: Literal["json_mode"] | None = None

    def with_structured_output(
        self,
        schema: type[VisualAnalysis],
        *,
        method: Literal["json_mode"],
    ) -> FakeRunnable:
        self.schema = schema
        self.method = method
        return self.runnable


def valid_analysis(**overrides):
    result = {
        "is_grocery_product": True,
        "is_organic": None,
        "product_category": "Food",
        "product_type": "Honey",
        "brand": None,
        "model": None,
        "primary_color": "gold",
        "secondary_colors": [],
        "shape_or_design": "short glass jar",
        "distinctive_features": ["metal lid"],
        "visible_text": ["honey"],
        "search_query": "honey jar",
        "description": "A jar of honey.",
        "confidence": {
            "product_category": 0.9,
            "product_type": 0.9,
            "primary_color": 0.8,
            "shape_or_design": 0.8,
            "search_query": 0.9,
        },
    }
    result.update(overrides)
    return result


class ImageProcessingTests(unittest.TestCase):
    def test_normalizes_jpeg_png_and_webp(self):
        for image_format, extension in (
            ("JPEG", ".jpg"),
            ("PNG", ".png"),
            ("WEBP", ".webp"),
        ):
            with self.subTest(image_format=image_format):
                normalized = normalize_image_bytes(
                    make_image(image_format),
                    filename=f"product{extension}",
                    media_type={
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "WEBP": "image/webp",
                    }[image_format],
                )
                self.assertEqual(normalized.media_type, "image/jpeg")
                with Image.open(BytesIO(normalized.data)) as image:
                    self.assertEqual(image.format, "JPEG")

    def test_rejects_extension_and_mime_mismatches(self):
        jpeg = make_image("JPEG")
        with self.assertRaises(ImageValidationError):
            normalize_image_bytes(jpeg, filename="product.png")
        with self.assertRaises(ImageValidationError):
            normalize_image_bytes(
                jpeg,
                filename="product.jpg",
                media_type="image/png",
            )

    def test_rejects_corrupt_and_oversized_uploads(self):
        with self.assertRaises(ImageValidationError):
            normalize_image_bytes(b"not an image", filename="product.jpg")
        with self.assertRaisesRegex(ImageValidationError, "10 MB"):
            normalize_image_bytes(
                b"x" * (MAX_UPLOAD_SIZE_BYTES + 1),
                filename="product.jpg",
            )

    def test_rejects_excessive_dimensions_and_pixel_count(self):
        image = make_image(size=(3, 3))
        with patch(
            "backend.visual_search.image_processing.MAX_IMAGE_DIMENSION", 2
        ), self.assertRaises(ImageValidationError):
            normalize_image_bytes(image, filename="product.jpg")

        with patch(
            "backend.visual_search.image_processing.MAX_IMAGE_PIXELS", 8
        ), self.assertRaises(ImageValidationError):
            normalize_image_bytes(image, filename="product.jpg")

    def test_rejects_animated_webp(self):
        first = Image.new("RGB", (4, 4), (0, 0, 0))
        second = Image.new("RGB", (4, 4), (255, 255, 255))
        output = BytesIO()
        try:
            first.save(
                output,
                format="WEBP",
                save_all=True,
                append_images=[second],
                duration=100,
                loop=0,
            )
        except (KeyError, OSError):
            self.skipTest("Pillow WebP animation support is unavailable.")

        with self.assertRaisesRegex(ImageValidationError, "multi-frame"):
            normalize_image_bytes(output.getvalue(), filename="product.webp")

    def test_normalizes_exif_orientation_and_strips_metadata(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[271] = "private-camera-model"
        source = make_image("JPEG", size=(2, 3), exif=exif)

        normalized = normalize_image_bytes(source, filename="product.jpg")

        self.assertEqual((normalized.width, normalized.height), (3, 2))
        with Image.open(BytesIO(normalized.data)) as image:
            self.assertEqual(image.getexif(), {})
            self.assertNotIn("exif", image.info)

    def test_temporary_image_cleanup_is_idempotent_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload.jpg"
            path.write_bytes(make_image())

            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                try:
                    raise RuntimeError("provider failed")
                finally:
                    cleanup_temporary_image(path)

            self.assertFalse(path.exists())
            cleanup_temporary_image(path)

    def test_cleanup_error_is_reported_without_echoing_path(self):
        with patch.object(Path, "unlink", side_effect=PermissionError):
            with self.assertRaises(ImageCleanupError) as context:
                cleanup_temporary_image("private-upload-path.jpg")
        self.assertNotIn("private-upload-path", str(context.exception))


class VisualAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.image = NormalizedImage(
            data=b"normalized image",
            media_type="image/jpeg",
            width=8,
            height=6,
        )

    def test_unknown_or_low_confidence_brand_and_model_are_null(self):
        result = valid_analysis(
            brand="Possible Brand",
            model="Maybe Model",
            confidence={
                "brand": 0.4,
                "model": 0.59,
            },
        )

        analysis = VisualAnalysis.model_validate(result)

        self.assertIsNone(analysis.brand)
        self.assertIsNone(analysis.model)
        self.assertIsNone(analysis.confidence.brand)
        self.assertIsNone(analysis.confidence.model)

    def test_confidence_and_text_limits_are_validated(self):
        invalid_confidence = valid_analysis(
            confidence={"brand": 1.1},
        )
        with self.assertRaises(ValidationError):
            VisualAnalysis.model_validate(invalid_confidence)

        invalid_text = valid_analysis(search_query="x" * 161)
        with self.assertRaises(ValidationError):
            VisualAnalysis.model_validate(invalid_text)

    def test_analyzer_requests_json_mode_and_returns_validated_model(self):
        runnable = FakeRunnable(result=valid_analysis())
        model = FakeVisionModel(runnable)
        analyzer = VisionAnalyzer(model)

        result = analyzer.analyze(self.image)

        self.assertIsInstance(result, VisualAnalysis)
        self.assertIs(model.schema, VisualAnalysis)
        self.assertEqual(model.method, "json_mode")
        legacy_response = result.to_legacy_response()
        self.assertEqual(
            set(legacy_response),
            {
                "is_grocery_product",
                "product_type",
                "search_query",
                "is_organic",
                "description",
            },
        )
        self.assertEqual(legacy_response["search_query"], "honey jar")
        messages = runnable.messages
        if messages is None:
            self.fail("The analyzer did not invoke the structured vision model.")
        content = messages[0].content
        if not isinstance(content, list):
            self.fail("The vision message did not contain multimodal content.")
        text_blocks = [part for part in content if isinstance(part, dict)]
        self.assertTrue(
            any("untrusted product data" in str(block.get("text", "")) for block in text_blocks)
        )

    def test_malformed_structured_output_is_rejected(self):
        analyzer = VisionAnalyzer(FakeVisionModel(FakeRunnable(result={"brand": "guess"})))

        with self.assertRaises(InvalidVisionOutputError):
            analyzer.analyze(self.image)

    def test_provider_timeout_is_sanitized(self):
        analyzer = VisionAnalyzer(
            FakeVisionModel(
                FakeRunnable(error=TimeoutError("credential-like internal detail"))
            )
        )

        with self.assertRaises(VisionProviderError) as context:
            analyzer.analyze(self.image)

        self.assertNotIn("credential-like", str(context.exception))
        self.assertIn("temporarily unavailable", str(context.exception))

    def test_provider_failure_does_not_expose_exception_details(self):
        analyzer = VisionAnalyzer(
            FakeVisionModel(
                FakeRunnable(error=RuntimeError("secret provider response"))
            )
        )

        with self.assertRaises(VisionProviderError) as context:
            analyzer.analyze(self.image)

        self.assertNotIn("secret provider response", str(context.exception))


class TemporaryImageLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_app_image_helpers()

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temporary_directory.name)
        self.session_state = {}
        self.create_pending_image = self.helpers["_create_pending_image"]
        self.cleanup_pending_image = self.helpers["_cleanup_pending_image"]
        self.discard_pending_image = self.helpers["_discard_pending_image"]

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create_image(self):
        return self.create_pending_image(
            b"normalized image",
            ".jpg",
            "uploaded.jpg",
            self.session_state,
            self.temp_path,
        )

    def test_write_failure_removes_partial_file_and_path_was_registered(self):
        real_named_temporary_file = tempfile.NamedTemporaryFile
        created_paths = []
        registered_paths_at_write = []
        session_state = self.session_state

        class FailingTemporaryFile:
            def __init__(self, **kwargs):
                self.file = real_named_temporary_file(**kwargs)
                self.name = self.file.name
                created_paths.append(Path(self.name))

            def __enter__(self):
                self.file.__enter__()
                return self

            def __exit__(self, *args):
                return self.file.__exit__(*args)

            def write(self, _image_data):
                registered_paths_at_write.append(
                    session_state.get("pending_image_path")
                )
                self.file.write(_image_data[:4])
                raise OSError("simulated write failure")

        with patch.object(
            tempfile,
            "NamedTemporaryFile",
            side_effect=lambda **kwargs: FailingTemporaryFile(**kwargs),
        ):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                self.create_image()

        self.assertEqual(registered_paths_at_write, [str(created_paths[0])])
        self.assertFalse(created_paths[0].exists())
        self.assertNotIn("pending_image_path", self.session_state)

    def test_deletion_failure_retains_path_for_successful_retry(self):
        path = Path(self.create_image())
        real_remove = os.remove

        with patch.object(os, "remove", side_effect=PermissionError("busy")):
            self.assertFalse(
                self.cleanup_pending_image(
                    self.session_state,
                    self.temp_path,
                )
            )

        self.assertTrue(path.exists())
        self.assertEqual(self.session_state["pending_image_path"], str(path))
        self.assertTrue(self.session_state["pending_image_processed"])
        with patch.object(os, "remove", side_effect=PermissionError("busy")):
            with self.assertRaisesRegex(OSError, "pending cleanup"):
                self.create_image()
        self.assertEqual(self.session_state["pending_image_path"], str(path))

        with patch.object(os, "remove", side_effect=real_remove):
            self.assertTrue(
                self.cleanup_pending_image(
                    self.session_state,
                    self.temp_path,
                )
            )
        self.assertFalse(path.exists())
        self.assertNotIn("pending_image_path", self.session_state)

    def test_successful_upload_cleanup_and_repeated_cleanup_are_idempotent(self):
        path = Path(self.create_image())
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), b"normalized image")
        self.assertFalse(self.session_state["pending_image_processed"])

        self.assertTrue(
            self.cleanup_pending_image(self.session_state, self.temp_path)
        )
        self.assertFalse(path.exists())
        self.assertTrue(
            self.cleanup_pending_image(self.session_state, self.temp_path)
        )
        self.assertNotIn("pending_image_path", self.session_state)

    def test_analysis_failure_and_logout_both_clean_pending_images(self):
        for error in (
            VisionProviderError("provider unavailable"),
            InvalidVisionOutputError("malformed response"),
        ):
            failed_analysis_path = Path(self.create_image())
            with self.assertRaises(type(error)):
                try:
                    raise error
                finally:
                    self.cleanup_pending_image(
                        self.session_state,
                        self.temp_path,
                    )
            self.assertFalse(failed_analysis_path.exists())

        logout_path = Path(self.create_image())
        self.assertTrue(
            self.discard_pending_image(self.session_state, self.temp_path)
        )
        self.assertFalse(logout_path.exists())

    def test_analysis_failure_always_cleans_the_pending_image(self):
        failed_analysis_path = Path(self.create_image())
        with self.assertRaisesRegex(RuntimeError, "vision failed"):
            try:
                raise RuntimeError("vision failed")
            finally:
                self.cleanup_pending_image(self.session_state, self.temp_path)
        self.assertFalse(failed_analysis_path.exists())

    def test_cleanup_refuses_unrelated_files(self):
        unrelated = self.temp_path / "unrelated.jpg"
        unrelated.write_bytes(b"not an owned temporary image")
        self.session_state["pending_image_path"] = str(unrelated)

        self.assertFalse(
            self.cleanup_pending_image(self.session_state, self.temp_path)
        )
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.session_state["pending_image_path"], str(unrelated))

    def test_account_logout_and_chat_clear_use_shared_cleanup(self):
        app_path = Path(__file__).resolve().parents[2] / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
        discard_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_discard_pending_image"
        ]
        self.assertEqual(len(discard_calls), 3)


if __name__ == "__main__":
    unittest.main()
