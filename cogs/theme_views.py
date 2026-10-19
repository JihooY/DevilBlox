from __future__ import annotations

import logging

import discord

from services.themes import MAX_GIF_BYTES
from utils.gifs import ALL_GIFS, choose_gif, gif_file, gif_media_url


async def check_admin(interaction: discord.Interaction, guild_id: int, owner_id: int | None) -> bool:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if (interaction.guild_id == guild_id and (owner_id is None or interaction.user.id == owner_id)
            and permissions is not None and permissions.administrator):
        return True
    message = "서버 관리자만 사용할 수 있습니다." if owner_id is None else "이 화면을 연 서버 관리자만 사용할 수 있습니다."
    await interaction.response.send_message(message, ephemeral=True)
    return False


async def refresh_public_theme_panel(bot, guild_id: int, selected: str | None = None) -> None:
    cog = bot.get_cog("ThemesCog")
    if cog is not None:
        try:
            await cog.refresh_panel(guild_id, selected=selected)
        except Exception:
            # The mutation and private response have already succeeded. A
            # refresh failure must not tell the administrator to repeat it.
            logging.getLogger(__name__).exception("Failed to synchronize theme panel: guild_id=%s", guild_id)


class ThemeErrors:
    async def on_error(self, interaction, error, *args):
        message = str(error)
        if not isinstance(error, ValueError):
            logging.getLogger(__name__).error("Theme management failed", exc_info=(type(error), error, error.__traceback__))
            message = "테마 처리 중 오류가 발생했습니다. 새로고침 후 다시 시도해주세요."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class ThemeModal(ThemeErrors, discord.ui.Modal):
    def __init__(self, manager: "ThemeManagerView", *, upload: bool, owner_id: int | None = None):
        super().__init__(title="테마 GIF 업로드" if upload else "새 테마 만들기")
        self.manager = manager
        self.owner_id = owner_id if owner_id is not None else manager.owner_id
        self.upload = upload
        if upload:
            self.files = discord.ui.FileUpload(min_values=1, max_values=5)
            self.add_item(discord.ui.Label(
                text="GIF 파일 1~5개", description="파일당 최대 8MB · 실제 GIF 파일만 등록됩니다.",
                component=self.files,
            ))
        else:
            self.name = discord.ui.TextInput(label="테마 이름", placeholder="예: 스텔, 할로윈", max_length=40)
            self.add_item(self.name)

    async def interaction_check(self, interaction):
        return await check_admin(interaction, self.manager.guild_id, self.owner_id)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        manager = self.manager
        selected = manager.selected
        if self.upload:
            limit = min(MAX_GIF_BYTES, interaction.guild.filesize_limit)
            if any(file.size > limit for file in self.files.values):
                raise ValueError(f"GIF 파일은 파일당 {limit // (1024 * 1024)}MB 이하여야 합니다.")
            uploads = [(file.filename, await file.read()) for file in self.files.values]
            state = await manager.service.upload(manager.guild_id, manager.selected, uploads)
        else:
            state, selected = await manager.service.create(manager.guild_id, str(self.name))
        await interaction.edit_original_response(
            content=None,
            view=ThemeManagerView(
                manager.bot, manager.guild_id, interaction.user.id, state, selected,
                notice="GIF를 추가했습니다." if self.upload else "테마를 만들었습니다. GIF를 추가한 뒤 적용해주세요.",
            ),
        )
        await refresh_public_theme_panel(manager.bot, manager.guild_id,
                                         selected if manager.owner_id is None else None)


