from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.cogs_brokerage import (
    BrokerageAdminAdjustmentModal,
    BrokerageAdminPanelView,
    BrokerageConfigModal,
    BrokerageDeleteModal,
    BrokerageListingModal,
    BrokerageListingView,
    BrokerageNotificationModal,
    BrokeragePanelView,
    BrokerageReportModal,
    BrokerageReviewModal,
    BrokerageReviewRequestView,
    BrokerageTicketView,
    BrokerageVerificationView,
    BrokerageVerificationCodeView,
    BrokerageCog,
    _listing_interval,
    _listing_markdown,
    _profile_markdown,
)


def member(user_id: int = 123, name: str = "tester") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        mention=f"<@{user_id}>",
        display_name=name,
    )

def layout_text(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content
        for item in view.walk_children()
        if isinstance(item, discord.ui.TextDisplay)
    )

def dispatchable_ids(view: discord.ui.LayoutView) -> set[str]:
    return {
        item.custom_id
        for item in view.walk_children()
        if item.is_dispatchable()
    }

def button_with_id(view: discord.ui.LayoutView, custom_id: str) -> discord.ui.Button:
    return next(
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button) and item.custom_id == custom_id
    )

def set_input_value(text_input: discord.ui.TextInput, value: str) -> None:
    # discord.py populates this private state from the modal submission payload.
    text_input._value = value

def close_uploaded_files(kwargs: dict) -> None:
    for uploaded in kwargs.get("files", []):
        uploaded.close()


class BrokerageComponentCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_integrated_panel_buttons_delegate_to_the_expected_flows(self) -> None:
        cog = SimpleNamespace(
            send_profile=AsyncMock(),
            send_notification_settings=AsyncMock(),
        )
        view = BrokeragePanelView(cog)
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock())
        )

        await button_with_id(view, "devilblox:broker:profile").callback(interaction)
        await button_with_id(view, "devilblox:broker:notifications").callback(interaction)
        await button_with_id(view, "devilblox:broker:register").callback(interaction)
        await button_with_id(view, "devilblox:broker:verify").callback(interaction)

        cog.send_profile.assert_awaited_once_with(interaction)
        cog.send_notification_settings.assert_awaited_once_with(interaction)
        listing_modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(listing_modal, BrokerageListingModal)
        verification_kwargs = interaction.response.send_message.await_args.kwargs
        self.assertIsInstance(verification_kwargs["view"], BrokerageVerificationView)
        self.assertTrue(verification_kwargs["ephemeral"])
        close_uploaded_files(verification_kwargs)

    async def test_listing_buttons_delegate_with_the_listing_id(self) -> None:
        cog = SimpleNamespace(reserve_listing=AsyncMock(), like_listing=AsyncMock())
        view = BrokerageListingView(
            cog,
            {
                "_id": "listing-42",
                "seller_id": 10,
                "title": "상품",
                "description": "설명",
                "price": 1_000,
                "status": "open",
            },
        )
        interaction = SimpleNamespace(response=SimpleNamespace(send_modal=AsyncMock()))

        await button_with_id(view, "devilblox:broker:buy:listing-42").callback(interaction)
        await button_with_id(view, "devilblox:broker:like:listing-42").callback(interaction)
        await button_with_id(view, "devilblox:broker:report:listing-42").callback(interaction)

        cog.reserve_listing.assert_awaited_once_with(interaction, "listing-42")
        cog.like_listing.assert_awaited_once_with(interaction, "listing-42")
        report_modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(report_modal, BrokerageReportModal)
        self.assertEqual(report_modal.listing_id, "listing-42")

    async def test_ticket_and_review_buttons_delegate_with_object_ids(self) -> None:
        cog = SimpleNamespace(complete_sale=AsyncMock(), cancel_and_promote=AsyncMock())
        ticket = BrokerageTicketView(cog, "listing-42", 987)
        review = BrokerageReviewRequestView(
            cog,
            {"_id": "review-24", "seller_id": 10},
        )
        interaction = SimpleNamespace(response=SimpleNamespace(send_modal=AsyncMock()))

        await button_with_id(
            ticket, "devilblox:broker:ticket:complete:listing-42:987"
        ).callback(interaction)
        await button_with_id(
            ticket, "devilblox:broker:ticket:next:listing-42:987"
        ).callback(interaction)
        await button_with_id(
            ticket, "devilblox:broker:ticket:report:listing-42:987"
        ).callback(interaction)
        ticket_report = interaction.response.send_modal.await_args.args[0]
        await button_with_id(
            review, "devilblox:broker:review:review-24"
        ).callback(interaction)

        cog.complete_sale.assert_awaited_once_with(interaction, "listing-42", 987)
        cog.cancel_and_promote.assert_awaited_once_with(interaction, "listing-42", 987)
        self.assertIsInstance(ticket_report, BrokerageReportModal)
        self.assertEqual(ticket_report.listing_id, "listing-42")
        review_modal = interaction.response.send_modal.await_args.args[0]
        self.assertIsInstance(review_modal, BrokerageReviewModal)
        self.assertEqual(review_modal.review_id, "review-24")

    async def test_admin_panel_checks_permission_and_delegates_actions(self) -> None:
        brokerage_repo = SimpleNamespace(get_config=AsyncMock(return_value={}))
        cog = SimpleNamespace(
            is_admin=AsyncMock(return_value=True),
            send_admin_overview=AsyncMock(),
            repos=SimpleNamespace(brokerage=brokerage_repo),
        )
        view = BrokerageAdminPanelView(cog)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=77),
            response=SimpleNamespace(send_modal=AsyncMock(), send_message=AsyncMock()),
        )

        self.assertTrue(await view.interaction_check(interaction))
        await button_with_id(view, "devilblox:broker:admin:delete").callback(interaction)
        delete_modal = interaction.response.send_modal.await_args.args[0]
        await button_with_id(view, "devilblox:broker:admin:adjust").callback(interaction)
        adjust_modal = interaction.response.send_modal.await_args.args[0]
        await button_with_id(view, "devilblox:broker:admin:configure").callback(interaction)
        config_modal = interaction.response.send_modal.await_args.args[0]
        await button_with_id(view, "devilblox:broker:admin:overview").callback(interaction)

        cog.is_admin.assert_awaited_once_with(interaction)
        self.assertIsInstance(delete_modal, BrokerageDeleteModal)
        self.assertIsInstance(adjust_modal, BrokerageAdminAdjustmentModal)
        brokerage_repo.get_config.assert_awaited_once_with(77)
        self.assertIsInstance(config_modal, BrokerageConfigModal)
        cog.send_admin_overview.assert_awaited_once_with(interaction)

    async def test_admin_panel_rejects_non_admin_interactions(self) -> None:
        cog = SimpleNamespace(is_admin=AsyncMock(return_value=False))
        view = BrokerageAdminPanelView(cog)
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock())
        )

        self.assertFalse(await view.interaction_check(interaction))

        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertEqual(kwargs["embed"].title, "권한 없음")
        self.assertTrue(kwargs["ephemeral"])

