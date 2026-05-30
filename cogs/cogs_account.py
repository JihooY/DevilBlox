from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import error_embed, info_embed, success_embed
from utils.panels import restore_panel_message, save_panel_location
from utils.roles import has_role


class ToggleAnonymousView(discord.ui.View):
    def __init__(self, cog: "AccountCog"):
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="중개 로그 익명 토글", style=discord.ButtonStyle.secondary)
    async def toggle(self, interaction: discord.Interaction, _: discord.ui.Button):
        new_value = await self.cog.repos.users.toggle_middleman_anonymous(interaction.guild.id, interaction.user.id)
        state = "익명 사용" if new_value else "익명 사용 안함"
        await interaction.response.send_message(embed=success_embed("익명 설정 변경", state), ephemeral=True)


class AccountView(discord.ui.View):
    def __init__(self, cog: "AccountCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="계정 정보", style=discord.ButtonStyle.success, custom_id="devilblox:account:info")
    async def info(self, interaction: discord.Interaction, _: discord.ui.Button):
        embed = await self.cog.build_account_embed(interaction.guild, interaction.user)
        await interaction.response.send_message(embed=embed, view=ToggleAnonymousView(self.cog), ephemeral=True)

    @discord.ui.button(label="보유 쿠폰", style=discord.ButtonStyle.primary, custom_id="devilblox:account:coupons")
    async def coupons(self, interaction: discord.Interaction, _: discord.ui.Button):
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

    async def build_account_embed(self, guild: discord.Guild, member: discord.Member) -> discord.Embed:
        settings = await self.repos.settings.get(guild.id)
        user = await self.repos.users.ensure_user(guild.id, member.id, settings["roles"].get("verified"))
        grade_role = guild.get_role(user.get("grade_role_id") or 0)
        embed = info_embed("ACCOUNT INFORMATION")
        embed.add_field(name="유저", value=f"{member.mention} (`{member.id}`)", inline=False)
        embed.add_field(name="현재 등급", value=grade_role.mention if grade_role else "미설정", inline=False)
        embed.add_field(name="누적 사용 금액", value=f"{user.get('accrued_spent', 0):,}원", inline=True)
        embed.add_field(name="보유 포인트", value=f"{user.get('points', 0):,}P", inline=True)
        embed.add_field(
            name="중개 로그 익명 여부",
            value="익명 사용중" if user.get("middleman_anonymous") else "익명 사용 안함",
            inline=False,
        )
        return embed

    async def refresh_account_panel(self, guild: discord.Guild):
        await restore_panel_message(
            self.repos,
            guild,
            "account",
            "account_panel_message_id",
            embed=info_embed("ACCOUNT INFO", "계정 정보와 보유 쿠폰을 확인할 수 있습니다."),
            view=AccountView(self),
        )

    @tasks.loop(seconds=1, count=1)
    async def restore_account_panel_loop(self):
        for guild in self.bot.guilds:
            await self.refresh_account_panel(guild)

    @restore_account_panel_loop.before_loop
    async def before_restore_account_panel_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="계정패널", description="현재 채널에 계정 정보 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def account_panel(self, interaction: discord.Interaction):
        message = await interaction.channel.send(
            embed=info_embed("ACCOUNT INFO", "계정 정보와 보유 쿠폰을 확인할 수 있습니다."),
            view=AccountView(self),
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