class ThemeManagerView(ThemeErrors, discord.ui.LayoutView):
    def __init__(self, bot, guild_id: int, owner_id: int | None, state: dict,
                 selected: str | None = None, *, notice: str | None = None):
        super().__init__(timeout=None if owner_id is None else 900)
        self.bot = bot
        self.service = bot.themes
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.selected = selected if selected in state["themes"] else state["active"]
        theme = state["themes"][self.selected]
        active_name = discord.utils.escape_markdown(state["themes"][state["active"]]["name"])
        name = discord.utils.escape_markdown(theme["name"])
        container = discord.ui.Container(accent_color=0x5865F2)
        container.add_item(discord.ui.TextDisplay(
            f"## GIF 테마 관리\n사용 중: **{active_name}**\n"
            f"선택한 테마: **{name}** · GIF **{len(theme['gifs'])}개**\n\n"
            "선택한 테마를 적용하면 그 안의 GIF만 랜덤으로 표시됩니다.\n"
            "새 메시지부터 적용되며 기존 패널은 1분 내 자동 갱신됩니다."
        ))
        selector = discord.ui.Select(placeholder="관리할 테마 선택", options=[
            discord.SelectOption(label=t["name"], value=key, default=key == self.selected,
                                 description=f"GIF {len(t['gifs'])}개" + (" · 사용 중" if key == state["active"] else ""))
            for key, t in state["themes"].items()
        ])
        if notice:
            container.add_item(discord.ui.TextDisplay(notice))
        selector.custom_id = f"devilblox:themes:{guild_id}:select"

        async def select(interaction):
            await interaction.response.defer()
            state = await self.service.state(self.guild_id)
            await self.update(interaction, state, selector.values[0])

        selector.callback = select
        container.add_item(discord.ui.ActionRow(selector))
        row = discord.ui.ActionRow()
        self.button(row, "테마 만들기", self.create, disabled=len(state["themes"]) >= 25)
        self.button(row, "이 테마 적용", self.activate, style=discord.ButtonStyle.success,
                    disabled=self.selected == state["active"] or not theme["gifs"])
        self.button(row, "미리보기", self.preview, disabled=not theme["gifs"])
        self.button(row, "테마 삭제", self.delete, style=discord.ButtonStyle.danger,
                    disabled=self.selected in ("default", state["active"]))
        container.add_item(row)
        row = discord.ui.ActionRow()
        self.button(row, "GIF 업로드", self.upload)
        self.button(row, "기존 GIF 추가", self.add_existing)
        self.button(row, "GIF 제거", self.remove, disabled=not theme["gifs"])
        self.button(row, "새로고침", self.refresh)
        container.add_item(row)
        self.add_item(container)

    def button(self, row, label, callback, *, style=discord.ButtonStyle.secondary, disabled=False):
        button = discord.ui.Button(
            label=label, style=style, disabled=disabled,
            custom_id=f"devilblox:themes:{self.guild_id}:{callback.__name__}",
        )
        button.callback = callback
        row.add_item(button)

    async def interaction_check(self, interaction):
        return await check_admin(interaction, self.guild_id, self.owner_id)

    async def update(self, interaction, state, selected=None):
        view = ThemeManagerView(self.bot, self.guild_id, self.owner_id, state, selected or self.selected)
        if self.owner_id is None:
            await self.bot.repos.settings.set_value(self.guild_id, "meta", "theme_panel_selected", view.selected)
        if interaction.response.is_done():
            await interaction.edit_original_response(content=None, view=view)
        else:
            await interaction.response.edit_message(content=None, view=view)
        if self.owner_id is not None:
            await refresh_public_theme_panel(self.bot, self.guild_id)

    async def create(self, interaction):
        await interaction.response.send_modal(ThemeModal(self, upload=False, owner_id=interaction.user.id))

    async def upload(self, interaction):
        await interaction.response.send_modal(ThemeModal(self, upload=True, owner_id=interaction.user.id))

    async def activate(self, interaction):
        await interaction.response.defer()
        state = await self.service.activate(self.guild_id, self.selected)
        await self.update(interaction, state)

    async def delete(self, interaction):
        await interaction.response.defer()
        state = await self.service.delete(self.guild_id, self.selected)
        await self.update(interaction, state)

    async def refresh(self, interaction):
        await interaction.response.defer()
        await self.update(interaction, await self.service.state(self.guild_id))

    async def preview(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = await self.service.state(self.guild_id)
        theme = self.service.theme(state, self.selected)
        filename = choose_gif(theme["gifs"])
        url = gif_media_url(filename)
        if url is None:
            raise ValueError("표시할 GIF가 없거나 현재 GIF 절전 모드입니다.")
        view = discord.ui.LayoutView(timeout=60)
        view.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(url)))
        file = gif_file(filename)
        await interaction.followup.send(view=view, files=[file] if file else [], ephemeral=True)

    async def add_existing(self, interaction):
        await self.picker(interaction, "add")

    async def remove(self, interaction):
        await self.picker(interaction, "remove")

    async def picker(self, interaction, mode):
        await interaction.response.defer()
        state = await self.service.state(self.guild_id)
        labels = {doc["_id"]: doc["label"] async for doc in self.service.store.media.find(
            {"guild_id": self.guild_id}, {"label": 1},
        )}
        await interaction.edit_original_response(view=GifPickerView(self, state, labels, mode))


