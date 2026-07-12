from .coupons import CouponStore
from .lottery import LotteryStore
from .reviews import ReviewStore
from .settings import GuildSettingsStore
from .stock import StockStore
from .tickets import MiddlemanStore, SellerStore, TicketStore
from .users import UserStore
from .vending import ArchiveStore, ProductCategoryStore, ProductStore, VendingLogStore
from .warnings import WarningStore


class Repositories:
    def __init__(self, db):
        self.settings = GuildSettingsStore(db)
        self.users = UserStore(db)
        self.tickets = TicketStore(db)
        self.sellers = SellerStore(db)
        self.middlemen = MiddlemanStore(db)
        self.coupons = CouponStore(db)
        self.lottery = LotteryStore(db)
        self.product_categories = ProductCategoryStore(db)
        self.products = ProductStore(db)
        self.archives = ArchiveStore(db)
        self.vending = VendingLogStore(db)
        self.stock = StockStore(db)
        self.reviews = ReviewStore(db)
        self.warnings = WarningStore(db)

    async def ensure_indexes(self):
        stores = (
            self.settings,
            self.users,
            self.tickets,
            self.sellers,
            self.middlemen,
            self.coupons,
            self.lottery,
            self.product_categories,
            self.products,
            self.archives,
            self.vending,
            self.stock,
            self.reviews,
            self.warnings,
        )
        for store in stores:
            await store.ensure_indexes()
