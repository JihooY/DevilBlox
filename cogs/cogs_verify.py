from __future__ import annotations

import secrets
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.assets import asset_path, has_asset
from utils.embeds import COLOR_DARK, error_embed, success_embed
from utils.panels import restore_panel_message, save_panel_location
from utils.roles import has_role

VERIFY_TIMEOUT = 120
MAX_ATTEMPTS = 3
VERIFY_GIFS = ("verify1.gif", "verify2.gif", "verify3.gif", "festival_pair.gif", "starlight_panel.gif")


class NumberButton(discord.ui.Button):
    def __init__(self, number: str, row: int):
        super().__init__(label=number, style=discord.ButtonStyle.secondary, row=row)
        self.number = number

    async def callback(self, interaction: discord.Interaction):
        await self.view.press_number(interaction, self.number)


class ClearButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="DELETE", style=discord.ButtonStyle.danger, row=3)

    async def callback(self, interaction: discord.Interaction):
        await self.view.clear(interaction)


class ConfirmButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="CONFIRM", style=discord.ButtonStyle.success, row=3)

    async def callback(self, interaction: discord.Interaction):
        await self.view.confirm(interaction)


class VerifyPad(discord.ui.View):
    def __init__(self, cog: "VerificationCog", user_id: int, code: str, gif_name: str | None):
        super().__init__(timeout=VERIFY_TIMEOUT)
        self.cog = cog
        self.user_id = user_id
        self.code = code
        self.gif_name = gif_name
        self.input_code = ""
        self.attempts = 0
        self.created_at = time.time()
        self.message: discord.WebhookMessage | None = None

        numbers = list("123456789")
        secrets.SystemRandom().shuffle(numbers)
        for index, number in enumerate(numbers):
            self.add_item(NumberButton(number, index // 3))
        self.add_item(ClearButton())
        self.add_item(NumberButton("0", 3))
        self.add_item(ConfirmButton())

    def disable_controls(self):
        for item in self.children:
            item.disabled = True

    def _asset_url(self) -> str | None:
        if not self.gif_name:
            return None
        return f"attachment://{self.gif_name}"

    def _remaining(self) -> int:
        return max(0, VERIFY_TIMEOUT - int(time.time() - self.created_at))

    def build_embed(self, status: str = "WAITING INPUT", color: int = COLOR_DARK) -> discord.Embed:
        filled = "■ " * len(self.input_code)
        empty = "□ " * (4 - len(self.input_code))
        embed = discord.Embed(
            title="DEVILBLOX VERIFICATION",
            description="화면의 보안 코드를 아래 버튼으로 입력하세요.",
            color=color,
        )
        embed.add_field(name="보안 코드", value=f"```fix\n{self.code}\n```", inline=True)
        embed.add_field(name="입력 상태", value=f"```fix\n{filled}{empty}\n```", inline=True)
        embed.add_field(name="상태", value=f"```yaml\n{status}\n```", inline=False)
        embed.add_field(name="남은 시간", value=f"`{self._remaining()}초`", inline=True)
        embed.add_field(name="남은 시도", value=f"`{MAX_ATTEMPTS - self.attempts}`", inline=True)
        if self._asset_url():
            embed.set_image(url=self._asset_url())
        return embed

    async def interaction_allowed(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("본인 인증 세션만 조작할 수 있습니다.", ephemeral=True)
        return False

    async def press_number(self, interaction: discord.Interaction, number: str):
        if not await self.interaction_allowed(interaction):
            return
        if len(self.input_code) < 4:
            self.input_code += number
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def clear(self, interaction: discord.Interaction):
        if not await self.interaction_allowed(interaction):
            return
        self.input_code = ""
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def confirm(self, interaction: discord.Interaction):
        if not await self.interaction_allowed(interaction):
            return

        if self.input_code != self.code:
            self.attempts += 1
            self.input_code = ""
            if self.attempts >= MAX_ATTEMPTS:
                self.disable_controls()
                await interaction.response.edit_message(
                    embed=self.build_embed("LOCKED", 0xE5484D),
                    view=self,
                )
                return
            await interaction.response.edit_message(
                embed=self.build_embed("INVALID CODE", 0xE5484D),
                view=self,
            )
            return

        settings = await self.cog.settings.get(interaction.guild.id)
        role_id = settings["roles"].get("verified")
        role = interaction.guild.get_role(role_id or 0)
        if role is None:
            await interaction.response.edit_message(
                embed=error_embed("인증 설정 오류", "`/역할설정`으로 인증 역할을 먼저 설정해주세요."),
                view=None,
            )
            return

        try:
            await interaction.user.add_roles(role, reason="DevilBlox verification completed")
        except discord.Forbidden:
            await interaction.response.edit_message(
                embed=error_embed("권한 오류", "봇 역할이 인증 역할보다 낮거나 역할 관리 권한이 없습니다."),
                view=None,
            )
            return

        await self.cog.users.set_verified(interaction.guild.id, interaction.user.id, role.id)
        self.disable_controls()
        await self.cog.send_verify_log(interaction, role)
        await interaction.response.edit_message(
            embed=success_embed("인증 완료", f"{role.mention} 역할이 지급되었습니다."),
            view=self,
        )

    async def on_timeout(self):
        self.disable_controls()
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.build_embed("EXPIRED", 0xE5484D), view=self)
        except discord.HTTPException:
            pass


class VerifyStartView(discord.ui.View):
    def __init__(self, cog: "VerificationCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="START VERIFICATION",
        style=discord.ButtonStyle.success,
        custom_id="devilblox:verify:start",
    )
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
            return

        settings = await self.cog.settings.get(interaction.guild.id)
        verified_role_id = settings["roles"].get("verified")
        if has_role(interaction.user, verified_role_id):
            await interaction.response.send_message("이미 인증이 완료되어 있습니다.", ephemeral=True)
            return

        code = "".join(secrets.choice("0123456789") for _ in range(4))
        available_gifs = [name for name in VERIFY_GIFS if has_asset("gifs", name)]
        gif_name = secrets.choice(available_gifs) if available_gifs else None
        view = VerifyPad(self.cog, interaction.user.id, code, gif_name)
        embed = view.build_embed()
        file = None
        if gif_name and has_asset("gifs", gif_name):
            file = discord.File(str(asset_path("gifs", gif_name)), filename=gif_name)

        await interaction.response.defer(ephemeral=True)
        view.message = await interaction.followup.send(
            embed=embed,
            view=view,
            file=file,
            ephemeral=True,
            wait=True,
        )


class VerificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(VerifyStartView(self))

    async def cog_load(self):
        self.restore_verify_panel_loop.start()

    async def cog_unload(self):
        self.restore_verify_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    @property
    def settings(self):
        return self.bot.repos.settings

    @property
    def users(self):
        return self.bot.repos.users

    async def refresh_verify_panel(self, guild: discord.Guild):
        await restore_panel_message(
            self.repos,
            guild,
            "verify",
            "verify_panel_message_id",
            view=VerifyStartView(self),
        )

    @tasks.loop(seconds=1, count=1)
    async def restore_verify_panel_loop(self):
        for guild in self.bot.guilds:
            await self.refresh_verify_panel(guild)

    @restore_verify_panel_loop.before_loop
    async def before_restore_verify_panel_loop(self):
        await self.bot.wait_until_ready()

    async def send_verify_log(self, interaction: discord.Interaction, role: discord.Role):
        settings = await self.settings.get(interaction.guild.id)
        channel = interaction.guild.get_channel(settings["channels"].get("verify_log") or 0)
        if channel is None:
            return
        embed = discord.Embed(title="VERIFY LOG", color=0x5865F2)
        embed.add_field(name="유저", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="역할", value=role.mention, inline=False)
        await channel.send(embed=embed)

    @app_commands.command(name="인증역할", description="인증 성공 시 지급할 역할을 설정합니다.")
    @app_commands.default_permissions(administrator=True)
    async def verify_role(self, interaction: discord.Interaction, 역할: discord.Role):
        await self.settings.set_value(interaction.guild.id, "roles", "verified", 역할.id)
        await interaction.response.send_message(
            embed=success_embed("인증 역할 설정 완료", f"인증 역할: {역할.mention}"),
            ephemeral=True,
        )

    @app_commands.command(name="인증패널", description="현재 채널에 인증 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def verify_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="DEVILBLOX VERIFICATION",
            description="서버 이용을 시작하려면 아래 버튼으로 인증을 완료해주세요.",
            color=COLOR_DARK,
        )
        file = None
        if has_asset("banners", "verify_panel.gif"):
            file = discord.File(str(asset_path("banners", "verify_panel.gif")), filename="verify_panel.gif")
            embed.set_image(url="attachment://verify_panel.gif")

        message = await interaction.channel.send(embed=embed, file=file, view=VerifyStartView(self))
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "verify",
            "verify_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("인증 패널 생성 완료"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VerificationCog(bot))
