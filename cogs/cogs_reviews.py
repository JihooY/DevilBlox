from __future__ import annotations

import io
import re

import discord
from discord import app_commands
from discord.ext import commands

from database.reviews import ReviewStore
from utils.embeds import branded_files, error_embed, info_embed, success_embed
from utils.gifs import SUCCESS_GIFS, random_embed_gif_kwargs

MAX_REVIEW_PHOTO_BYTES = 8 * 1024 * 1024
MAX_REVIEW_PHOTOS = 5
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _timestamp(value) -> int | None:
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    return None


def discord_time(value, style: str = "F") -> str:
    timestamp = _timestamp(value)
    if timestamp is None:
        return "-"
    return f"<t:{timestamp}:{style}>"


def star_text(rating: int) -> str:
    rating = max(1, min(5, int(rating)))
    return "★" * rating + "☆" * (5 - rating)


def truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def safe_photo_filename(attachment: discord.Attachment, index: int) -> str:
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", attachment.filename or "review-photo.png")
    if "." not in filename:
        filename += ".png"
    if "." in filename:
        stem, extension = filename.rsplit(".", 1)
        filename = f"review_{index}_{stem[:60]}.{extension}"
    else:
        filename = f"review_{index}_{filename[:60]}"
    return filename[:80]


def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").casefold()
    if content_type.startswith("image/"):
        return True
    filename = (attachment.filename or "").casefold()
    return any(filename.endswith(extension) for extension in IMAGE_EXTENSIONS)


def attachment_image_url(filename: str) -> str:
    return f"attachment://{filename}"