class BrokerageGuildIsolationTests(unittest.IsolatedAsyncioTestCase):
    def build(self, operation_name: str) -> tuple[BrokerageCog, SimpleNamespace, AsyncMock]:
        blocked_operation = AsyncMock()
        store = SimpleNamespace(
            get_listing=AsyncMock(
                return_value={
                    "_id": "foreign-listing",
                    "guild_id": 88,
                    "seller_id": 456,
                    "status": "open",
                }
            ),
            **{operation_name: blocked_operation},
        )
        cog = object.__new__(BrokerageCog)
        cog.bot = SimpleNamespace(repos=SimpleNamespace(brokerage=store))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=77),
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        return cog, interaction, blocked_operation

    async def test_foreign_guild_listing_cannot_be_liked(self) -> None:
        cog, interaction, add_like = self.build("add_like")

        await cog.like_listing(interaction, "foreign-listing")

        add_like.assert_not_awaited()

    async def test_foreign_guild_listing_cannot_be_reported(self) -> None:
        cog, interaction, add_report = self.build("add_report")

        await cog.report_listing(interaction, "foreign-listing", "신고")

        add_report.assert_not_awaited()

    async def test_foreign_guild_listing_cannot_be_reserved(self) -> None:
        cog, interaction, enqueue_buyer = self.build("enqueue_buyer")

        await cog.reserve_listing(interaction, "foreign-listing")

        enqueue_buyer.assert_not_awaited()

