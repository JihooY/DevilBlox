from __future__ import annotations

import secrets
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import (
    BRAND_LOGO_FILENAME,
    BRAND_LOGO_URL,
    COLOR_DARK,
    branded_files,
    success_embed,
)
from utils.gifs import (
    VERIFY_GIFS,
    choose_gif,
    claim_local_gif_upload_slot,
    gif_file,
    gif_file_from_folder,
    gif_delivery_status,
    gif_media_url,
    is_gif_filename,
    retained_non_gif_attachments,
)
from utils.panels import save_panel_location
from utils.roles import has_role

VERIFY_TIMEOUT = 120
MAX_ATTEMPTS = 3


class NumberButton(discord.ui.Button):
    def __init__(self, pad: "VerifyPad", number: str, *, disabled: bool = False):
        super().__init__(
            label=number,
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.pad = pad
        self.number = number

    async def callback(self, interaction: discord.Interaction):
        await self.pad.press_number(interaction, self.number)


class ClearButton(discord.ui.Button):
    def __init__(self, pad: "VerifyPad", *, disabled: bool = False):
        super().__init__(
            label="DELETE",
            style=discord.ButtonStyle.danger,
            disabled=disabled,
        )
        self.pad = pad

    async def callback(self, interaction: discord.Interaction):
        await self.pad.clear(interaction)


class ConfirmButton(discord.ui.Button):
    def __init__(self, pad: "VerifyPad", *, disabled: bool = False):
        super().__init__(
            label="CONFIRM",
            style=discord.ButtonStyle.success,
            disabled=disabled,
        )
        self.pad = pad

    async def callback(self, interaction: discord.Interaction):
        await self.pad.confirm(interaction)


def _add_brand_section(container: discord.ui.Container, content: str):
    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(content),
            accessory=discord.ui.Thumbnail(
                BRAND_LOGO_URL,
                description="DevilBlox logo",
            ),
        )
    )


def _add_gif_media(
    container: discord.ui.Container,
    gif_name: str | None,
    description: str,
):
    media_url = gif_media_url(gif_name)
    if media_url:
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media_url, description=description)
            )
        )


def _verify_send_kwargs(
    view: discord.ui.LayoutView,
    media_file: discord.File | None,
) -> dict:
    kwargs = {"view": view}
    files = branded_files(media_file)
    if files:
        kwargs["files"] = files
    return kwargs


class VerifyPad(discord.ui.LayoutView):
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
        self.number_order = list("123456789")
        secrets.SystemRandom().shuffle(self.number_order)
        self.controls_disabled = False
        self.display_status = "WAITING INPUT"
        self.accent_color = COLOR_DARK
        self.note: str | None = None
        self._render()

    def disable_controls(self):
        self.controls_disabled = True
        for item in self.walk_children():
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    def _asset_url(self) -> str | None:
        return gif_media_url(self.gif_name)

    def _remaining(self) -> int:
        return max(0, VERIFY_TIMEOUT - int(time.time() - self.created_at))

    def _content(self) -> str:
        filled = "■ " * len(self.input_code)
        empty = "□ " * (4 - len(self.input_code))
        lines = [
            "## DEVILBLOX VERIFICATION",
            "화면의 보안 코드를 아래 버튼으로 입력하세요.",
            "",
            f"### 보안 코드\n```fix\n{self.code}\n```",
            f"### 입력 상태\n```fix\n{filled}{empty}\n```",
            f"**상태**  `{self.display_status}`",
            f"**남은 시간**  `{self._remaining()}초` · "
            f"**남은 시도**  `{max(0, MAX_ATTEMPTS - self.attempts)}`",
        ]
        if self.note:
            lines.extend(("", self.note))
        return "\n".join(lines)

    def _render(
        self,
        status: str | None = None,
        color: int | None = None,
        note: str | None = None,
    ) -> None:
        if status is not None:
            self.display_status = status
        if color is not None:
            self.accent_color = color
        self.note = note
        self.clear_items()
        container = discord.ui.Container(accent_color=self.accent_color)
        _add_brand_section(container, self._content())
        _add_gif_media(container, self.gif_name, "DevilBlox verification challenge")
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        for index in range(0, len(self.number_order), 3):
            container.add_item(
                discord.ui.ActionRow(
                    *(
                        NumberButton(
                            self,
                            number,
                            disabled=self.controls_disabled,
                        )
                        for number in self.number_order[index : index + 3]
                    )
                )
            )
        container.add_item(
            discord.ui.ActionRow(
                ClearButton(self, disabled=self.controls_disabled),
                NumberButton(self, "0", disabled=self.controls_disabled),
                ConfirmButton(self, disabled=self.controls_disabled),
            )
        )
        self.add_item(container)

    def _edit_kwargs(self) -> dict:
        return {"content": None, "embeds": [], "view": self}

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
        self._render()
        await interaction.response.edit_message(**self._edit_kwargs())

    async def clear(self, interaction: discord.Interaction):
        if not await self.interaction_allowed(interaction):
            return
        self.input_code = ""
        self._render()
        await interaction.response.edit_message(**self._edit_kwargs())

    async def confirm(self, interaction: discord.Interaction):
        if not await self.interaction_allowed(interaction):
            return

        if self.input_code != self.code:
            self.attempts += 1
            self.input_code = ""
            if self.attempts >= MAX_ATTEMPTS:
                self.disable_controls()
                self._render(
                    "LOCKED",
                    0xE5484D,
                    "인증 시도 횟수를 초과했습니다. 새 인증 세션을 시작해주세요.",
                )
                await interaction.response.edit_message(**self._edit_kwargs())
                return
            self._render(
                "INVALID CODE",
                0xE5484D,
                "입력한 코드가 일치하지 않습니다. 다시 입력해주세요.",
            )
            await interaction.response.edit_message(**self._edit_kwargs())
            return

        settings = await self.cog.settings.get(interaction.guild.id)
        role_id = settings["roles"].get("verified")
        role = interaction.guild.get_role(role_id or 0)
        if role is None:
            self.disable_controls()
            self._render(
                "CONFIGURATION ERROR",
                0xE5484D,
                "`/역할설정`으로 인증 역할을 먼저 설정해주세요.",
            )
            await interaction.response.edit_message(**self._edit_kwargs())
            return

        try:
            await interaction.user.add_roles(role, reason="DevilBlox verification completed")
        except discord.Forbidden:
            self.disable_controls()
            self._render(
                "PERMISSION ERROR",
                0xE5484D,
                "봇 역할이 인증 역할보다 낮거나 역할 관리 권한이 없습니다.",
            )
            await interaction.response.edit_message(**self._edit_kwargs())
            return

        await self.cog.users.set_verified(interaction.guild.id, interaction.user.id, role.id)
        self.disable_controls()
        await self.cog.send_verify_log(interaction, role)
        self._render(
            "VERIFIED",
            0x2ECC71,
            f"인증이 완료되어 {role.mention} 역할이 지급되었습니다.",
        )
        await interaction.response.edit_message(**self._edit_kwargs())

    async def on_timeout(self):
        self.disable_controls()
        if self.message is None:
            return
        self._render("EXPIRED", 0xE5484D, "인증 시간이 만료되었습니다. 다시 시작해주세요.")
        try:
            await self.message.edit(**self._edit_kwargs())
        except discord.HTTPException:
            pass


