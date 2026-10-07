from __future__ import annotations

import base64
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


LANGUAGES = {"ko": "한국어", "en": "English", "ja": "日本語", "zh": "中文"}
TEXT = {
    "ko": ["화면의 보안 코드를 아래 버튼으로 입력하세요.", "보안 코드", "입력 상태", "상태", "남은 시간", "초", "남은 시도", "본인 인증 세션만 조작할 수 있습니다.", "인증 시도 횟수를 초과했습니다. 새 인증 세션을 시작해주세요.", "입력한 코드가 일치하지 않습니다. 다시 입력해주세요.", "`/역할설정`으로 인증 역할을 먼저 설정해주세요.", "봇 역할이 인증 역할보다 낮거나 역할 관리 권한이 없습니다.", "인증이 완료되어 {role} 역할이 지급되었습니다.", "인증 시간이 만료되었습니다. 다시 시작해주세요.", "서버 안에서만 사용할 수 있습니다.", "이미 인증이 완료되어 있습니다.", "삭제", "확인", "입력 대기", "잠김", "잘못된 코드", "설정 오류", "권한 오류", "인증 완료", "시간 만료"],
    "en": ["Enter the security code using the buttons below.", "Security code", "Your input", "Status", "Time left", "s", "Attempts left", "You can only use your own verification session.", "Too many attempts. Please start a new verification session.", "Incorrect code. Please try again.", "Please configure the verification role with `/역할설정` first.", "The bot needs Manage Roles permission and a role above the verification role.", "Verification complete. You received the {role} role.", "Verification timed out. Please start again.", "This is only available in a server.", "You are already verified.", "DELETE", "CONFIRM", "WAITING INPUT", "LOCKED", "INVALID CODE", "CONFIGURATION ERROR", "PERMISSION ERROR", "VERIFIED", "EXPIRED"],
    "ja": ["下のボタンで画面の認証コードを入力してください。", "認証コード", "入力内容", "状態", "残り時間", "秒", "残り試行回数", "自分の認証セッションのみ操作できます。", "試行回数の上限に達しました。認証をやり直してください。", "コードが一致しません。もう一度入力してください。", "先に `/역할설정` で認証ロールを設定してください。", "ボットにロール管理権限がないか、認証ロールより順位が低くなっています。", "認証が完了し、{role} ロールが付与されました。", "認証の有効期限が切れました。やり直してください。", "サーバー内でのみ使用できます。", "すでに認証済みです。", "削除", "確認", "入力待ち", "ロック済み", "コード不一致", "設定エラー", "権限エラー", "認証完了", "期限切れ"],
    "zh": ["请使用下方按钮输入屏幕上的验证码。", "验证码", "输入内容", "状态", "剩余时间", "秒", "剩余尝试次数", "您只能操作自己的验证会话。", "尝试次数已用尽。请重新开始验证。", "验证码不匹配，请重新输入。", "请先使用 `/역할설정` 配置验证身份组。", "机器人缺少管理身份组权限，或其身份组低于验证身份组。", "验证完成，已获得 {role} 身份组。", "验证已超时，请重新开始。", "只能在服务器内使用。", "您已完成验证。", "删除", "确认", "等待输入", "已锁定", "验证码错误", "配置错误", "权限错误", "验证完成", "已超时"],
}
STATUS_KEYS = ["WAITING INPUT", "LOCKED", "INVALID CODE", "CONFIGURATION ERROR", "PERMISSION ERROR", "VERIFIED", "EXPIRED"]


EASTER_LANGUAGES = {
    "clay": "점토판에 구운 뒤 유니코드 UTF-32로 복원한 수메르어",
    "dna": "DNA 염기서열 ACGT 4진법으로 인코딩한 산스크리트어",
    "dolphin": "돌고래 클릭음을 유니코드로 직렬화한 뒤 Base64로 인코딩한 고대 이집트어",
}


def easter_egg_message(language: str) -> str:
    # Fictional language ciphers, not translations into historical languages.
    raw = "깨비 바보".encode("utf-32-be")
    if language == "clay":
        alphabet = "𒀀𒀁𒀂𒀃𒀄𒀅𒀆𒀇𒀈𒀉𒀊𒀋𒀌𒀍𒀎𒀏"
        payload = " ".join("".join(alphabet[int(n, 16)] for n in raw[i:i + 4].hex())
                           for i in range(0, len(raw), 4))
    elif language == "dna":
        payload = " ".join("".join("ACGT"[(byte >> shift) & 3]
                                  for shift in (6, 4, 2, 0)) for byte in raw)
    elif language == "dolphin":
        clay = easter_egg_message("clay")
        dna = easter_egg_message("dna")
        payload = base64.b64encode((clay + "\n" + dna).encode("utf-32-be")).decode("ascii")
    else:
        raise ValueError("Unsupported Easter egg language")
    return payload


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
            label=pad.text[16],
            style=discord.ButtonStyle.danger,
            disabled=disabled,
        )
        self.pad = pad

    async def callback(self, interaction: discord.Interaction):
        await self.pad.clear(interaction)


