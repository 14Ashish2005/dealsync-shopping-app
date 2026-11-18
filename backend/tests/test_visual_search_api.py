from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import closing
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from starlette import formparsers
from starlette.requests import ClientDisconnect, Request

from backend.providers import (
    LocalCatalogAdapter,
    NormalizedOffer,
    ProviderAdapter,
    ProviderError,
    ProviderRegistry,
)
from backend.providers.base import PriceInput
from backend.tests._support import (
    import_main_with_temporary_auth_db,
    prepare_test_schemas,
)
from backend.visual_search.analyzer import (
    InvalidVisionOutputError,
    VisionProviderError,
)
from backend.visual_search.models import VisualAnalysis


def _analysis(*, search_query: str | None = "honey") -> VisualAnalysis:
    return VisualAnalysis.model_validate(
        {
            "is_grocery_product": search_query is not None,
            "product_type": "Honey" if search_query is not None else None,
            "search_query": search_query,
            "confidence": {
                "product_type": 0.9,
                "search_query": 0.9 if search_query is not None else None,
            },
        }
    )


def _jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 6), (50, 90, 130)).save(output, format="JPEG")
    return output.getvalue()


class FakeAnalyzer:
    def __init__(self) -> None:
        self.analysis = _analysis()
        self.error: Exception | None = None
        self.calls = 0

    def analyze(self, _image: object) -> VisualAnalysis:
        _ = _image
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.analysis


class FailingProvider(ProviderAdapter):
    @property
    def provider_key(self) -> str:
        return "failing_provider"

    @property
    def display_name(self) -> str:
        return "Failing provider"

    def search(
        self,
        query: str = "",
        *,
        max_price: PriceInput | None = None,
        currency: str | None = None,
        is_organic: bool | None = None,
    ) -> list[NormalizedOffer]:
        _ = query, max_price, currency, is_organic
        raise ProviderError(self.provider_key, "private provider exception")

    def normalize_offer(
        self,
        product: Mapping[str, object],
    ) -> NormalizedOffer:
        _ = product
        raise NotImplementedError


class VisualSearchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = import_main_with_temporary_auth_db()

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "store.sqlite"
        self.auth_path = Path(self.temporary_directory.name) / "auth.sqlite"
        self._create_database()

        self.main = self.__class__.main
        self.original_store_path = self.main.STORE_DB_PATH
        self.original_auth_path = self.main.AUTH_DB_PATH
        self.original_registry = self.main.PROVIDER_REGISTRY
        self.original_outer_timeout = self.main.VISION_OUTER_DEADLINE_SECONDS
        self.original_rate_limit = self.main.VISION_RATE_LIMIT_REQUESTS
        setattr(self.main, "STORE_DB_PATH", self.database_path)
        setattr(self.main, "AUTH_DB_PATH", self.auth_path)
        setattr(
            self.main,
            "PROVIDER_REGISTRY",
            ProviderRegistry(
                [
                    LocalCatalogAdapter(self.database_path),
                    FailingProvider(),
                ]
            ),
        )
        setattr(self.main, "VISION_OUTER_DEADLINE_SECONDS", 35.0)
        setattr(self.main, "VISION_RATE_LIMIT_REQUESTS", 5)
        prepare_test_schemas(self.database_path, self.auth_path)

        self.analyzer = FakeAnalyzer()
        self.factory_patch = patch.object(
            self.main,
            "create_vision_analyzer",
            return_value=self.analyzer,
        )
        self.factory_patch.start()
        self.client = TestClient(self.main.app)
        self.client.__enter__()
        self.session_token = self.main.create_session(
            "test-owner",
            self.main.app.state.database_engines.authentication,
        )
        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            self.session_token,
        )

    def tearDown(self) -> None:
        event = getattr(self, "vision_finished", None)
        if event is not None:
            event.set()
        if getattr(self, "client", None) is not None:
            self.client.__exit__(None, None, None)
        self.factory_patch.stop()
        setattr(self.main, "STORE_DB_PATH", self.original_store_path)
        setattr(self.main, "AUTH_DB_PATH", self.original_auth_path)
        setattr(self.main, "PROVIDER_REGISTRY", self.original_registry)
        setattr(
            self.main,
            "VISION_OUTER_DEADLINE_SECONDS",
            self.original_outer_timeout,
        )
        setattr(
            self.main,
            "VISION_RATE_LIMIT_REQUESTS",
            self.original_rate_limit,
        )
        self.temporary_directory.cleanup()

    def _create_database(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    description TEXT,
                    is_organic INTEGER DEFAULT 0,
                    currency TEXT
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO products
                    (id, name, category, price, description, is_organic, currency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, "Honey jar", "Food", 8.0, "Golden honey", 0, "USD"),
                    (2, "Honey bottle", "Food", 250.0, "Honey", 0, "INR"),
                    (3, "Honey tin", "Food", 5.0, "Honey", 0, None),
                ],
            )
            connection.commit()

    def _post(
        self,
        *,
        filename: str = "product.jpg",
        image: bytes | None = None,
        media_type: str = "image/jpeg",
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = {"Origin": "http://localhost:5174"}
        if headers:
            request_headers.update(headers)
        return self.client.post(
            "/api/offers/search-by-image",
            files={
                "image": (
                    filename,
                    _jpeg_bytes() if image is None else image,
                    media_type,
                )
            },
            data=data or {},
            headers=request_headers,
        )

    def _run_body_middleware(
        self,
        events: list[dict[str, object]],
        *,
        max_body_bytes: int,
        repeated_reads: int = 0,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
        async def run() -> tuple[
            list[dict[str, object]],
            list[dict[str, object]],
            int,
        ]:
            response_messages: list[dict[str, object]] = []
            downstream_messages: list[dict[str, object]] = []
            receive_index = 0

            async def receive() -> dict[str, object]:
                nonlocal receive_index
                if receive_index >= len(events):
                    return {"type": "http.disconnect"}
                message = events[receive_index]
                receive_index += 1
                return message

            async def send(message: dict[str, object]) -> None:
                response_messages.append(message)

            async def app(_scope, inner_receive, inner_send) -> None:
                if repeated_reads:
                    for _ in range(repeated_reads):
                        downstream_messages.append(await inner_receive())
                    status_code = 200
                else:
                    status_code = 200
                    while True:
                        message = await inner_receive()
                        downstream_messages.append(message)
                        if message["type"] == "http.disconnect":
                            status_code = 400
                            break
                        if not message.get("more_body", False):
                            break
                await inner_send({
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": [],
                })
                await inner_send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                })

            middleware = self.main._ImageSearchBodyLimitMiddleware(
                app=app,
                max_body_bytes=max_body_bytes,
            )
            await middleware(
                {
                    "type": "http",
                    "path": "/api/offers/search-by-image",
                    "method": "POST",
                    "headers": [],
                },
                receive,
                send,
            )
            return response_messages, downstream_messages, receive_index

        return asyncio.run(run())

    def _post_raw_multipart(self, body: bytes, boundary: str):
        return self.client.post(
            "/api/offers/search-by-image",
            content=body,
            headers={
                "Origin": "http://localhost:5174",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    def _invoke_image_route_with_receive(self, receive, boundary: str) -> None:
        async def invoke() -> None:
            path = "/api/offers/search-by-image"
            request = Request(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode("ascii"),
                    "query_string": b"",
                    "headers": [
                        (b"origin", b"http://localhost:5174"),
                        (
                            b"content-type",
                            f"multipart/form-data; boundary={boundary}".encode(
                                "ascii"
                            ),
                        ),
                        (b"content-length", b"1024"),
                    ],
                    "client": ("testclient", 123),
                    "server": ("testserver", 80),
                    "app": self.main.app,
                },
                receive,
            )
            await self.main.api_offer_search_by_image(
                request,
                "test-owner",
                self.main.PROVIDER_REGISTRY,
            )

        asyncio.run(invoke())

    def test_authentication_and_origin_protection(self) -> None:
        self.client.cookies.clear()
        response = self._post()
        self.assertEqual(response.status_code, 401)

        self.client.cookies.set(
            self.main.SESSION_COOKIE_NAME,
            self.session_token,
        )
        response = self._post(headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("search_query", response.json())

        response = self.client.post(
            "/api/offers/search-by-image",
            files={"image": ("product.jpg", _jpeg_bytes(), "image/jpeg")},
        )
        self.assertEqual(response.status_code, 403)

    def test_origin_and_referer_cases(self) -> None:
        configured_origin = self._post(
            data={"provider_key": "local_catalog"},
            headers={"Origin": "http://localhost:5174"},
        )
        self.assertEqual(configured_origin.status_code, 200)

        valid_referer = self.client.post(
            "/api/offers/search-by-image",
            files={"image": ("product.jpg", _jpeg_bytes(), "image/jpeg")},
            data={"provider_key": "local_catalog"},
            headers={"Referer": "http://localhost:5174/search"},
        )
        self.assertEqual(valid_referer.status_code, 200)

        for headers in (
            {"Origin": "null"},
            {"Origin": "http://localhost:invalid-port"},
            {"Origin": "https://evil.example"},
            {"Origin": "http://localhost:5175"},
            {},
        ):
            with self.subTest(headers=headers):
                response = self.client.post(
                    "/api/offers/search-by-image",
                    files={
                        "image": ("product.jpg", _jpeg_bytes(), "image/jpeg")
                    },
                    headers=headers,
                )
                self.assertEqual(response.status_code, 403)

    def test_successful_ranked_result_and_partial_provider_outcome(self) -> None:
        response = self._post()

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["search_query"], "honey")
        self.assertEqual(payload["ranking_method"], "uncalibrated_catalog_text")
        self.assertEqual(len(payload["rankings"]), 3)
        self.assertEqual(
            [provider["status"] for provider in payload["providers"]],
            ["success", "error"],
        )
        self.assertTrue(payload["partial"])
        self.assertNotIn("private provider exception", response.text)
        self.assertEqual(
            {summary["currency"] for summary in payload["price_summaries"]},
            {"USD", "INR"},
        )

    def test_no_query_returns_empty_result_without_provider_call(self) -> None:
        self.analyzer.analysis = _analysis(search_query=None)
        engine = self.main.app.state.discovery_engine
        with patch.object(
            engine,
            "search",
            side_effect=AssertionError("provider search must not run"),
        ):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["search_query"], "")
        self.assertEqual(response.json()["offers"], [])
        self.assertEqual(response.json()["providers"], [])
        self.assertEqual(self.analyzer.calls, 1)

    def test_invalid_images_and_mime_or_extension_mismatch(self) -> None:
        cases = (
            ("broken.jpg", b"not an image", "image/jpeg"),
            ("wrong.png", _jpeg_bytes(), "image/png"),
            ("product.jpg", _jpeg_bytes(), "text/plain"),
        )
        for filename, image, media_type in cases:
            with self.subTest(filename=filename, media_type=media_type):
                response = self._post(
                    filename=filename,
                    image=image,
                    media_type=media_type,
                )
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("path", response.text.casefold())

    def test_oversized_image_and_request_body(self) -> None:
        image_response = self._post(
            image=b"x" * (self.main.MAX_UPLOAD_SIZE_BYTES + 1),
        )
        self.assertEqual(image_response.status_code, 413)

        messages, _, _ = self._run_body_middleware(
            [
                {
                    "type": "http.request",
                    "body": b"x" * 33,
                    "more_body": False,
                }
            ],
            max_body_bytes=32,
        )
        self.assertEqual(messages[0]["status"], 413)

    def test_body_limit_rejects_multiple_chunked_events_without_content_length(self):
        messages, downstream, receive_count = self._run_body_middleware(
            [
                {"type": "http.request", "body": b"a" * 20, "more_body": True},
                {"type": "http.request", "body": b"b" * 13, "more_body": True},
                {"type": "http.request", "body": b"c", "more_body": False},
            ],
            max_body_bytes=32,
        )

        self.assertEqual(messages[0]["status"], 413)
        self.assertEqual(receive_count, 2)
        self.assertEqual(
            [message["type"] for message in downstream],
            ["http.request", "http.disconnect"],
        )
        self.assertEqual(downstream[0]["body"], b"a" * 20)

    def test_body_limit_accepts_multiple_events_within_limit(self):
        messages, downstream, receive_count = self._run_body_middleware(
            [
                {"type": "http.request", "body": b"a" * 16, "more_body": True},
                {"type": "http.request", "body": b"b" * 16, "more_body": False},
            ],
            max_body_bytes=32,
        )

        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(receive_count, 2)
        self.assertEqual(
            b"".join(message["body"] for message in downstream),
            b"a" * 16 + b"b" * 16,
        )

    def test_body_limit_preserves_client_disconnect_during_upload(self):
        messages, downstream, receive_count = self._run_body_middleware(
            [
                {"type": "http.request", "body": b"partial", "more_body": True},
                {"type": "http.disconnect"},
            ],
            max_body_bytes=32,
        )

        self.assertEqual(messages[0]["status"], 400)
        self.assertEqual(receive_count, 2)
        self.assertEqual(downstream[-1]["type"], "http.disconnect")

    def test_body_limit_repeated_receive_after_overflow_stays_disconnected(self):
        messages, downstream, receive_count = self._run_body_middleware(
            [
                {"type": "http.request", "body": b"a" * 20, "more_body": True},
                {"type": "http.request", "body": b"b" * 13, "more_body": True},
                {"type": "http.request", "body": b"unread", "more_body": False},
            ],
            max_body_bytes=32,
            repeated_reads=4,
        )

        self.assertEqual(messages[0]["status"], 413)
        self.assertEqual(receive_count, 2)
        self.assertEqual(downstream[0]["type"], "http.request")
        self.assertEqual(
            [message["type"] for message in downstream[1:]],
            ["http.disconnect", "http.disconnect", "http.disconnect"],
        )

    def test_upload_is_closed_on_success_and_invalid_image(self) -> None:
        original_close = self.main.UploadFile.close
        closed_uploads: list[object] = []

        async def track_close(upload) -> None:
            closed_uploads.append(upload)
            await original_close(upload)

        with patch.object(self.main.UploadFile, "close", track_close):
            self.assertEqual(self._post().status_code, 200)
            self.assertEqual(
                self._post(image=b"invalid").status_code,
                400,
            )
        self.assertEqual(len(closed_uploads), 2)

    def test_form_data_and_uploads_close_after_success_and_unexpected_field(self):
        closed_forms: list[object] = []
        closed_uploads: list[object] = []
        original_form_close = self.main.FormData.close
        original_upload_close = self.main.UploadFile.close

        async def track_form_close(form) -> None:
            closed_forms.append(form)
            await original_form_close(form)

        async def track_upload_close(upload) -> None:
            closed_uploads.append(upload)
            await original_upload_close(upload)

        with (
            patch.object(self.main.FormData, "close", track_form_close),
            patch.object(self.main.UploadFile, "close", track_upload_close),
        ):
            success = self._post(data={"provider_key": "local_catalog"})
            invalid_form = self._post(data={"unexpected": "field"})

        self.assertEqual(success.status_code, 200)
        self.assertEqual(invalid_form.status_code, 400)
        self.assertEqual(len(closed_forms), 2)
        self.assertEqual(len(closed_uploads), 2)
        self.assertTrue(all(upload.file.closed for upload in closed_uploads))

    def test_malformed_multipart_closes_partially_created_file(self):
        boundary = "malformed-image-boundary"
        boundary_bytes = boundary.encode("ascii")
        body = (
            b"--" + boundary_bytes + b"\r\n"
            b'Content-Disposition: form-data; name="image"; filename="broken.jpg"\r\n'
            b"Content-Type: image/jpeg\r\n\r\n"
            + _jpeg_bytes()
            + b"\r\n--"
            + boundary_bytes
            + b"\r\n"
        )
        opened_files: list[object] = []
        original_factory = formparsers.SpooledTemporaryFile

        def track_spooled_file(*args, **kwargs):
            file_object = original_factory(*args, **kwargs)
            opened_files.append(file_object)
            return file_object

        with patch.object(
            formparsers,
            "SpooledTemporaryFile",
            track_spooled_file,
        ):
            response = self._post_raw_multipart(body, boundary)

        self.assertEqual(response.status_code, 400)
        self.assertTrue(opened_files)
        self.assertTrue(all(file_object.closed for file_object in opened_files))

    def test_truncation_during_file_payload_closes_parser_file(self):
        boundary = "truncated-file-payload"
        body = (
            f"--{boundary}\r\n".encode("ascii")
            + b'Content-Disposition: form-data; name="image"; filename="partial.jpg"\r\n'
            + b"Content-Type: image/jpeg\r\n\r\n"
            + _jpeg_bytes()[:12]
        )
        opened_files: list[object] = []
        original_factory = formparsers.SpooledTemporaryFile

        def track_spooled_file(*args, **kwargs):
            file_object = original_factory(*args, **kwargs)
            opened_files.append(file_object)
            return file_object

        with patch.object(
            formparsers,
            "SpooledTemporaryFile",
            track_spooled_file,
        ):
            response = self._post_raw_multipart(body, boundary)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(opened_files), 1)
        self.assertTrue(all(file_object.closed for file_object in opened_files))

    def test_truncation_during_later_field_closes_completed_file(self):
        boundary = "truncated-later-field"
        body = (
            f"--{boundary}\r\n".encode("ascii")
            + b'Content-Disposition: form-data; name="image"; filename="complete.jpg"\r\n'
            + b"Content-Type: image/jpeg\r\n\r\n"
            + _jpeg_bytes()
            + f"\r\n--{boundary}\r\n".encode("ascii")
            + b'Content-Disposition: form-data; name="provider_key"\r\n\r\n'
            + b"local_catalog"
        )
        opened_files: list[object] = []
        original_factory = formparsers.SpooledTemporaryFile

        def track_spooled_file(*args, **kwargs):
            file_object = original_factory(*args, **kwargs)
            opened_files.append(file_object)
            return file_object

        with patch.object(
            formparsers,
            "SpooledTemporaryFile",
            track_spooled_file,
        ):
            response = self._post_raw_multipart(body, boundary)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(opened_files), 1)
        self.assertTrue(all(file_object.closed for file_object in opened_files))

    def test_valid_multipart_closes_parser_created_file(self):
        opened_files: list[object] = []
        original_factory = formparsers.SpooledTemporaryFile

        def track_spooled_file(*args, **kwargs):
            file_object = original_factory(*args, **kwargs)
            opened_files.append(file_object)
            return file_object

        with patch.object(
            formparsers,
            "SpooledTemporaryFile",
            track_spooled_file,
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(opened_files), 1)
        self.assertTrue(all(file_object.closed for file_object in opened_files))

    def test_parser_exception_closes_created_file(self):
        boundary = "parser-exception-after-file"
        body = (
            f"--{boundary}\r\n".encode("ascii")
            + b'Content-Disposition: form-data; name="image"; filename="broken.jpg"\r\n'
            + b"Content-Type: image/jpeg\r\n\r\n"
            + _jpeg_bytes()
            + f"\r\n--{boundary}--\r\n".encode("ascii")
        )
        opened_files: list[object] = []
        original_factory = formparsers.SpooledTemporaryFile
        original_on_part_data = self.main._InMemoryImageSearchParser.on_part_data

        def track_spooled_file(*args, **kwargs):
            file_object = original_factory(*args, **kwargs)
            opened_files.append(file_object)
            return file_object

        def fail_after_file_data(parser, data, start, end):
            original_on_part_data(parser, data, start, end)
            if parser._current_part.file is not None and end > start:
                raise formparsers.MultiPartException("injected parser failure")

        with (
            patch.object(
                formparsers,
                "SpooledTemporaryFile",
                track_spooled_file,
            ),
            patch.object(
                self.main._InMemoryImageSearchParser,
                "on_part_data",
                fail_after_file_data,
            ),
        ):
            response = self._post_raw_multipart(body, boundary)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(opened_files), 1)
        self.assertTrue(all(file_object.closed for file_object in opened_files))

    def test_upload_parse_admission_is_acquired_before_multipart_parsing(self):
        admission = self.main._UploadParsingAdmission(max_admitted=1)
        self.main.app.state.image_upload_parse_admission = admission
        original_parse = self.main._InMemoryImageSearchParser.parse
        parse_observations: list[bool] = []

        async def observe_parse(parser):
            probe = admission.reserve()
            parse_observations.append(probe is None)
            if probe is not None:
                probe.release()
            return await original_parse(parser)

        with patch.object(
            self.main._InMemoryImageSearchParser,
            "parse",
            observe_parse,
        ):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_observations, [True])

    def test_upload_parse_admission_rejects_when_capacity_is_full(self):
        admission = self.main._UploadParsingAdmission(max_admitted=1)
        self.main.app.state.image_upload_parse_admission = admission
        parser_started = Event()
        release_parser = Event()
        parse_calls: list[bool] = []
        original_parse = self.main._InMemoryImageSearchParser.parse

        async def observe_parse(parser):
            parse_calls.append(True)
            if len(parse_calls) == 1:
                parser_started.set()
                await asyncio.to_thread(release_parser.wait, 5)
            return await original_parse(parser)

        first_responses: list[object] = []

        def send_first_request() -> None:
            first_responses.append(self._post())

        try:
            with patch.object(
                self.main._InMemoryImageSearchParser,
                "parse",
                observe_parse,
            ):
                first_request = Thread(target=send_first_request, daemon=True)
                first_request.start()
                self.assertTrue(parser_started.wait(timeout=2))
                concurrent_response = self._post()
                self.assertEqual(concurrent_response.status_code, 503)
                release_parser.set()
                first_request.join(timeout=3)
                self.assertFalse(first_request.is_alive())
            self.assertEqual(len(first_responses), 1)
            self.assertEqual(first_responses[0].status_code, 200)
            self.assertEqual(parse_calls, [True])
        finally:
            release_parser.set()

    def test_upload_parse_admission_released_after_success(self):
        admission = self.main._UploadParsingAdmission(max_admitted=1)
        self.main.app.state.image_upload_parse_admission = admission

        response = self._post()
        available = admission.reserve()

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(available)
        assert available is not None
        available.release()

    def test_upload_parse_admission_released_after_malformed_request(self):
        admission = self.main._UploadParsingAdmission(max_admitted=1)
        self.main.app.state.image_upload_parse_admission = admission
        boundary = "admission-malformed"
        body = (
            f"--{boundary}\r\n".encode("ascii")
            + b'Content-Disposition: form-data; name="image"; filename="partial.jpg"\r\n'
            + b"Content-Type: image/jpeg\r\n\r\npartial"
        )

        malformed = self._post_raw_multipart(body, boundary)
        available = admission.reserve()

        self.assertEqual(malformed.status_code, 400)
        self.assertIsNotNone(available)
        assert available is not None
        available.release()

    def test_upload_parse_admission_is_reusable_after_failed_request(self):
        admission = self.main._UploadParsingAdmission(max_admitted=1)
        self.main.app.state.image_upload_parse_admission = admission

        failed = self._post(image=b"not an image")
        succeeded = self._post()
        available = admission.reserve()

        self.assertEqual(failed.status_code, 400)
        self.assertEqual(succeeded.status_code, 200)
        self.assertIsNotNone(available)
        assert available is not None
        available.release()

    def test_upload_parse_admission_released_on_disconnect_and_cancellation(self):
        boundary = "interrupted-upload"
        prefix = (
            f"--{boundary}\r\n".encode("ascii")
            + b'Content-Disposition: form-data; name="image"; filename="partial.jpg"\r\n'
            + b"Content-Type: image/jpeg\r\n\r\npartial"
        )

        for interruption in ("disconnect", "cancellation"):
            with self.subTest(interruption=interruption):
                admission = self.main._UploadParsingAdmission(max_admitted=1)
                self.main.app.state.image_upload_parse_admission = admission
                opened_files: list[object] = []
                original_factory = formparsers.SpooledTemporaryFile
                receive_index = 0

                def track_spooled_file(*args, **kwargs):
                    file_object = original_factory(*args, **kwargs)
                    opened_files.append(file_object)
                    return file_object

                async def receive():
                    nonlocal receive_index
                    if receive_index == 0:
                        receive_index += 1
                        return {
                            "type": "http.request",
                            "body": prefix,
                            "more_body": True,
                        }
                    if interruption == "disconnect":
                        return {"type": "http.disconnect"}
                    raise asyncio.CancelledError

                with patch.object(
                    formparsers,
                    "SpooledTemporaryFile",
                    track_spooled_file,
                ):
                    if interruption == "disconnect":
                        with self.assertRaises(ClientDisconnect):
                            self._invoke_image_route_with_receive(
                                receive,
                                boundary,
                            )
                    else:
                        with self.assertRaises(asyncio.CancelledError):
                            self._invoke_image_route_with_receive(
                                receive,
                                boundary,
                            )

                self.assertEqual(len(opened_files), 1)
                self.assertTrue(all(file_object.closed for file_object in opened_files))
                available = admission.reserve()
                self.assertIsNotNone(available)
                assert available is not None
                available.release()

    def test_actual_task_cancel_closes_partial_upload_and_releases_admission(self):
        boundary = "actual-task-cancellation"
        prefix = (
            f"--{boundary}\r\n".encode("ascii")
            + b'Content-Disposition: form-data; name="image"; filename="partial.jpg"\r\n'
            + b"Content-Type: image/jpeg\r\n\r\npartial"
        )
        admission = self.main._UploadParsingAdmission(max_admitted=1)
        self.main.app.state.image_upload_parse_admission = admission
        opened_files: list[object] = []
        original_factory = formparsers.SpooledTemporaryFile

        def track_spooled_file(*args, **kwargs):
            file_object = original_factory(*args, **kwargs)
            opened_files.append(file_object)
            return file_object

        async def run_route_cancellation() -> None:
            receive_waiting = asyncio.Event()
            release_receive = asyncio.Event()
            receive_count = 0

            async def receive():
                nonlocal receive_count
                if receive_count == 0:
                    receive_count += 1
                    return {
                        "type": "http.request",
                        "body": prefix,
                        "more_body": True,
                    }
                receive_waiting.set()
                await release_receive.wait()
                return {"type": "http.disconnect"}

            path = "/api/offers/search-by-image"
            request = Request(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": path,
                    "raw_path": path.encode("ascii"),
                    "query_string": b"",
                    "headers": [
                        (b"origin", b"http://localhost:5174"),
                        (
                            b"content-type",
                            f"multipart/form-data; boundary={boundary}".encode(
                                "ascii"
                            ),
                        ),
                        (b"content-length", b"1024"),
                    ],
                    "client": ("testclient", 123),
                    "server": ("testserver", 80),
                    "app": self.main.app,
                },
                receive,
            )
            route_task = asyncio.create_task(
                self.main.api_offer_search_by_image(
                    request,
                    "test-owner",
                    self.main.PROVIDER_REGISTRY,
                )
            )
            try:
                await asyncio.wait_for(receive_waiting.wait(), timeout=2)
                self.assertEqual(len(opened_files), 1)
                self.assertFalse(opened_files[0].closed)
                self.assertTrue(route_task.cancel())
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(route_task, timeout=2)
            finally:
                if not route_task.done():
                    route_task.cancel()
                    release_receive.set()
                    try:
                        await asyncio.wait_for(route_task, timeout=2)
                    except asyncio.CancelledError:
                        pass

            self.assertTrue(all(file_object.closed for file_object in opened_files))
            available = admission.reserve()
            self.assertIsNotNone(available)
            assert available is not None
            available.release()

        with patch.object(
            formparsers,
            "SpooledTemporaryFile",
            track_spooled_file,
        ):
            asyncio.run(run_route_cancellation())

    def test_duplicate_file_fields_are_rejected_and_partial_files_closed(self):
        opened_files: list[object] = []
        original_factory = formparsers.SpooledTemporaryFile

        def track_spooled_file(*args, **kwargs):
            file_object = original_factory(*args, **kwargs)
            opened_files.append(file_object)
            return file_object

        with patch.object(
            formparsers,
            "SpooledTemporaryFile",
            track_spooled_file,
        ):
            response = self.client.post(
                "/api/offers/search-by-image",
                files=[
                    ("image", ("first.jpg", _jpeg_bytes(), "image/jpeg")),
                    ("image", ("second.jpg", _jpeg_bytes(), "image/jpeg")),
                ],
                headers={"Origin": "http://localhost:5174"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertTrue(opened_files)
        self.assertTrue(all(file_object.closed for file_object in opened_files))

    def test_upload_read_is_bounded_to_image_limit_plus_one(self) -> None:
        original_read = self.main.UploadFile.read
        read_sizes: list[int] = []

        async def track_read(upload, size: int = -1) -> bytes:
            read_sizes.append(size)
            return await original_read(upload, size)

        with patch.object(self.main.UploadFile, "read", track_read):
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(read_sizes, [self.main.MAX_UPLOAD_SIZE_BYTES + 1])

    def test_filter_validation_and_explicit_currency(self) -> None:
        missing_currency = self._post(data={"max_price": "10"})
        self.assertEqual(missing_currency.status_code, 400)

        invalid_currency = self._post(
            data={"max_price": "10", "currency": "XXX"}
        )
        self.assertEqual(invalid_currency.status_code, 400)

        invalid_boolean = self._post(data={"is_organic": "not-a-bool"})
        self.assertEqual(invalid_boolean.status_code, 400)

    def test_usd_and_inr_filters_never_cross_currencies(self) -> None:
        usd = self._post(
            data={
                "max_price": "10",
                "currency": "USD",
                "provider_key": "local_catalog",
            }
        )
        inr = self._post(
            data={
                "max_price": "300",
                "currency": "INR",
                "provider_key": "local_catalog",
            }
        )

        self.assertEqual(usd.status_code, 200)
        self.assertEqual(inr.status_code, 200)
        self.assertEqual(
            {offer["currency"] for offer in usd.json()["offers"]},
            {"USD"},
        )
        self.assertEqual(
            {offer["currency"] for offer in inr.json()["offers"]},
            {"INR"},
        )

    def test_provider_failure_and_model_errors_are_sanitized(self) -> None:
        provider_failure = self._post(
            data={"provider_key": "failing_provider"}
        )
        self.assertEqual(provider_failure.status_code, 502)
        self.assertIn("providers", provider_failure.json())
        self.assertNotIn("private provider exception", provider_failure.text)

        self.analyzer.error = VisionProviderError("sensitive provider detail")
        model_failure = self._post()
        self.assertEqual(model_failure.status_code, 503)
        self.assertNotIn("sensitive provider detail", model_failure.text)

        self.analyzer.error = InvalidVisionOutputError("raw malformed response")
        malformed = self._post()
        self.assertEqual(malformed.status_code, 502)
        self.assertNotIn("raw malformed response", malformed.text)

    def test_per_user_rate_limit_and_global_admission_overload(self) -> None:
        self.main.app.state.visual_search_rate_limiter._max_requests = 1
        first = self._post()
        self.assertEqual(first.status_code, 200)
        repeated = [self._post() for _ in range(20)]
        self.assertTrue(all(response.status_code == 429 for response in repeated))
        tracked = self.main.app.state.visual_search_rate_limiter._requests
        self.assertEqual(tuple(tracked), ("test-owner",))
        self.assertEqual(len(tracked["test-owner"]), 1)

        self.main.app.state.visual_search_rate_limiter._max_requests = 5
        executor = self.main.app.state.visual_request_executor
        reservations = [executor.reserve() for _ in range(4)]
        try:
            overloaded = self._post()
            self.assertEqual(overloaded.status_code, 503)
        finally:
            for reservation in reservations:
                reservation.release_if_unsubmitted()

    def test_rate_limiter_expiry_and_tracked_user_cap(self) -> None:
        now = [100.0]
        with patch.object(
            self.main.time,
            "monotonic",
            side_effect=lambda: now[0],
        ):
            expiring = self.main._PerUserRateLimiter(
                max_requests=1,
                window_seconds=10,
                max_users=2,
            )
            self.assertTrue(expiring.allow("owner-a"))
            now[0] = 109.0
            self.assertFalse(expiring.allow("owner-a"))
            now[0] = 110.0
            self.assertTrue(expiring.allow("owner-a"))

            now[0] = 200.0
            bounded = self.main._PerUserRateLimiter(
                max_requests=2,
                window_seconds=10,
                max_users=2,
            )
            accepted = [
                bounded.allow("owner-a")
                for _ in range(20)
            ]
            self.assertEqual(sum(accepted), 2)
            self.assertTrue(bounded.allow("owner-b"))
            self.assertFalse(bounded.allow("owner-c"))
            self.assertEqual(len(bounded._requests), 2)
            self.assertLessEqual(len(bounded._requests["owner-a"]), 2)

            now[0] = 210.0
            self.assertTrue(bounded.allow("owner-c"))
            self.assertEqual(tuple(bounded._requests), ("owner-c",))

    def test_submission_failure_releases_admission_exactly_once(self) -> None:
        executor = self.main._VisionRequestExecutor(
            max_workers=1,
            max_admitted=1,
        )
        try:
            admission = executor.reserve()
            with patch.object(
                executor._executor,
                "submit",
                side_effect=RuntimeError("submission failure"),
            ):
                with self.assertRaises(self.main._VisionUnavailableError):
                    admission.submit(lambda: None)

            admission.release_if_unsubmitted()
            next_admission = executor.reserve()
            next_admission.release_if_unsubmitted()
            final_admission = executor.reserve()
            final_admission.release_if_unsubmitted()
        finally:
            executor.shutdown()

    def test_shutdown_cancels_queued_work_without_waiting_for_running_work(self):
        executor = self.main._VisionRequestExecutor(
            max_workers=1,
            max_admitted=2,
        )
        worker_started = Event()
        release_worker = Event()
        running_done = Event()
        queued_done = Event()
        shutdown_done = Event()

        def blocking_work() -> str:
            worker_started.set()
            release_worker.wait(timeout=5)
            return "finished"

        def shutdown_executor() -> None:
            try:
                executor.shutdown()
            finally:
                shutdown_done.set()

        shutdown_thread = Thread(target=shutdown_executor, daemon=True)
        shutdown_thread_started = False
        try:
            running_admission = executor.reserve()
            running_future = running_admission.submit(blocking_work)
            running_future.add_done_callback(
                lambda completed: running_done.set()
            )
            self.assertTrue(worker_started.wait(timeout=2))

            queued_admission = executor.reserve()
            queued_future = queued_admission.submit(lambda: "queued")
            queued_future.add_done_callback(
                lambda completed: queued_done.set()
            )

            shutdown_thread.start()
            shutdown_thread_started = True
            self.assertTrue(shutdown_done.wait(timeout=2))
            self.assertTrue(queued_future.cancelled())
            self.assertTrue(queued_done.wait(timeout=2))
            self.assertFalse(running_future.done())

            release_worker.set()
            self.assertTrue(running_done.wait(timeout=2))
            self.assertEqual(running_future.result(timeout=2), "finished")
            self.assertEqual(executor._admission._value, 2)
        finally:
            release_worker.set()
            if shutdown_thread_started:
                shutdown_thread.join(timeout=2)
            else:
                executor.shutdown()

    def test_outer_timeout_retains_admission_until_worker_finishes(self) -> None:
        setattr(self.main, "VISION_OUTER_DEADLINE_SECONDS", 0.05)
        finished = Event()
        started = Event()
        task_completed = Event()
        self.vision_finished = finished
        service = self.main.app.state.visual_discovery_service
        original_discover = service.discover
        executor = self.main.app.state.visual_request_executor
        original_submit = executor._submit

        def blocking_discover(image, **kwargs):
            started.set()
            finished.wait(timeout=5)
            return original_discover(image, **kwargs)

        def observe_submit(slot, function, *args, **kwargs):
            future = original_submit(slot, function, *args, **kwargs)
            future.add_done_callback(lambda completed: task_completed.set())
            return future

        service.discover = blocking_discover
        with patch.object(executor, "_submit", side_effect=observe_submit):
            response = self._post()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(started.is_set())

        reservations = [executor.reserve() for _ in range(3)]
        try:
            with self.assertRaises(self.main._VisionOverloadedError):
                executor.reserve()
        finally:
            for reservation in reservations:
                reservation.release_if_unsubmitted()

        finished.set()
        self.assertTrue(task_completed.wait(timeout=2))
        released_admissions = [executor.reserve() for _ in range(4)]
        for admission in released_admissions:
            admission.release_if_unsubmitted()

    def test_existing_text_search_contract_is_unchanged(self) -> None:
        response = self.client.get("/api/offers/search", params={"query": "honey"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue({"offers", "providers", "groups", "price_summaries"} <= payload.keys())
        self.assertEqual(len(payload["offers"]), 3)
        self.assertIn("partial", payload)


if __name__ == "__main__":
    unittest.main()
