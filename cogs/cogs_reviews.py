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


def safe_photo_filename(attachment: discord.Attachment) -> str:
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", attachment.filename or "review-photo.png")
    if "." not in filename:
        filename += ".png"
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
        self.photo_upload = discord.ui.FileUpload(required=False, min_values=0, max_values=1)
        self.add_item(self.rating)
        self.add_item(self.content)
        self.add_item(
            discord.ui.Label(
                text="사진 리뷰",
                description="선택 사항입니다. 이미지 파일 1장을 업로드할 수 있습니다.",
                component=self.photo_upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.submit_review(
            interaction,
            self.review_id,
            rating_text=str(self.rating.value),
            content=str(self.content.value),
            photo=self.photo_upload.values[0] if self.photo_upload.values else None,
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
        photo: discord.Attachment | None = None,
    ):
        await self.ensure_review_store()
        private_response = interaction.guild is not None
        existing = await self.repos.reviews.get(review_id)
        if existing is None:
            await interaction.response.send_message(
                embed=error_embed("후기 없음", "후기 요청을 찾을 수 없습니다."),
                ephemeral=private_response,
            )
            return
        if existing.get("buyer_id") != interaction.user.id:
            await interaction.response.send_message(
                embed=error_embed("작성 불가", "이 후기는 구매자만 작성할 수 있습니다."),
                ephemeral=private_response,
            )
            return
        if existing.get("status") != "pending":
            await interaction.response.send_message(
                embed=error_embed("작성 완료", "이미 작성된 후기입니다."),
                ephemeral=private_response,
            )
            return

        try:
            rating = int(rating_text.strip())
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed("별점 오류", "별점은 1~5 사이 숫자로 입력해주세요."),
                ephemeral=private_response,
            )
            return
        if rating < 1 or rating > 5:
            await interaction.response.send_message(
                embed=error_embed("별점 오류", "별점은 1~5 사이 숫자로 입력해주세요."),
                ephemeral=private_response,
            )
            return

        content = content.strip()
        if len(content) < 2:
            await interaction.response.send_message(
                embed=error_embed("후기 오류", "후기 내용은 2자 이상 입력해주세요."),
                ephemeral=private_response,
            )
            return

        photo_filename = ""
        photo_bytes = None
        if photo is not None:
            if not is_image_attachment(photo):
                await interaction.response.send_message(
                    embed=error_embed("파일 오류", "사진 리뷰는 이미지 파일만 업로드할 수 있습니다."),
                    ephemeral=private_response,
                )
                return
            if photo.size > MAX_REVIEW_PHOTO_BYTES:
                await interaction.response.send_message(
                    embed=error_embed("파일 용량 오류", "사진 리뷰는 8MB 이하로 업로드해주세요."),
                    ephemeral=private_response,
                )
                return
            photo_filename = safe_photo_filename(photo)
            try:
                photo_bytes = await photo.read()
            except discord.HTTPException:
                await interaction.response.send_message(
                    embed=error_embed("파일 처리 실패", "사진 리뷰 파일을 읽지 못했습니다. 다시 시도해주세요."),
                    ephemeral=private_response,
                )
                return

        doc = await self.repos.reviews.submit(
            review_id,
            interaction.user.id,
            rating=rating,
            content=content,
            photo_filename=photo_filename,
            photo_content_type=(photo.content_type or "") if photo else "",
            photo_size=photo.size if photo else 0,
        )
        if doc is None:
            await interaction.response.send_message(
                embed=error_embed("처리 실패", "후기를 저장하지 못했습니다. 다시 시도해주세요."),
                ephemeral=private_response,
            )
            return

        photo_file = discord.File(io.BytesIO(photo_bytes), filename=photo_filename) if photo_bytes is not None else None
        await self.send_review_log(doc, photo_file=photo_file)
        await interaction.response.send_message(
            embed=success_embed("후기 등록 완료", "소중한 후기 감사합니다."),
            ephemeral=private_response,
        )

    async def send_review_log(self, review: dict, *, photo_file: discord.File | None = None):
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
            if photo_file is not None:
                embed.set_image(url=attachment_image_url(photo_file.filename))
                await channel.send(embed=embed, files=branded_files(photo_file))
                return
            await channel.send(**random_embed_gif_kwargs(embed, SUCCESS_GIFS))
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
