from .coupons import CouponStore
from .settings import GuildSettingsStore
from .tickets import MiddlemanStore, SellerStore, TicketStore
from .users import UserStore


class Repositories:
    def __init__(self, db):
        self.settings = GuildSettingsStore(db)
        self.users = UserStore(db)
        self.tickets = TicketStore(db)
        self.sellers = SellerStore(db)
        self.middlemen = MiddlemanStore(db)
        self.coupons = CouponStore(db)

    async def ensure_indexes(self):
        stores = (
            self.settings,
            self.users,
            self.tickets,
            self.sellers,
            self.middlemen,
            self.coupons,
        )
        for store in stores:
            await store.ensure_indexes()
