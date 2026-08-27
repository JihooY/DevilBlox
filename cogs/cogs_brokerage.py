
from __future__ import annotations

from discord.ext import commands

from .brokerage.cog import BrokerageCog
from .brokerage.common import (
    _listing_interval,
    _listing_markdown,
    _profile_markdown,
)
from .brokerage.components import (
    BrokerageAdminPanelView,
    BrokerageListingView,
    BrokerageMessageView,
    BrokerageNotificationView,
    BrokeragePanelView,
    BrokerageReviewRequestView,
    BrokerageTicketView,
    BrokerageVerificationCodeView,
    BrokerageVerificationView,
)
from .brokerage.modals import (
    BrokerageAdminAdjustmentModal,
    BrokerageConfigModal,
    BrokerageDeleteModal,
    BrokerageListingModal,
    BrokerageNotificationModal,
    BrokerageReportModal,
    BrokerageReviewModal,
)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BrokerageCog(bot))


__all__ = [
    "BrokerageAdminAdjustmentModal",
    "BrokerageAdminPanelView",
    "BrokerageCog",
    "BrokerageConfigModal",
    "BrokerageDeleteModal",
    "BrokerageListingModal",
    "BrokerageListingView",
    "BrokerageMessageView",
    "BrokerageNotificationModal",
    "BrokerageNotificationView",
    "BrokeragePanelView",
    "BrokerageReportModal",
    "BrokerageReviewModal",
    "BrokerageReviewRequestView",
    "BrokerageTicketView",
    "BrokerageVerificationCodeView",
    "BrokerageVerificationView",
    "_listing_interval",
    "_listing_markdown",
    "_profile_markdown",
    "setup",
]
