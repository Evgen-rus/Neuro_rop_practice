from __future__ import annotations

import unittest
from unittest.mock import patch


class QuickHelpOpenedApiTests(unittest.TestCase):
    def test_endpoint_passes_opening_context_to_storage(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "require_deal"), patch.object(
            api_app,
            "auth_current_user",
            return_value={"id": 42, "role": "manager", "manager_id": "10"},
        ), patch.object(
            api_app.storage,
            "record_quick_help_opened_event",
            return_value={"id": 7},
            create=True,
        ) as record:
            result = api_app.deal_quick_help_opened_create(
                "101",
                api_app.QuickHelpOpenedRequest(
                    occurrence_id="opening-123",
                    assistant_mode="push",
                    active_quick_help_id=9,
                ),
            )

        self.assertEqual(result, {"ok": True, "event_id": 7})
        self.assertEqual(record.call_args.args, (api_app.DEFAULT_DB_PATH,))
        self.assertEqual(
            record.call_args.kwargs,
            {
                "deal_id": "101",
                "auth_user_id": 42,
                "occurrence_id": "opening-123",
                "entrypoint": "assistant_button",
                "assistant_mode": "push",
                "active_quick_help_id": 9,
            },
        )

    def test_endpoint_maps_storage_permission_error_to_403(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "require_deal"), patch.object(
            api_app,
            "auth_current_user",
            return_value={"id": 7, "role": "manager", "manager_id": "77"},
        ), patch.object(
            api_app.storage,
            "record_quick_help_opened_event",
            side_effect=PermissionError("только менеджер своей сделки"),
            create=True,
        ):
            with self.assertRaises(api_app.HTTPException) as raised:
                api_app.deal_quick_help_opened_create(
                    "101",
                    api_app.QuickHelpOpenedRequest(occurrence_id="opening-123"),
                )

        self.assertEqual(raised.exception.status_code, 403)

    def test_endpoint_maps_storage_validation_error_to_400(self) -> None:
        from api import app as api_app

        with patch.object(api_app, "require_deal"), patch.object(
            api_app,
            "auth_current_user",
            return_value={"id": 42, "role": "manager", "manager_id": "10"},
        ), patch.object(
            api_app.storage,
            "record_quick_help_opened_event",
            side_effect=ValueError("сделка не найдена"),
            create=True,
        ):
            with self.assertRaises(api_app.HTTPException) as raised:
                api_app.deal_quick_help_opened_create(
                    "101",
                    api_app.QuickHelpOpenedRequest(occurrence_id="opening-123"),
                )

        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