class ReviewModal(discord.ui.Modal):
    def __init__(self, cog: "ReviewsCog", review_id: str):
        super().__init__(title="구매 후기 작성")
        self.cog = cog
        self.review_id = review_id
        self.rating = discord.ui.TextInput(
            label="별점 (1~5)",
            placeholder="예: 5",
            min_length=1,
            max_length=1,
        )
        self.content = discord.ui.TextInput(
            label="후기 내용",
            style=discord.TextStyle.paragraph,
            placeholder="구매 경험을 솔직하게 적어주세요.",
            min_length=2,
            max_length=1000,
        )
        self.photo_upload = discord.ui.FileUpload(required=False, min_values=0, max_values=MAX_REVIEW_PHOTOS)
        self.add_item(self.rating)
        self.add_item(self.content)
        self.add_item(
            discord.ui.Label(
                text="사진 리뷰",
                description=f"선택 사항입니다. 이미지 파일을 최대 {MAX_REVIEW_PHOTOS}장까지 업로드할 수 있습니다.",
                component=self.photo_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.submit_review(
            interaction,
            self.review_id,
            rating_text=str(self.rating.value),
            content=str(self.content.value),
            photos=list(self.photo_upload.values or []),
        )


class ReviewRequestView(discord.ui.View):
    def __init__(self, cog: "ReviewsCog", review_id: str):
        super().__init__(timeout=60 * 60 * 24 * 7)
        self.cog = cog
        self.review_id = review_id

    @discord.ui.button(label="후기 작성", style=discord.ButtonStyle.primary)
    async def write_review(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ReviewModal(self.cog, self.review_id))


class ReviewsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await self.ensure_review_store()

    @property
    def repos(self):
        return self.bot.repos

    async def ensure_review_store(self):
        repos = self.repos
        db = getattr(self.bot, "db", None)
        if repos is None or db is None or hasattr(repos, "reviews"):
            return
        repos.reviews = ReviewStore(db)
        await repos.reviews.ensure_indexes()

    async def fetch_user(self, user_id: int) -> discord.User | discord.Member | None:
        user = self.bot.get_user(user_id)
        if user is not None:
            return user
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    async def review_log_channel(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None
        settings = await self.repos.settings.get(guild_id)
        channel_id = settings["channels"].get("review_log") or settings["channels"].get("purchase_log")
        return guild.get_channel(channel_id or 0)

    async def request_review(
        self,
        *,
        guild: discord.Guild,
        buyer_id: int,
        seller_id: int | None,
        product_title: str,
        product_id: str = "",
        category_id: str = "",
        category_name: str = "",
        source: str,
        purchased_at=None,
        amount: int | None = None,
    ) -> bool:
        await self.ensure_review_store()
        doc = await self.repos.reviews.create_pending(
            guild_id=guild.id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            product_id=product_id,
            product_title=product_title,
            category_id=category_id,
            category_name=category_name,
            source=source,
            purchased_at=purchased_at,
            amount=amount,
        )

        user = guild.get_member(buyer_id) or await self.fetch_user(buyer_id)
        if user is None:
            return False

        embed = info_embed(
            "구매 후기 작성",
            "구매 감사합니다. 아래 버튼을 눌러 별점과 후기를 남겨주세요.",
        )
        embed.add_field(name="구매 상품", value=product_title or "-", inline=False)
        embed.add_field(name="구매 셀러", value=f"<@{seller_id}>" if seller_id else "-", inline=True)
        if category_id:
            category_label = category_name or category_id
            embed.add_field(name="카테고리", value=f"{category_label} (`{category_id}`)", inline=True)
        embed.add_field(name="구매 시간", value=discord_time(purchased_at), inline=False)
        embed.set_footer(text=f"후기 ID: {doc['_id']}")

        try:
            await user.send(**random_embed_gif_kwargs(embed, SUCCESS_GIFS), view=ReviewRequestView(self, doc["_id"]))
        except discord.HTTPException:
            return False
        return True

    async def submit_review(
        self,
        interaction: discord.Interaction,
        review_id: str,
        *,
        rating_text: str,
        content: str,
        photos: list[discord.Attachment] | None = None,
    ):
        private_response = interaction.guild is not None
        await interaction.response.defer(ephemeral=private_response)
        await self.ensure_review_store()
        existing = await self.repos.reviews.get(review_id)
        if existing is None:
            await interaction.followup.send(
                embed=error_embed("후기 없음", "후기 요청을 찾을 수 없습니다."),
                ephemeral=private_response,
            )
            return
        if existing.get("buyer_id") != interaction.user.id:
            await interaction.followup.send(
                embed=error_embed("작성 불가", "이 후기는 구매자만 작성할 수 있습니다."),
                ephemeral=private_response,
            )
            return
        if existing.get("status") != "pending":
            await interaction.followup.send(
                embed=error_embed("작성 완료", "이미 작성된 후기입니다."),
                ephemeral=private_response,
            )
            return

        try:
            rating = int(rating_text.strip())
        except ValueError:
            await interaction.followup.send(
                embed=error_embed("별점 오류", "별점은 1~5 사이 숫자로 입력해주세요."),
                ephemeral=private_response,
            )
            return
        if rating < 1 or rating > 5:
            await interaction.followup.send(
                embed=error_embed("별점 오류", "별점은 1~5 사이 숫자로 입력해주세요."),
                ephemeral=private_response,
            )
            return

        content = content.strip()
        if len(content) < 2:
            await interaction.followup.send(
                embed=error_embed("후기 오류", "후기 내용은 2자 이상 입력해주세요."),
                ephemeral=private_response,
            )
            return

        photos = list(photos or [])
        if len(photos) > MAX_REVIEW_PHOTOS:
            await interaction.followup.send(
                embed=error_embed("사진 개수 오류", f"사진 리뷰는 최대 {MAX_REVIEW_PHOTOS}장까지 업로드할 수 있습니다."),
                ephemeral=private_response,
            )
            return

        photo_payloads: list[tuple[str, bytes]] = []
        photo_docs: list[dict] = []
        for index, photo in enumerate(photos, start=1):
            if not is_image_attachment(photo):
                await interaction.followup.send(
                    embed=error_embed("파일 오류", "사진 리뷰는 이미지 파일만 업로드할 수 있습니다."),
                    ephemeral=private_response,
                )
                return
            if photo.size > MAX_REVIEW_PHOTO_BYTES:
                await interaction.followup.send(
                    embed=error_embed("파일 용량 오류", "사진 리뷰는 8MB 이하로 업로드해주세요."),
                    ephemeral=private_response,
                )
                return
            photo_filename = safe_photo_filename(photo, index)
            try:
                photo_bytes = await photo.read()
            except discord.HTTPException:
                await interaction.followup.send(
                    embed=error_embed("파일 처리 실패", "사진 리뷰 파일을 읽지 못했습니다. 다시 시도해주세요."),
                    ephemeral=private_response,
                )
                return
            photo_payloads.append((photo_filename, photo_bytes))
            photo_docs.append(
                {
                    "filename": photo_filename,
                    "content_type": photo.content_type or "",
                    "size": int(photo.size or 0),
                }
            )

        doc = await self.repos.reviews.submit(
            review_id,
            interaction.user.id,
            rating=rating,
            content=content,
            photos=photo_docs,
        )
        if doc is None:
            await interaction.followup.send(
                embed=error_embed("처리 실패", "후기를 저장하지 못했습니다. 다시 시도해주세요."),
                ephemeral=private_response,
            )
            return

        photo_files = [discord.File(io.BytesIO(payload), filename=filename) for filename, payload in photo_payloads]
        await self.send_review_log(doc, photo_files=photo_files)
        await interaction.followup.send(
            embed=success_embed("후기 등록 완료", "소중한 후기 감사합니다."),
            ephemeral=private_response,
        )

    async def send_review_log(self, review: dict, *, photo_files: list[discord.File] | None = None):
        channel = await self.review_log_channel(review["guild_id"])
        if channel is None:
            return

        rating = int(review.get("rating", 0) or 0)
        embed = success_embed("PURCHASE REVIEW")
        embed.add_field(name="별점", value=f"{star_text(rating)} ({rating}/5)", inline=True)
        embed.add_field(name="구매 상품", value=review.get("product_title") or "-", inline=False)
        embed.add_field(name="후기 내용", value=truncate(review.get("content") or "-", 1024), inline=False)
        embed.add_field(name="구매자", value=f"<@{review['buyer_id']}>", inline=True)
        embed.add_field(name="구매 셀러", value=f"<@{review['seller_id']}>" if review.get("seller_id") else "-", inline=True)
        if review.get("category_id"):
            category_label = review.get("category_name") or review["category_id"]
            embed.add_field(name="카테고리", value=f"{category_label} (`{review['category_id']}`)", inline=True)
        embed.add_field(name="구매 시간", value=discord_time(review.get("purchased_at")), inline=True)
        embed.add_field(name="후기 작성 시간", value=discord_time(review.get("reviewed_at")), inline=True)
        try:
            photo_files = list(photo_files or [])
            if photo_files:
                embed.set_image(url=attachment_image_url(photo_files[0].filename))
                await channel.send(embed=embed, files=branded_files(*photo_files))
                return
            await channel.send(embed=embed)
        except discord.HTTPException:
            return

    def build_reviews_embed(self, title: str, reviews: list[dict], *, category: dict | None = None) -> discord.Embed:
        if not reviews:
            return info_embed(title, "등록된 후기가 없습니다.")

        average = sum(int(review.get("rating", 0) or 0) for review in reviews) / len(reviews)
        if category:
            description = f"{category.get('emoji') or ''} {category['name']} (`{category['category_id']}`)\n최근 {len(reviews)}개 · 평균 {average:.1f}/5"
        else:
            description = f"최근 {len(reviews)}개 · 평균 {average:.1f}/5"
        embed = info_embed(title, description)

        for review in reviews[:10]:
            rating = int(review.get("rating", 0) or 0)
            seller = f"<@{review['seller_id']}>" if review.get("seller_id") else "-"
            value = (
                f"{truncate(review.get('content') or '-', 420)}\n"
                f"구매자: <@{review['buyer_id']}> · 셀러: {seller}\n"
                f"구매 시간: {discord_time(review.get('purchased_at'), 'd')}"
            )
            product = truncate(review.get("product_title") or "상품", 80)
            embed.add_field(name=f"{star_text(rating)} · {product}", value=value, inline=False)
        return embed

    def build_seller_rating_embed(
        self,
        *,
        seller_id: int,
        rating_doc: dict | None,
        recent_reviews: list[dict],
    ) -> discord.Embed:
        seller_label = f"<@{seller_id}>"
        if rating_doc is None or int(rating_doc.get("rating_count", 0) or 0) <= 0:
            return info_embed("셀러 평점", f"{seller_label} 셀러의 등록된 평점이 없습니다.")

        average = float(rating_doc.get("rating_average", 0) or 0)
        rating_count = int(rating_doc.get("rating_count", 0) or 0)
        embed = info_embed("셀러 평점", f"{seller_label} 셀러의 구매 후기 평점입니다.")
        embed.add_field(name="평균 별점", value=f"{star_text(round(average))} `{average:.2f}/5`", inline=True)
        embed.add_field(name="후기 수", value=f"`{rating_count}`개", inline=True)
        if rating_doc.get("last_reviewed_at"):
            embed.add_field(name="최근 후기", value=discord_time(rating_doc.get("last_reviewed_at")), inline=False)

        if recent_reviews:
            lines = []
            for review in recent_reviews[:5]:
                rating = int(review.get("rating", 0) or 0)
                product = truncate(review.get("product_title") or "상품", 40)
                content = truncate(review.get("content") or "-", 80)
                lines.append(f"{star_text(rating)} `{product}` · {content}")
            embed.add_field(name="최근 후기 내용", value="\n".join(lines)[:1024], inline=False)
        return embed

    def build_seller_rating_list_embed(self, stats: list[dict]) -> discord.Embed:
        if not stats:
            return info_embed("셀러 평점", "아직 등록된 셀러 평점이 없습니다.")

        embed = info_embed("셀러 평점 순위", "평균 별점과 후기 수를 기준으로 표시합니다.")
        lines = []
        for index, stat in enumerate(stats[:10], start=1):
            seller_id = int(stat.get("seller_id", 0) or 0)
            average = float(stat.get("rating_average", 0) or 0)
            rating_count = int(stat.get("rating_count", 0) or 0)
            lines.append(
                f"`{index}.` <@{seller_id}> · {star_text(round(average))} `{average:.2f}/5` · 후기 `{rating_count}`개"
            )
        embed.description = "\n".join(lines)
        return embed

    @app_commands.command(name="후기채널설정", description="구매 후기가 올라갈 채널을 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    async def set_review_channel(self, interaction: discord.Interaction, 채널: discord.TextChannel):
        await self.repos.settings.set_value(interaction.guild.id, "channels", "review_log", 채널.id)
        await interaction.response.send_message(
            embed=success_embed("후기 채널 설정 완료", 채널.mention),
            ephemeral=True,
        )

    @app_commands.command(name="후기검색", description="상품 카테고리별 구매 후기를 조회합니다.")
    @app_commands.describe(category_id="조회할 상품 카테고리 ID. 비우면 최근 후기를 표시합니다.")
    async def search_reviews(self, interaction: discord.Interaction, category_id: str = ""):
        await interaction.response.defer()
        await self.ensure_review_store()
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("서버 전용", "후기 검색은 서버 안에서만 사용할 수 있습니다."))
            return

        category = None
        category_id = category_id.strip()
        if category_id:
            category = await self.repos.product_categories.get(interaction.guild.id, category_id)
            if category is None:
                await interaction.followup.send(embed=error_embed("카테고리 없음", "해당 카테고리 ID를 찾을 수 없습니다."))
                return
            reviews = await self.repos.reviews.list_by_category(interaction.guild.id, category["category_id"], limit=10)
            title = "카테고리 후기 검색"
        else:
            reviews = await self.repos.reviews.list_recent(interaction.guild.id, limit=10)
            title = "최근 구매 후기"

        await interaction.followup.send(embed=self.build_reviews_embed(title, reviews, category=category))

    @app_commands.command(name="셀러평점", description="셀러별 평균 구매 후기 평점을 확인합니다.")
    @app_commands.describe(셀러="조회할 셀러. 비우면 평점 순위를 표시합니다.")
    async def seller_rating(self, interaction: discord.Interaction, 셀러: discord.Member | None = None):
        await interaction.response.defer()
        await self.ensure_review_store()
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("서버 전용", "셀러 평점은 서버 안에서만 사용할 수 있습니다."))
            return

        if 셀러 is not None:
            rating_doc = await self.repos.reviews.get_seller_rating(interaction.guild.id, 셀러.id)
            recent_reviews = await self.repos.reviews.list_by_seller(interaction.guild.id, 셀러.id, limit=5)
            await interaction.followup.send(
                embed=self.build_seller_rating_embed(
                    seller_id=셀러.id,
                    rating_doc=rating_doc,
                    recent_reviews=recent_reviews,
                )
            )
            return

        stats = await self.repos.reviews.list_seller_ratings(interaction.guild.id, limit=10)
        if not stats:
            stats = await self.repos.reviews.rebuild_all_seller_ratings(interaction.guild.id, limit=10)
        await interaction.followup.send(embed=self.build_seller_rating_list_embed(stats))

    async def autocomplete_categories(self, interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        current_lower = current.casefold()
        categories = await self.repos.product_categories.list_active(interaction.guild.id)
        choices = []
        for category in categories:
            label = f"{category.get('emoji') or ''} {category['name']} ({category['category_id']})".strip()
            haystack = f"{category['category_id']} {category['name']}".casefold()
            if current_lower and current_lower not in haystack:
                continue
            choices.append(app_commands.Choice(name=label[:100], value=category["category_id"]))
        return choices[:25]

    @search_reviews.autocomplete("category_id")
    async def search_category_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.autocomplete_categories(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReviewsCog(bot))
