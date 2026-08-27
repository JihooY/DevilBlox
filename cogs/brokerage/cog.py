
from discord.ext import commands

from .admin import BrokerageAdminMixin
from .commands import BrokerageCommandMixin
from .listings import BrokerageListingMixin
from .runtime import BrokerageRuntimeMixin
from .trades import BrokerageTradeMixin
from .verification import BrokerageVerificationMixin
from .workers import BrokerageWorkerMixin


class BrokerageCog(
    BrokerageRuntimeMixin,
    BrokerageVerificationMixin,
    BrokerageListingMixin,
    BrokerageTradeMixin,
    BrokerageAdminMixin,
    BrokerageWorkerMixin,
    BrokerageCommandMixin,
    commands.Cog,
):
    '''Discord marketplace brokerage system.'''