class GifPickerView(ThemeErrors, discord.ui.LayoutView):
    def __init__(self, manager, state, labels, mode, page=0):
        super().__init__(timeout=None if manager.owner_id is None else 900)
        self.manager = manager
        self.state = state
        self.labels = labels
        self.mode = mode
        self.page = page
        theme = manager.service.theme(state, manager.selected)
        pool = [name for name in ALL_GIFS if name not in theme["gifs"]] if mode == "add" else theme["gifs"]
        page = min(page, max(0, (len(pool) - 1) // 25))
        self.page = page
        page_items = pool[page * 25:(page + 1) * 25]
        container = discord.ui.Container(accent_color=0x5865F2)
        container.add_item(discord.ui.TextDisplay(
            f"## {'기존 GIF 추가' if mode == 'add' else 'GIF 제거'}\n"
            f"{page + 1} / {max(1, (len(pool) + 24) // 25)} 페이지 · 여러 개 선택 가능"
        ))
        if page_items:
            selector = discord.ui.Select(placeholder="GIF 선택", min_values=1, max_values=len(page_items), options=[
                discord.SelectOption(label=labels.get(name, name)[:100], value=name) for name in page_items
            ])
            selector.custom_id = f"devilblox:themes:{manager.guild_id}:picker-{mode}-{page}"

            async def select(interaction):
                await interaction.response.defer()
                service = manager.service
                if mode == "add":
                    state = await service.add_existing(manager.guild_id, manager.selected, selector.values)
                else:
                    state = await service.remove(manager.guild_id, manager.selected, selector.values)
                await manager.update(interaction, state)

            selector.callback = select
            container.add_item(discord.ui.ActionRow(selector))
        else:
            container.add_item(discord.ui.TextDisplay("선택 가능한 GIF가 없습니다."))
        row = discord.ui.ActionRow()
        manager.button(row, "이전", self.previous, disabled=page == 0)
        manager.button(row, "다음", self.next, disabled=(page + 1) * 25 >= len(pool))
        manager.button(row, "테마 관리로", self.back)
        container.add_item(row)
        self.add_item(container)

    async def interaction_check(self, interaction):
        return await self.manager.interaction_check(interaction)

    async def previous(self, interaction):
        await self.navigate(interaction, self.page - 1)

    async def next(self, interaction):
        await self.navigate(interaction, self.page + 1)

    async def navigate(self, interaction, page):
        await interaction.response.defer()
        state = await self.manager.service.state(self.manager.guild_id)
        await interaction.edit_original_response(view=GifPickerView(self.manager, state, self.labels, self.mode, page))

    async def back(self, interaction):
        await interaction.response.defer()
        await self.manager.update(interaction, await self.manager.service.state(self.manager.guild_id))


async def open_theme_manager(bot, interaction: discord.Interaction) -> None:
    permissions = getattr(interaction.user, "guild_permissions", None)
    if interaction.guild_id is None or permissions is None or not permissions.administrator:
        await interaction.response.send_message("서버 관리자만 사용할 수 있습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    state = await bot.themes.state(interaction.guild_id)
    await interaction.followup.send(
        view=ThemeManagerView(bot, interaction.guild_id, interaction.user.id, state), ephemeral=True,
    )
