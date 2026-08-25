from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.assets import asset_path, has_asset
from utils.embeds import BRAND_LOGO_URL, branded_files, error_embed, success_embed
from utils.roles import has_role

MUSEUM_ACCENT_COLOR = 0xB3122C

MUSEUM_ANNOUNCE_TEXT = (
    "# 🔥 2,000명, 그 여정의 대가 🔥\n"
    "### DEVIL BLOX 역사 박물관, 지금 개관합니다\n\n"
    "아홉 달 전, 두 개의 작은 서버가 하나로 합쳐지며 이 이야기는 시작됐습니다.\n"
    "매일같이 올라온 공지, 잠 못 자고 버틴 봇 점검, 디도스를 뚫고 지켜낸 하루, "
    "셀러와 헬퍼들의 면접, 대회의 함성, 그리고 100명마다 함께 나눈 감사 인사까지.\n\n"
    "그 모든 순간이 쌓여 만들어진 대가가 바로 지금의 **2,000명**입니다.\n\n"
    "그 기록을 모아 박물관을 지었습니다. 지금 바로 입장해서 걸어보세요.\n"
    "-# 아래 버튼을 누르면 박물관으로 이동합니다."
)


def build_museum_view(museum_url: str) -> discord.ui.LayoutView:
    """Build the maximum-impact Components V2 container for the 2,000-member museum announcement."""
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=MUSEUM_ACCENT_COLOR)

    container.add_item(
        discord.ui.Section(
            discord.ui.TextDisplay(MUSEUM_ANNOUNCE_TEXT),
            accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
        )
    )

    if has_asset("logos", "devilblox_logo.png"):
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    "attachment://devilblox_logo.png",
                    description="DEVIL BLOX 역사 박물관",
                )
            )
        )

    container.add_item(discord.ui.Separator())
    row = discord.ui.ActionRow()
    row.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.link,
            url=museum_url,
            label="🏛️ 역사 박물관 입장하기",
        )
    )
    container.add_item(row)

    view.add_item(container)
    return view


class MuseumCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def repos(self):
        return self.bot.repos

    async def admin_allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        if interaction.user.guild_permissions.administrator:
            return True
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("admin"))

    async def resolve_announce_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None
    ) -> discord.TextChannel | discord.Thread | None:
        if channel is not None:
            return channel
        settings = await self.repos.settings.get(interaction.guild.id)
        channel_id = settings["channels"].get("event_announce")
        configured = interaction.guild.get_channel(channel_id or 0) if channel_id else None
        return configured or interaction.channel

    @app_commands.command(
        name="이벤트시작",
        description="2,000명 기념 역사 박물관 이벤트를 지정된 공지 채널에 시작합니다.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(채널="공지를 보낼 채널 (비우면 설정된 이벤트 공지 채널 또는 현재 채널을 사용합니다)")
    async def start_museum_event(
        self,
        interaction: discord.Interaction,
        채널: discord.TextChannel | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return

        target_channel = await self.resolve_announce_channel(interaction, 채널)
        if target_channel is None:
            await interaction.followup.send(
                embed=error_embed("채널 없음", "`/채널설정`으로 이벤트 공지 채널을 먼저 설정해주세요."),
                ephemeral=True,
            )
            return

        files = []
        if has_asset("logos", "devilblox_logo.png"):
            files.append(discord.File(str(asset_path("logos", "devilblox_logo.png")), filename="devilblox_logo.png"))
        files = branded_files(*files)

        await target_channel.send(
            content="@everyone",
            view=build_museum_view(self.bot.config.museum_url),
            files=files,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        await interaction.followup.send(
            embed=success_embed("이벤트 공지 완료", f"{target_channel.mention}에 역사 박물관 이벤트를 공지했습니다."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MuseumCog(bot))