class ConfirmButton(discord.ui.Button):
    def __init__(self, pad: "VerifyPad", *, disabled: bool = False):
        super().__init__(
            label=pad.text[17],
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
    def __init__(self, cog: "VerificationCog", user_id: int, code: str, gif_name: str | None, language: str = "ko"):
        super().__init__(timeout=VERIFY_TIMEOUT)
        self.language = language if language in LANGUAGES else "ko"
        self.text = TEXT[self.language]
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
        t = self.text
        status = t[18 + STATUS_KEYS.index(self.display_status)]
        lines = [
            "## DEVILBLOX VERIFICATION", t[0], "",
            f"### {t[1]}\n```fix\n{self.code}\n```",
            f"### {t[2]}\n```fix\n{filled}{empty}\n```",
            f"**{t[3]}**  `{status}`",
            f"**{t[4]}**  `{self._remaining()}{t[5]}` · "
            f"**{t[6]}**  `{max(0, MAX_ATTEMPTS - self.attempts)}`",
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
        await interaction.response.send_message(self.text[7], ephemeral=True)
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
                    self.text[8],
                )
                await interaction.response.edit_message(**self._edit_kwargs())
                return
            self._render(
                "INVALID CODE",
                0xE5484D,
                self.text[9],
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
                self.text[10],
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
                self.text[11],
            )
            await interaction.response.edit_message(**self._edit_kwargs())
            return

        await self.cog.users.set_verified(interaction.guild.id, interaction.user.id, role.id)
        self.disable_controls()
        await self.cog.send_verify_log(interaction, role)
        self._render(
            "VERIFIED",
            0x2ECC71,
            self.text[12].format(role=role.mention),
        )
        await interaction.response.edit_message(**self._edit_kwargs())

    async def on_timeout(self):
        self.disable_controls()
        if self.message is None:
            return
        self._render("EXPIRED", 0xE5484D, self.text[13])
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
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        container.add_item(discord.ui.ActionRow(*(
            discord.ui.Button(
                label=label, style=discord.ButtonStyle.secondary,
                custom_id=f"devilblox:verify:supported:{language}", disabled=True,
            )
            for language, label in LANGUAGES.items()
        )))
        self.add_item(container)

    async def start(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
            return

        await interaction.response.send_message(
            view=VerifyLanguageView(self.cog, interaction.user.id), ephemeral=True,
        )


class VerifyLanguageView(discord.ui.LayoutView):
    def __init__(self, cog: "VerificationCog", user_id: int):
        super().__init__(timeout=VERIFY_TIMEOUT)
        self.cog = cog
        self.user_id = user_id
        self.started = False
        container = discord.ui.Container(accent_color=COLOR_DARK)
        container.add_item(discord.ui.TextDisplay(
            "## LANGUAGE\n한국어 / English / 日本語 / 中文"
        ))
        row = discord.ui.ActionRow()
        for language, label in LANGUAGES.items():
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)

            async def select(interaction, language=language):
                await self.select_language(interaction, language)

            button.callback = select
            row.add_item(button)
        container.add_item(row)
        secret = discord.ui.Select(
            placeholder="그 밖의 언어… / Other languages…",
            options=[discord.SelectOption(label=label, value=key)
                     for key, label in EASTER_LANGUAGES.items()],
        )

        async def select_secret(interaction):
            await self.show_easter_egg(interaction, secret.values[0])

        secret.callback = select_secret
        container.add_item(discord.ui.ActionRow(secret))
        self.easter_display = discord.ui.TextDisplay("-# 다른 차원의 언어도 감지되었습니다.")
        container.add_item(self.easter_display)
        self.add_item(container)

    async def show_easter_egg(self, interaction: discord.Interaction, language: str):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(TEXT["ko"][7], ephemeral=True)
            return
        if self.started or self.is_finished():
            await interaction.response.send_message(TEXT["ko"][13], ephemeral=True)
            return
        payload = easter_egg_message(language)
        self.easter_display.content = (
            f"### {EASTER_LANGUAGES[language]}\n```\n{payload}\n```\n"
            "-# 이스터에그용 가상 암호입니다. 인증은 위의 네 언어 중 하나를 선택하세요."
        )
        await interaction.response.edit_message(view=self)

    async def select_language(self, interaction: discord.Interaction, language: str):
        t = TEXT[language]
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(t[7], ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message(t[14], ephemeral=True)
            return
        if self.started or self.is_finished():
            await interaction.response.send_message(t[13], ephemeral=True)
            return
        self.started = True
        await interaction.response.defer()
        settings = await self.cog.settings.get(interaction.guild.id)
        if has_role(interaction.user, settings["roles"].get("verified")):
            await interaction.edit_original_response(content=t[15], view=None)
            self.stop()
            return
        code = "".join(secrets.choice("0123456789") for _ in range(4))
        gif_name = choose_gif(VERIFY_GIFS)
        view = VerifyPad(self.cog, interaction.user.id, code, gif_name, language)
        files = branded_files(gif_file(gif_name))
        view.message = await interaction.edit_original_response(
            content=None, view=view, attachments=files,
        )
        self.stop()


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
        await interaction.response.defer(ephemeral=True, thinking=True)
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
        await interaction.followup.send(embed=success_embed("인증 패널 생성 완료"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VerificationCog(bot))