class VerifyStartView(discord.ui.LayoutView):
    def __init__(self, cog: "VerificationCog", *, include_media: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        container = discord.ui.Container(accent_color=COLOR_DARK)
        _add_brand_section(
            container,
            "## DEVILBLOX VERIFICATION\n서버 이용을 시작하려면 아래 버튼으로 인증을 완료해주세요.",
        )
        if include_media:
            _add_gif_media(container, "verify_panel.gif", "DevilBlox verification panel")
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        start = discord.ui.Button(
            label="START VERIFICATION",
            style=discord.ButtonStyle.success,
            custom_id="devilblox:verify:start",
        )
        start.callback = self.start
        container.add_item(discord.ui.ActionRow(start))
        self.add_item(container)

    async def start(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
            return

        settings = await self.cog.settings.get(interaction.guild.id)
        verified_role_id = settings["roles"].get("verified")
        if has_role(interaction.user, verified_role_id):
            await interaction.response.send_message("이미 인증이 완료되어 있습니다.", ephemeral=True)
            return

        code = "".join(secrets.choice("0123456789") for _ in range(4))
        gif_name = choose_gif(VERIFY_GIFS)
        view = VerifyPad(self.cog, interaction.user.id, code, gif_name)
        file = gif_file(gif_name)

        await interaction.response.defer(ephemeral=True)
        view.message = await interaction.followup.send(
            **_verify_send_kwargs(view, file),
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

    def build_verify_panel_view(self, *, include_media: bool = True) -> VerifyStartView:
        return VerifyStartView(self, include_media=include_media)

    async def refresh_verify_panel(self, guild: discord.Guild):
        settings = await self.settings.get(guild.id)
        channel = guild.get_channel(settings["channels"].get("verify") or 0)
        message_id = settings["meta"].get("verify_panel_message_id")
        if channel is None or not message_id or not hasattr(channel, "fetch_message"):
            return

        try:
            message = await channel.fetch_message(message_id)
            status = gif_delivery_status()
            gif_attachments = [
                attachment
                for attachment in message.attachments
                if is_gif_filename(attachment.filename)
            ]
            has_logo = any(
                attachment.filename == BRAND_LOGO_FILENAME for attachment in message.attachments
            )
            retained = retained_non_gif_attachments(message)
            include_media = (
                status.effective_mode != "local"
                or bool(gif_attachments)
                or claim_local_gif_upload_slot()
            )
            update: dict = {
                "content": None,
                "embeds": [],
                "view": self.build_verify_panel_view(include_media=include_media),
            }
            if status.effective_mode == "local":
                if not gif_attachments and include_media:
                    file = gif_file_from_folder("verify_panel.gif", "banners")
                    if has_logo:
                        update["attachments"] = [
                            *retained,
                            *([file] if file is not None else []),
                        ]
                    else:
                        update["attachments"] = [*branded_files(file), *retained]
                elif not has_logo:
                    update["attachments"] = [
                        *branded_files(),
                        *retained,
                        *gif_attachments,
                    ]
            elif gif_attachments or not has_logo:
                update["attachments"] = (
                    retained if has_logo else [*branded_files(), *retained]
                )
            await message.edit(**update)
        except discord.NotFound:
            await self.settings.set_value(guild.id, "meta", "verify_panel_message_id", None)
        except discord.HTTPException:
            return

    @tasks.loop(minutes=1)
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
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

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
        status = gif_delivery_status()
        include_media = status.effective_mode != "local" or claim_local_gif_upload_slot()
        file = gif_file_from_folder("verify_panel.gif", "banners") if include_media else None
        view = self.build_verify_panel_view(include_media=include_media)

        message = await interaction.channel.send(**_verify_send_kwargs(view, file))
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
