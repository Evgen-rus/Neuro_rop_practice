from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage.rop_db import (
    activate_auth_user,
    clear_auth_login_attempts,
    connect,
    create_auth_session,
    create_auth_user,
    deactivate_auth_user,
    digest_auth_token,
    get_auth_login_throttle,
    get_auth_session,
    get_auth_user,
    hash_auth_password,
    init_db,
    list_auth_users,
    record_auth_login_attempt,
    revoke_auth_session,
    revoke_auth_user_sessions,
    set_auth_user_password,
    update_auth_user,
    verify_auth_password,
)


class AuthStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "auth.sqlite"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_admin(self, login: str = "admin") -> dict:
        return create_auth_user(
            self.db_path,
            login=login,
            password_hash=hash_auth_password("Admin-pass-123!"),
            role="admin",
        )

    def test_password_helpers_and_rows_do_not_leak_hash_by_default(self) -> None:
        password = "Пароль менеджера 123!"
        password_hash = hash_auth_password(password)
        self.assertTrue(password_hash.startswith("$argon2id$"))
        self.assertTrue(verify_auth_password(password, password_hash))
        self.assertFalse(verify_auth_password("wrong", password_hash))

        user = create_auth_user(
            self.db_path,
            login="Admin@Example.test",
            password_hash=password_hash,
            role="admin",
        )
        self.assertEqual(user["login"], "admin@example.test")
        self.assertEqual(user["password_hash"], password_hash)
        public = get_auth_user(self.db_path, login="ADMIN@example.test")
        self.assertIsNotNone(public)
        assert public is not None
        self.assertNotIn("password_hash", public)
        private = get_auth_user(
            self.db_path,
            user_id=int(user["id"]),
            include_password_hash=True,
        )
        self.assertEqual(private["password_hash"], password_hash)
        self.assertNotIn(password, password_hash)
        self.assertNotIn("password_hash", list_auth_users(self.db_path)[0])

        with connect(self.db_path) as conn:
            raw = conn.execute(
                "SELECT password_hash FROM auth_users WHERE id = ?",
                (int(user["id"]),),
            ).fetchone()[0]
        self.assertEqual(raw, password_hash)
        self.assertNotIn(password, raw)

    def test_manager_and_login_invariants_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "Сначала создайте активного администратора"):
            create_auth_user(
                self.db_path,
                login="manager-before-admin",
                password_hash="hash",
                role="manager",
                manager_id="10",
            )
        admin = self._create_admin()

        inactive_manager = create_auth_user(
            self.db_path,
            login="manager-inactive",
            password_hash="hash",
            role="manager",
            is_active=False,
        )
        self.assertFalse(inactive_manager["is_active"])
        manager = create_auth_user(
            self.db_path,
            login="manager-one",
            password_hash="hash",
            role="manager",
            manager_id="10",
        )
        self.assertEqual(manager["manager_id"], "10")
        with self.assertRaisesRegex(ValueError, "manager_id"):
            create_auth_user(
                self.db_path,
                login="manager-no-id",
                password_hash="hash",
                role="manager",
            )
        with self.assertRaisesRegex(ValueError, "manager_id"):
            create_auth_user(
                self.db_path,
                login="rop-with-id",
                password_hash="hash",
                role="rop",
                manager_id="11",
            )
        with self.assertRaisesRegex(ValueError, "занят"):
            create_auth_user(
                self.db_path,
                login=" ADMIN ",
                password_hash="hash",
                role="rop",
            )
        with self.assertRaisesRegex(ValueError, "manager_id"):
            create_auth_user(
                self.db_path,
                login="manager-two",
                password_hash="hash",
                role="manager",
                manager_id="10",
            )
        self.assertEqual(admin["role"], "admin")

    def test_last_active_admin_is_protected(self) -> None:
        first = self._create_admin("first-admin")
        with self.assertRaisesRegex(ValueError, "последнего активного администратора"):
            deactivate_auth_user(self.db_path, user_id=int(first["id"]))
        with self.assertRaisesRegex(ValueError, "последнего активного администратора"):
            update_auth_user(self.db_path, user_id=int(first["id"]), role="rop")

        second = self._create_admin("second-admin")
        deactivated = deactivate_auth_user(self.db_path, user_id=int(first["id"]))
        self.assertFalse(deactivated["is_active"])
        with self.assertRaisesRegex(ValueError, "последнего активного администратора"):
            deactivate_auth_user(self.db_path, user_id=int(second["id"]))

    def test_sessions_expire_revoke_and_follow_user_state(self) -> None:
        user = self._create_admin()
        token = "opaque-token-for-test"
        digest = digest_auth_token(token)
        session = create_auth_session(
            self.db_path,
            user_id=int(user["id"]),
            token_digest=digest,
            expires_at="2026-08-12T12:10:00+03:00",
            created_at="2026-08-12T12:00:00+03:00",
        )
        self.assertEqual(session["token_digest"], digest)
        found = get_auth_session(
            self.db_path,
            token_digest=digest,
            now="2026-08-12T12:05:00+03:00",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["user_login"], "admin")
        self.assertEqual(
            get_auth_session(
                self.db_path,
                token_digest=digest,
                now="2026-08-12T12:10:00+03:00",
            ),
            None,
        )

        second_digest = digest_auth_token("second-token")
        create_auth_session(
            self.db_path,
            user_id=int(user["id"]),
            token_digest=second_digest,
            expires_at="2026-08-13T12:10:00+03:00",
        )
        self.assertTrue(revoke_auth_session(self.db_path, token_digest=second_digest))
        self.assertFalse(revoke_auth_session(self.db_path, token_digest=second_digest))

        third_digest = digest_auth_token("third-token")
        create_auth_session(
            self.db_path,
            user_id=int(user["id"]),
            token_digest=third_digest,
            expires_at="2026-08-13T12:10:00+03:00",
        )
        updated = set_auth_user_password(
            self.db_path,
            user_id=int(user["id"]),
            password_hash=hash_auth_password("New-password-123!"),
        )
        self.assertNotEqual(updated["password_hash"], user["password_hash"])
        self.assertIsNone(
            get_auth_session(self.db_path, token_digest=third_digest, now="2026-08-12T12:05:00+03:00")
        )

    def test_deactivation_revokes_all_sessions_and_reactivation_requires_manager_id(self) -> None:
        self._create_admin()
        manager = create_auth_user(
            self.db_path,
            login="inactive-manager",
            password_hash="hash",
            role="manager",
            manager_id="77",
        )
        token_digest = digest_auth_token("manager-token")
        create_auth_session(
            self.db_path,
            user_id=int(manager["id"]),
            token_digest=token_digest,
            expires_at="2026-08-13T12:00:00+03:00",
        )
        deactivated = deactivate_auth_user(self.db_path, user_id=int(manager["id"]))
        self.assertFalse(deactivated["is_active"])
        self.assertIsNone(
            get_auth_session(self.db_path, token_digest=token_digest, now="2026-08-12T12:00:00+03:00")
        )
        with self.assertRaisesRegex(ValueError, "manager_id"):
            update_auth_user(self.db_path, user_id=int(manager["id"]), manager_id=None, is_active=True)
        activated = activate_auth_user(self.db_path, user_id=int(manager["id"]))
        self.assertTrue(activated["is_active"])
        self.assertEqual(activated["manager_id"], "77")
        self.assertEqual(revoke_auth_user_sessions(self.db_path, user_id=int(manager["id"])), 0)

    def test_login_throttle_is_scoped_and_can_be_cleared_by_success(self) -> None:
        base = "2026-08-12T12:00:00+03:00"
        for minute in range(5):
            record_auth_login_attempt(
                self.db_path,
                login="User@Example.test",
                client_ip="192.0.2.10",
                attempted_at=f"2026-08-12T12:0{minute}:00+03:00",
                succeeded=False,
            )
        throttle = get_auth_login_throttle(
            self.db_path,
            login="user@example.test",
            client_ip="192.0.2.10",
            now="2026-08-12T12:05:00+03:00",
            window_seconds=900,
            max_attempts=5,
        )
        self.assertIsNotNone(throttle)
        assert throttle is not None
        self.assertEqual(throttle["failure_count"], 5)
        self.assertEqual(throttle["first_failed_at"], base)
        self.assertTrue(throttle["is_locked"])
        self.assertIsNone(
            get_auth_login_throttle(
                self.db_path,
                login="user@example.test",
                client_ip="192.0.2.11",
                now="2026-08-12T12:05:00+03:00",
            )
        )
        record_auth_login_attempt(
            self.db_path,
            login="user@example.test",
            client_ip="192.0.2.10",
            attempted_at="2026-08-12T12:06:00+03:00",
            succeeded=True,
        )
        self.assertIsNone(
            get_auth_login_throttle(
                self.db_path,
                login="user@example.test",
                client_ip="192.0.2.10",
                now="2026-08-12T12:07:00+03:00",
            )
        )
        clear_auth_login_attempts(
            self.db_path,
            login="user@example.test",
            client_ip="192.0.2.10",
        )
        with connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_login_attempts").fetchone()[0], 0)

    def test_cli_uses_getpass_and_does_not_print_password_hash(self) -> None:
        from scripts import manage_user

        output = io.StringIO()
        with patch.object(manage_user.getpass, "getpass", side_effect=["Cli-pass-123!", "Cli-pass-123!"]), contextlib.redirect_stdout(output):
            self.assertEqual(
                manage_user.main(
                    [
                        "--db",
                        str(self.db_path),
                        "create",
                        "--login",
                        "cli-admin",
                        "--role",
                        "admin",
                    ]
                ),
                0,
            )
        created_output = output.getvalue()
        self.assertIn("login=cli-admin", created_output)
        self.assertNotIn("Cli-pass-123!", created_output)
        self.assertNotIn("password_hash", created_output)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                manage_user.main(["--db", str(self.db_path), "list"]),
                0,
            )
        self.assertNotIn("password_hash", output.getvalue())


class AuthMigrationTests(unittest.TestCase):
    def test_auth_schema_and_migration_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "migration.sqlite"
            init_db(db_path)
            init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                migration = conn.execute(
                    "SELECT COUNT(*) FROM local_migrations WHERE migration_id = ?",
                    ("2026-08-12-auth-core",),
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertTrue({"auth_users", "auth_sessions", "auth_login_attempts"}.issubset(tables))
            self.assertEqual(migration, 1)


if __name__ == "__main__":
    unittest.main()
