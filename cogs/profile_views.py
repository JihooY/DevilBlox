from __future__ import annotations

import asyncio

import discord

from services.user_preferences import toggle_notification
from utils.embeds import BRAND_LOGO_URL, COLOR_INFO, branded_files
from utils.gifs import PANEL_GIFS, choose_gif, gif_file, gif_media_url
from utils.preferences import LANGUAGES, NOTIFICATION_ROLES, default_language
from utils.profile_i18n import profile_text


class ProfileView(discord.ui.LayoutView):
    def __init__(self, repos, member, user: dict, settings: dict, *, gif_name=None, welcome=False):
        super().__init__(timeout=300)
        self.repos = repos
        self.member = member
        self.user_id = member.id
        self.user = user
        self.settings = settings
        self.language = default_language(user)
        self.gif_name = gif_name
        self.welcome = welcome
        self.message = None
        self._update_lock = asyncio.Lock()
        self._render()

    def _text(self, key):
        return profile_text(key, self.language)

    def _render(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=COLOR_INFO)
        content = (
            f"## PROFILE\n{self._text('welcome' if self.welcome else 'description')}\n\n"
            f"{self.member.mention}\n"
            f"**{self._text('language')}** · {LANGUAGES[self.language]}"
        )
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(content),
            accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
        ))
        media_url = gif_media_url(self.gif_name)
        if media_url:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.MediaGallery(
                discord.MediaGalleryItem(media_url, description="DevilBlox profile"),
            ))
        container.add_item(discord.ui.Separator())
        select = discord.ui.Select(
            placeholder=self._text("language"),
            custom_id="devilblox:profile:language",
            options=[discord.SelectOption(label=label, value=code, default=code == self.language)
                     for code, label in LANGUAGES.items()],
        )

        async def select_language(interaction):
            await self.set_language(interaction, select.values[0])

        select.callback = select_language
        container.add_item(discord.ui.ActionRow(select))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
        status_lines = [f"### {self._text('notifications')}"]
        row = discord.ui.ActionRow()
        for role_key in NOTIFICATION_ROLES:
            role_id = self.settings["roles"].get(role_key) or 0
            role = self.member.guild.get_role(role_id)
            enabled = any(item.id == role_id for item in self.member.roles) if role else False
            state = "missing" if role is None else "on" if enabled else "off"
            status_lines.append(f"**{self._text(role_key)}** · {self._text(state)}")
            button = discord.ui.Button(
                label=f"{self._text(role_key)} · {self._text(state)}",
                style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.primary,
                custom_id=f"devilblox:profile:{role_key}",
                disabled=role is None,
            )

            async def toggle(interaction, role_key=role_key):
                await self.toggle_alarm(interaction, role_key)

            button.callback = toggle
            row.add_item(button)
        container.add_item(discord.ui.TextDisplay("\n".join(status_lines)))
        container.add_item(row)
        container.add_item(discord.ui.TextDisplay(f"-# {self._text('hint')}"))
        self.add_item(container)

    async def interaction_check(self, interaction):
        if interaction.guild is None or interaction.guild.id != self.member.guild.id:
            await interaction.response.send_message(self._text("guild"), ephemeral=True)
            return False
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(self._text("owner"), ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction, member=None):
        self.member = member or await interaction.guild.fetch_member(self.user_id)
        self.user = await self.repos.users.ensure_user(interaction.guild.id, self.user_id)
        self.settings = await self.repos.settings.get(interaction.guild.id)
        self.language = default_language(self.user)
        self.welcome = False
        self._render()
        await interaction.edit_original_response(**{"content": None, "embeds": [], "view": self})

    async def set_language(self, interaction, language):
        if not await self.interaction_check(interaction):
            return
        if language not in LANGUAGES:
            await interaction.response.send_message(self._text("retry"), ephemeral=True)
            return
        await interaction.response.defer()
        async with self._update_lock:
            await self.repos.users.set_default_language(interaction.guild.id, self.user_id, language)
            await self._refresh(interaction)

    async def toggle_alarm(self, interaction, role_key):
        if not await self.interaction_check(interaction):
            return
        await interaction.response.defer()
        async with self._update_lock:
            try:
                member, _ = await toggle_notification(
                    self.repos, interaction.guild, interaction.user, role_key,
                )
            except ValueError:
                await interaction.followup.send(self._text("missing_error"), ephemeral=True)
                return
            except discord.Forbidden:
                await interaction.followup.send(self._text("permission_error"), ephemeral=True)
                return
            except discord.HTTPException:
                await interaction.followup.send(self._text("retry"), ephemeral=True)
                return
            await self._refresh(interaction, member)

    async def on_timeout(self):
        for item in self.walk_children():
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def build_profile_view(repos, guild, member, *, welcome=False):
    user = await repos.users.ensure_user(guild.id, member.id)
    settings = await repos.settings.get(guild.id)
    return ProfileView(repos, member, user, settings, gif_name=choose_gif(PANEL_GIFS, guild_id=guild.id), welcome=welcome)


def profile_send_kwargs(view):
    return {"view": view, "files": branded_files(gif_file(view.gif_name))}


async def send_profile(interaction, repos, *, welcome=False):
    if interaction.guild is None:
        await interaction.response.send_message(profile_text("guild"), ephemeral=True)
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    view = await build_profile_view(repos, interaction.guild, interaction.user, welcome=welcome)
    view.message = await interaction.followup.send(**profile_send_kwargs(view), ephemeral=True, wait=True)
    return view
