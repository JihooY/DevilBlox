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


class AlarmView(discord.ui.LayoutView):
    def __init__(self, cog: "AlarmCog", gif_name: str | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        container = discord.ui.Container(accent_color=COLOR_INFO)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "## ALARM SETTING\n받고 싶은 알림 역할을 켜거나 끌 수 있습니다."
                ),
                accessory=discord.ui.Thumbnail(
                    BRAND_LOGO_URL,
                    description="DevilBlox logo",
                ),
            )
        )
        media_url = gif_media_url(gif_name)
        if media_url:
            container.add_item(discord.ui.Separator())
            container.add_item(
                discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media_url, description="DevilBlox alarm panel")
                )
            )
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        announcement = discord.ui.Button(
            label="공지 알림",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:alarm:announcement",
        )
        announcement.callback = self.announcement
        seller = discord.ui.Button(
            label="티켓 상태 알림",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:alarm:seller",
        )
        seller.callback = self.seller
        stock = discord.ui.Button(
            label="입고 알림",
            style=discord.ButtonStyle.primary,
            custom_id="devilblox:alarm:stock",
        )
        stock.callback = self.stock
        container.add_item(discord.ui.ActionRow(announcement, seller, stock))
        self.add_item(container)

    async def toggle_role(self, interaction: discord.Interaction, role_key: str, label: str):
        settings = await self.cog.repos.settings.get(interaction.guild.id)
        role = interaction.guild.get_role(settings["roles"].get(role_key) or 0)
        if role is None:
            await interaction.response.send_message(
                embed=error_embed("알림 역할 미설정", f"`/역할설정`으로 {label} 역할을 먼저 설정해주세요."),
                ephemeral=True,
            )
            return

        if role in interaction.user.roles:
            await interaction.user.remove_roles(role, reason="DevilBlox alarm toggle")
            await interaction.response.send_message(embed=success_embed("알림 해제", f"{label}을 비활성화했습니다."), ephemeral=True)
        else:
            await interaction.user.add_roles(role, reason="DevilBlox alarm toggle")
            await interaction.response.send_message(embed=success_embed("알림 설정", f"{label}을 활성화했습니다."), ephemeral=True)

    async def announcement(self, interaction: discord.Interaction):
        await self.toggle_role(interaction, "alarm_announcement", "공지 알림")

    async def seller(self, interaction: discord.Interaction):
        await self.toggle_role(interaction, "alarm_seller", "티켓 상태 알림")

    async def stock(self, interaction: discord.Interaction):
        await self.toggle_role(interaction, "alarm_stock", "입고 알림")


class AlarmCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(AlarmView(self))

    async def cog_load(self):
        self.restore_alarm_panel_loop.start()

    async def cog_unload(self):
        self.restore_alarm_panel_loop.cancel()

    @property
    def repos(self):
        return self.bot.repos

    async def refresh_alarm_panel(self, guild: discord.Guild, *, rotate_image: bool = False):
        settings = await self.repos.settings.get(guild.id)
        channel_id = settings["channels"].get("alarm")
        message_id = settings["meta"].get("alarm_panel_message_id")
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
            view = AlarmView(self, gif_name)
            await message.edit(**_panel_edit_kwargs(message, view, gif_name))
        except discord.NotFound:
            await self.repos.settings.set_value(
                guild.id,
                "meta",
                "alarm_panel_message_id",
                None,
            )
        except discord.HTTPException:
            return

    @tasks.loop(minutes=1)
    async def restore_alarm_panel_loop(self):
        for guild in self.bot.guilds:
            await self.refresh_alarm_panel(guild, rotate_image=True)

    @restore_alarm_panel_loop.before_loop
    async def before_restore_alarm_panel_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="알림패널", description="현재 채널에 알림 설정 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def alarm_panel(self, interaction: discord.Interaction):
        gif_name = choose_gif(PANEL_GIFS)
        view = AlarmView(self, gif_name)
        message = await interaction.channel.send(
            **_panel_send_kwargs(view, gif_name),
        )
        await save_panel_location(
            self.repos,
            interaction.guild.id,
            "alarm",
            "alarm_panel_message_id",
            interaction.channel.id,
            message.id,
        )
        await interaction.response.send_message(embed=success_embed("알림 패널 생성 완료"), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AlarmCog(bot))
