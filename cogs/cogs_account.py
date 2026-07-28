from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import (
    BRAND_LOGO_FILENAME,
    BRAND_LOGO_URL,
    COLOR_INFO,
    branded_files,
    error_embed,
    info_embed,
    success_embed,
)
from utils.gifs import (
    PANEL_GIFS,
    choose_gif,
    gif_delivery_status,
    gif_file,
    gif_media_url,
    message_media_urls,
    retained_non_gif_attachments,
)
from utils.panels import save_panel_location
from utils.roles import has_role


def _add_brand_section(container: discord.ui.Container, content: str):
    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(content),
            accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
        )
    )


def _add_panel_gif(container: discord.ui.Container, gif_name: str | None, description: str):
    media_url = gif_media_url(gif_name)
    if media_url:
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media_url, description=description)
            )
        )


def _panel_send_kwargs(view: discord.ui.LayoutView, gif_name: str | None) -> dict:
    kwargs = {"view": view}
    files = branded_files(gif_file(gif_name))
    if files:
        kwargs["files"] = files
    return kwargs


def _panel_edit_kwargs(
    message: discord.Message,
    view: discord.ui.LayoutView,
    gif_name: str | None,
) -> dict:
    retained = list(retained_non_gif_attachments(message))
    if not any(item.filename == BRAND_LOGO_FILENAME for item in retained):
        retained = [*branded_files(), *retained]
    if gif_name and gif_delivery_status().effective_mode == "local":
        existing = next(
            (item for item in message.attachments if item.filename == gif_name),
            None,
        )
        retained.append(existing or gif_file(gif_name))
    return {
        "content": None,
        "embeds": [],
        "view": view,
        "attachments": [item for item in retained if item is not None],
    }


def _account_markdown(member: discord.Member, user: dict, grade_role: discord.Role | None) -> str:
    return "\n".join(
        (
            "## ACCOUNT INFORMATION",
            f"**유저**  {member.mention} (`{member.id}`)",
            f"**현재 등급**  {grade_role.mention if grade_role else '미설정'}",
            "",
            f"**누적 사용 금액**  `{int(user.get('accrued_spent', 0)):,}원`",
            f"**자판기 잔액**  `{int(user.get('cash', 0)):,}원`",
            f"**보유 포인트**  `{int(user.get('points', 0)):,}P`",
            "",
            "**중개 로그 익명 여부**  "
            + ("익명 사용중" if user.get("middleman_anonymous") else "익명 사용 안함"),
        )
    )


class ToggleAnonymousView(discord.ui.LayoutView):
    def __init__(self, cog: "AccountCog", content: str = "## ACCOUNT INFORMATION"):
        super().__init__(timeout=180)
        self.cog = cog
        container = discord.ui.Container(accent_color=COLOR_INFO)
        _add_brand_section(container, content)
        toggle = discord.ui.Button(
            label="중개 로그 익명 토글",
            style=discord.ButtonStyle.secondary,
        )
        toggle.callback = self.toggle
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(toggle))
        self.add_item(container)

    async def toggle(self, interaction: discord.Interaction):
        new_value = await self.cog.repos.users.toggle_middleman_anonymous(interaction.guild.id, interaction.user.id)
        state = "익명 사용" if new_value else "익명 사용 안함"
        await interaction.response.send_message(embed=success_embed("익명 설정 변경", state), ephemeral=True)


