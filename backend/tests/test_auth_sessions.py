import hashlib
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.db import transaction
from backend.tests._support import (
    create_cart_schema,
    create_confirmation_schema,
    import_main_with_temporary_auth_db,
    prepare_test_schemas,
)


main = import_main_with_temporary_auth_db()


def _create_business_database(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)
        connection.commit()


class AuthSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.business_path = self.root / "store.sqlite"
        self.authentication_path = self.root / "auth.sqlite"
        _create_business_database(self.business_path)
        prepare_test_schemas(self.business_path, self.authentication_path)

        self.original_business_path = main.STORE_DB_PATH
        self.original_authentication_path = main.AUTH_DB_PATH
        main.STORE_DB_PATH = self.business_path
        main.AUTH_DB_PATH = self.authentication_path

        self.vision_patch = patch.object(
            main,
            "create_vision_analyzer",
            side_effect=ValueError("not used by this test"),
        )
        self.vision_patch.start()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.engines = main.app.state.database_engines
        self.business_pool = self.engines.business.pool
        self.authentication_pool = self.engines.authentication.pool
        self.authentication_engine = self.engines.authentication

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.vision_patch.stop()
        main.STORE_DB_PATH = self.original_business_path
        main.AUTH_DB_PATH = self.original_authentication_path
        self.temp_dir.cleanup()
        self.assertIsNone(main.app.state.database_engines)
        self.assertIsNot(self.engines.business.pool, self.business_pool)
        self.assertIsNot(
            self.engines.authentication.pool,
            self.authentication_pool,
        )

    def test_session_tokens_are_hashed_and_retrievable(self):
        token = main.create_session("owner-1", self.authentication_engine)

        self.assertEqual(
            main.get_session_owner(token, self.authentication_engine),
            "owner-1",
        )
        with self.authentication_engine.connect() as connection:
            stored_hash = connection.execute(
                text("SELECT session_hash FROM auth_sessions")
            ).scalar_one()
        self.assertEqual(stored_hash, main.hash_session_token(token))
        self.assertNotEqual(stored_hash, token)

    def test_expired_sessions_are_removed_during_retrieval(self):
        token = main.create_session("owner-expired", self.authentication_engine)
        with transaction(self.authentication_engine) as connection:
            connection.execute(
                text("""
                    UPDATE auth_sessions
                    SET expires_at = :expires_at
                    WHERE session_hash = :session_hash
                """),
                {
                    "expires_at": time.time() - 1,
                    "session_hash": main.hash_session_token(token),
                },
            )

        self.assertIsNone(
            main.get_session_owner(token, self.authentication_engine)
        )
        with self.authentication_engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM auth_sessions")
            ).scalar_one()
        self.assertEqual(count, 0)

    def test_session_deletion_invalidates_token(self):
        token = main.create_session("owner-delete", self.authentication_engine)
        main.delete_session(token, self.authentication_engine)

        self.assertIsNone(
            main.get_session_owner(token, self.authentication_engine)
        )

    def test_authentication_routes_preserve_session_behavior(self):
        token = main.create_session("owner-api", self.authentication_engine)
        response = self.client.get(
            "/auth/me",
            cookies={main.SESSION_COOKIE_NAME: token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"authenticated": True})

        logout = self.client.post(
            "/auth/logout",
            cookies={main.SESSION_COOKIE_NAME: token},
        )
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(logout.json()["authenticated"], False)
        self.assertEqual(
            main.get_session_owner(token, self.authentication_engine),
            None,
        )

    def test_google_callback_creates_hashed_session(self):
        identity = {
            "userinfo": {
                "iss": "https://accounts.google.com",
                "sub": "temporary-test-subject",
            },
        }
        with (
            patch.object(
                main,
                "FRONTEND_ORIGINS",
                ("https://shop.example.test",),
            ),
            patch.object(
                main.oauth.google,
                "authorize_access_token",
                new=AsyncMock(return_value=identity),
            ),
        ):
            response = self.client.get(
                "/auth/google/callback?next=https://attacker.example",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "https://shop.example.test/dashboard",
        )
        token = response.cookies.get(main.SESSION_COOKIE_NAME)
        self.assertTrue(token)
        expected_owner = hashlib.sha256(
            b"https://accounts.google.com\0temporary-test-subject"
        ).hexdigest()
        self.assertEqual(
            main.get_session_owner(token, self.authentication_engine),
            expected_owner,
        )
        self.assertEqual(
            self.client.get("/auth/me").json(),
            {"authenticated": True},
        )


class AuthSchemaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.business_path = self.root / "store.sqlite"
        self.authentication_path = self.root / "auth.sqlite"
        _create_business_database(self.business_path)
        create_cart_schema(self.business_path)
        create_confirmation_schema(self.business_path)
        with closing(sqlite3.connect(self.authentication_path)):
            pass

        self.original_business_path = main.STORE_DB_PATH
        self.original_authentication_path = main.AUTH_DB_PATH
        main.STORE_DB_PATH = self.business_path
        main.AUTH_DB_PATH = self.authentication_path

    def tearDown(self):
        main.STORE_DB_PATH = self.original_business_path
        main.AUTH_DB_PATH = self.original_authentication_path
        self.temp_dir.cleanup()

    def test_missing_auth_schema_fails_without_creating_tables(self):
        engine_pools = []
        original_create_engines = main.create_engines

        def capture_engines():
            created = original_create_engines()
            engine_pools.append(
                (created, created.business.pool, created.authentication.pool)
            )
            return created

        with patch.object(main, "create_engines", side_effect=capture_engines):
            with self.assertRaisesRegex(RuntimeError, "auth_sessions"):
                with TestClient(main.app):
                    self.fail("Startup unexpectedly succeeded.")

        self.assertEqual(len(engine_pools), 1)
        self.assertIsNone(main.app.state.database_engines)
        with closing(sqlite3.connect(self.authentication_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertNotIn("auth_sessions", tables)
        engines, business_pool, authentication_pool = engine_pools[0]
        self.assertIsNot(business_pool, engines.business.pool)
        self.assertIsNot(authentication_pool, engines.authentication.pool)