class BrokerageComponentsV2SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_post_does_not_mix_layout_view_with_message_content(self) -> None:
        listing = {
            "_id": "listing-v2",
            "guild_id": 77,
            "seller_id": 123,
            "title": "V2 상품",
            "description": "설명",
            "price": 1_000,
            "status": "open",
            "like_count": 0,
        }
        bound = {**listing, "channel_id": 55, "message_id": 66}
        channel = SimpleNamespace(id=55, send=AsyncMock(return_value=SimpleNamespace(id=66)))
        store = SimpleNamespace(
            get_config=AsyncMock(return_value={}),
            ensure_profile=AsyncMock(return_value={"trust_score": 0}),
            bind_listing_message=AsyncMock(return_value=bound),
            mark_notified_users=AsyncMock(return_value={"listing": bound}),
        )
        settings = SimpleNamespace(
            get=AsyncMock(return_value={"channels": {"brokerage": 55}})
        )
        cog = object.__new__(BrokerageCog)
        cog.bot = SimpleNamespace(
            repos=SimpleNamespace(brokerage=store, settings=settings)
        )
        cog.notification_members = AsyncMock(
            return_value=([member(999, "subscriber")], [999])
        )
        guild = SimpleNamespace(id=77, get_channel=lambda channel_id: channel)

        await cog.post_listing(guild, listing)

        kwargs = channel.send.await_args.kwargs
        self.assertNotIn("content", kwargs)
        self.assertIn("<@999>", layout_text(kwargs["view"]))
        for uploaded in kwargs.get("files", []):
            uploaded.close()

    async def test_failed_ticket_send_removes_the_created_channel(self) -> None:
        seller = Mock(id=10, mention="<@10>", display_name="seller")
        buyer = Mock(id=20, mention="<@20>", display_name="buyer")
        channel = SimpleNamespace(
            id=55,
            send=AsyncMock(side_effect=RuntimeError("send failed")),
            delete=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=77,
            default_role=Mock(),
            get_member=lambda user_id: seller if user_id == 10 else buyer,
            get_channel=lambda channel_id: None,
            get_role=lambda role_id: None,
            create_text_channel=AsyncMock(return_value=channel),
        )
        settings = SimpleNamespace(
            get=AsyncMock(return_value={"categories": {}, "roles": {}})
        )
        cog = object.__new__(BrokerageCog)
        cog.bot = SimpleNamespace(
            repos=SimpleNamespace(settings=settings, brokerage=SimpleNamespace())
        )

        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await cog.open_trade_ticket(
                guild,
                {
                    "_id": "listing-ticket-failure",
                    "guild_id": 77,
                    "seller_id": 10,
                    "title": "상품",
                    "current_reservation_number": 1,
                },
                20,
            )

        channel.delete.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertNotIn("content", kwargs)
        close_uploaded_files(kwargs)

class BrokerageVerificationFlowTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self) -> SimpleNamespace:
        return SimpleNamespace(
            guild=SimpleNamespace(id=77),
            user=SimpleNamespace(id=123),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_challenge_is_reserved_before_email_delivery(self) -> None:
        events: list[str] = []
        store = SimpleNamespace(
            ensure_profile=AsyncMock(return_value={"verified_kinds": []}),
            create_verification_challenge=AsyncMock(
                side_effect=lambda *args, **kwargs: events.append("reserved")
                or {"_id": "challenge-1"}
            ),
        )
        cog = object.__new__(BrokerageCog)
        cog.bot = SimpleNamespace(repos=SimpleNamespace(brokerage=store))
        interaction = self.interaction()

        async def deliver(*args, **kwargs):
            events.append("delivered")
            return "t***@e***.com"

        with (
            patch.dict("os.environ", {"BROKERAGE_VERIFICATION_PEPPER": "test-pepper"}),
            patch("cogs.brokerage.verification.send_email_code", side_effect=deliver),
        ):
            await cog.start_verification(interaction, "email", "test@example.com")

        self.assertEqual(events, ["reserved", "delivered"])
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertIsInstance(kwargs["view"], BrokerageVerificationCodeView)
        self.assertEqual(kwargs["view"].challenge_id, "challenge-1")
        close_uploaded_files(kwargs)

    async def test_delivery_failure_cancels_reserved_challenge(self) -> None:
        store = SimpleNamespace(
            ensure_profile=AsyncMock(return_value={"verified_kinds": []}),
            create_verification_challenge=AsyncMock(return_value={"_id": "challenge-2"}),
            cancel_verification_challenge=AsyncMock(),
        )
        cog = object.__new__(BrokerageCog)
        cog.bot = SimpleNamespace(repos=SimpleNamespace(brokerage=store))
        interaction = self.interaction()

        from services.brokerage_verification import VerificationDeliveryError

        with (
            patch.dict("os.environ", {"BROKERAGE_VERIFICATION_PEPPER": "test-pepper"}),
            patch(
                "cogs.brokerage.verification.send_email_code",
                side_effect=VerificationDeliveryError("sanitized"),
            ),
        ):
            await cog.start_verification(interaction, "email", "test@example.com")

        store.cancel_verification_challenge.assert_awaited_once_with(
            "challenge-2", 77, 123, reason="delivery_failed"
        )
        self.assertEqual(
            interaction.followup.send.await_args.kwargs["embed"].title,
            "인증 코드 발송 실패",
        )

    async def test_cooldown_rejects_before_provider_delivery(self) -> None:
        store = SimpleNamespace(
            ensure_profile=AsyncMock(return_value={"verified_kinds": []}),
            create_verification_challenge=AsyncMock(
                side_effect=ValueError("verification challenge cooldown is active")
            ),
        )
        cog = object.__new__(BrokerageCog)
        cog.bot = SimpleNamespace(repos=SimpleNamespace(brokerage=store))
        interaction = self.interaction()

        with (
            patch.dict("os.environ", {"BROKERAGE_VERIFICATION_PEPPER": "test-pepper"}),
            patch("cogs.brokerage.verification.send_email_code", new_callable=AsyncMock) as sender,
        ):
            await cog.start_verification(interaction, "email", "test@example.com")

        sender.assert_not_awaited()
        self.assertEqual(
            interaction.followup.send.await_args.kwargs["embed"].title,
            "인증 요청 제한",
        )


if __name__ == "__main__":
    unittest.main()