class AccountView(discord.ui.LayoutView):
    def __init__(self, cog: "AccountCog", gif_name: str | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        container = discord.ui.Container(accent_color=COLOR_INFO)
        _add_brand_section(
            container,
            "## ACCOUNT INFO\n계정 정보와 보유 쿠폰을 확인할 수 있습니다.",
        )
        _add_panel_gif(container, gif_name, "DevilBlox account panel")
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        info_button = discord.ui.Button(
            label="계정 정보",
            style=discord.ButtonStyle.success,
            custom_id="devilblox:account:info",
        )
        info_button.callback = self.info
        coupon_button = discord.ui.Button(
            label="보유 쿠폰",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:account:coupons",
        )
        coupon_button.callback = self.coupons
        container.add_item(discord.ui.ActionRow(info_button, coupon_button))
        self.add_item(container)

    async def info(self, interaction: discord.Interaction):
        view = await self.cog.build_account_view(interaction.guild, interaction.user)
        await interaction.response.send_message(
            **_panel_send_kwargs(view, None),
            ephemeral=True,
        )

    async def coupons(self, interaction: discord.Interaction):
        coupons = await self.cog.repos.coupons.list_for_user(interaction.guild.id, interaction.user.id)
        embed = info_embed("COUPON")
        if not coupons:
            embed.description = "현재 보유중인 쿠폰이 없습니다."
        else:
            for owned in coupons[:25]:
                coupon = owned.get("coupon") or {}
                embed.add_field(
                    name=coupon.get("name") or owned.get("code", "쿠폰"),
                    value=(
                        f"{coupon.get('description', '')}\n"
                        f"획득일: {owned.get('acquired_date', '알 수 없음')}\n"
                        f"만료일: {owned.get('deadline', '알 수 없음')}"
                    ),
                    inline=False,
                )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class AccountCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(AccountView(self))

    async def cog_load(self):
        self.restore_account_panel_loop.start()

    async def cog_unload(self):
        self.restore_account_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def _staff_allowed(self, interaction: discord.Interaction) -> bool:
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("seller")) or has_role(
            interaction.user, settings["roles"].get("admin")
        )

    async def _account_snapshot(self, guild: discord.Guild, member: discord.Member):
        settings = await self.repos.settings.get(guild.id)
        user = await self.repos.users.ensure_user(guild.id, member.id, settings["roles"].get("verified"))
        grade_role = guild.get_role(user.get("grade_role_id") or 0)
        return user, grade_role

    async def build_account_view(
        self,
        guild: discord.Guild,
        member: discord.Member,
    ) -> ToggleAnonymousView:
        user, grade_role = await self._account_snapshot(guild, member)
        return ToggleAnonymousView(self, _account_markdown(member, user, grade_role))

    async def build_account_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        user, grade_role = await self._account_snapshot(guild, member)
        embed = info_embed("ACCOUNT INFORMATION")
        embed.add_field(name="유저", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="현재 등급", value=grade_role.mention if grade_role else "미설정", inline=False)
        embed.add_field(name="누적 사용 금액", value=f"{user.get('accrued_spent', 0):,}원", inline=True)
        embed.add_field(name="자판기 잔액", value=f"{user.get('cash', 0):,}원", inline=True)
        embed.add_field(name="보유 포인트", value=f"{user.get('points', 0):,}P", inline=True)
        embed.add_field(
            name="중개 로그 익명 여부",
            value="익명 사용중" if user.get("middleman_anonymous") else "익명 사용 안함",
            inline=False,
        )
        return embed

    async def refresh_account_panel(self, guild: discord.Guild, *, rotate_image: bool = False):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("account")
        message_id = settings["meta"].get("account_panel_message_id")
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            gif_name = choose_gif(
                PANEL_GIFS,
                message.attachments,
                force_new=rotate_image,
                existing_urls=message_media_urls(message),
            )
            view = AccountView(self, gif_name)
            await message.edit(**_panel_edit_kwargs(message, view, gif_name))
        except discord.NotFound:
            await self.repos.settings.set_value(
                guild.id,
                "meta",
                "account_panel_message_id",
                None,
            )
        except discord.HTTPException:
            return

    @tasks.loop(minutes=1)
    async def restore_account_panel_loop(self):
        for guild in self.bot.guilds:
            await self.refresh_account_panel(guild, rotate_image=True)

    @restore_account_panel_loop.before_loop
    async def before_restore_account_panel_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="계정패널", description="현재 채널에 계정 정보 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def account_panel(self, interaction: discord.Interaction):
        gif_name = choose_gif(PANEL_GIFS)
        view = AccountView(self, gif_name)
        message = await interaction.channel.send(
            **_panel_send_kwargs(view, gif_name),
        )
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "account",
            "account_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("계정 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="유저정보조회", description="특정 유저의 계정 정보를 조회합니다.")
    @app_commands.default_permissions(send_messages=True)
    async def user_info(self, interaction: discord.Interaction, 유저: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if not await self._staff_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "셀러 또는 관리자 권한이 필요합니다."), ephemeral=True)
            return
        await interaction.followup.send(embed=await self.build_account_embed(interaction.guild, 유저), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AccountCog(bot))
