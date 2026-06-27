from __future__ import annotations

import secrets

import discord
from discord import app_commands
from discord.ext import commands

from utils.embeds import BRAND_LOGO_FILENAME, BRAND_LOGO_URL, branded_files, error_embed, info_embed, success_embed
from utils.roles import has_role


COLOR_LOTTERY = 0xF1C40F
SHAPES = ("○", "△", "□")
LOTTERY_ENTRY_CUSTOM_ID = "devilblox:lottery:enter"


def clamp_winner_count(value: int) -> int:
    return max(1, int(value))


def panel_attachments(message: discord.Message) -> list[discord.Attachment | discord.File]:
    logo_attachments = [attachment for attachment in message.attachments if attachment.filename == BRAND_LOGO_FILENAME]
    return logo_attachments or branded_files()


def draw_winning_shapes(rng: secrets.SystemRandom) -> list[str]:
    shape = rng.choice(SHAPES)
    return [shape, shape, shape]


def draw_losing_shapes(rng: secrets.SystemRandom) -> list[str]:
    candidates = [
        [first, second, third]
        for first in SHAPES
        for second in SHAPES
        for third in SHAPES
        if not (first == second == third)
    ]
    return rng.choice(candidates)


def format_shapes(shapes: list[str] | tuple[str, ...]) -> str:
    return "  ".join(shapes or ("?", "?", "?"))


def format_reveal_slots(shapes: list[str], revealed_count: int) -> str:
    slots = []
    for index in range(3):
        value = shapes[index] if index < revealed_count and index < len(shapes) else "?"
        slots.append(f"[ {value} ]")
    return "  ".join(slots)


def reveal_status_text(revealed_count: int, is_winner: bool) -> str:
    if revealed_count <= 0:
        return "복권을 손에 쥐었습니다. 첫 칸부터 천천히 열어보세요."
    if revealed_count == 1:
        return "첫 번째 칸이 열렸습니다."
    if revealed_count == 2:
        return "두 번째 칸까지 열렸습니다. 마지막 칸만 남았습니다."
    return "같은 도형 3개 연속입니다." if is_winner else "같은 도형 3개 연속이 나오지 않았습니다."


class LotteryEntryView(discord.ui.LayoutView):
    def __init__(self, cog: "LotteryCog", event: dict | None = None, entry_count: int = 0):
        super().__init__(timeout=None)
        self.cog = cog
        self.event = event or {}
        self.entry_count = entry_count

        status = self.event.get("status", "open")
        title = self.event.get("title") or "도형 복권 추첨"
        lottery_id = self.event.get("_id") or "준비중"
        winner_count = int(self.event.get("winner_count", 1) or 1)
        status_text = "신청 접수중" if status == "open" else "마감됨"

        container = discord.ui.Container(accent_color=COLOR_LOTTERY)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "\n".join(
                        [
                            "## SHAPE LOTTERY",
                            f"**{title}**",
                            f"추첨번호: `{lottery_id}`",
                            f"상태: `{status_text}` · 신청자 `{entry_count}`명 · 당첨 예정 `{winner_count}`명",
                            "신청 후 마감되면 `/복권까기`로 복권을 열 수 있습니다.",
                        ]
                    )
                ),
                accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
            )
        )
        container.add_item(discord.ui.Separator())

        enter_button = discord.ui.Button(
            label="복권 신청",
            style=discord.ButtonStyle.success,
            custom_id=LOTTERY_ENTRY_CUSTOM_ID,
            disabled=bool(self.event) and status != "open",
        )
        enter_button.callback = self.enter
        container.add_item(discord.ui.ActionRow(enter_button))
        self.add_item(container)

    async def enter(self, interaction: discord.Interaction):
        await self.cog.handle_entry(interaction)


class LotteryTicketRevealView(discord.ui.LayoutView):
    def __init__(self, cog: "LotteryCog", event: dict, entry: dict, user_id: int, revealed_count: int = 0):
        super().__init__(timeout=900)
        self.cog = cog
        self.event = event
        self.entry = entry
        self.user_id = user_id
        self.revealed_count = max(0, min(3, int(revealed_count)))

        shapes = list(entry.get("shapes") or [])
        is_complete = self.revealed_count >= 3
        is_winner = bool(entry.get("is_winner"))
        accent_color = 0x2ECC71 if is_complete and is_winner else 0xE5484D if is_complete else COLOR_LOTTERY
        title = "당첨!" if is_complete and is_winner else "아쉽게도 꽝" if is_complete else "복권 개봉"

        container = discord.ui.Container(accent_color=accent_color)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "\n".join(
                        [
                            "## SHAPE LOTTERY",
                            f"**{title}**",
                            f"`{format_reveal_slots(shapes, self.revealed_count)}`",
                            f"{self.revealed_count}/3",
                            reveal_status_text(self.revealed_count, is_winner),
                            f"추첨: {event.get('title', '도형 복권 추첨')} (`{event['_id']}`)",
                        ]
                    )
                ),
                accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
            )
        )
        container.add_item(discord.ui.Separator())

        if self.revealed_count == 0:
            label = "첫 번째 칸 열기"
        elif self.revealed_count == 1:
            label = "두 번째 칸 열기"
        elif self.revealed_count == 2:
            label = "마지막 칸 열기"
        else:
            label = "개봉 완료"

        reveal_button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.success if not is_complete else discord.ButtonStyle.secondary,
            disabled=is_complete,
        )
        reveal_button.callback = self.reveal_next
        container.add_item(discord.ui.ActionRow(reveal_button))
        self.add_item(container)

    async def reveal_next(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("이 복권은 본인만 열 수 있습니다.", ephemeral=True)
            return
        await self.cog.handle_ticket_reveal(
            interaction,
            self.event,
            self.entry,
            self.user_id,
            self.revealed_count + 1,
        )


class LotteryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(LotteryEntryView(self))
        self.rng = secrets.SystemRandom()

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

    async def resolve_event(self, interaction: discord.Interaction, lottery_id: str = "", *, statuses=("open", "drawn")):
        if not interaction.guild:
            return None
        if lottery_id.strip():
            return await self.repos.lottery.get_event(interaction.guild.id, lottery_id.strip())
        return await self.repos.lottery.latest_event(interaction.guild.id, statuses=tuple(statuses))

    async def refresh_panel(self, guild: discord.Guild, event: dict):
        channel_id = event.get("channel_id")
        message_id = event.get("message_id")
        if not channel_id or not message_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(message_id)
            entry_count = await self.repos.lottery.count_entries(guild.id, event["_id"])
            await message.edit(
                view=LotteryEntryView(self, event, entry_count),
                attachments=panel_attachments(message),
            )
        except discord.HTTPException:
            return

    async def handle_entry(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild or interaction.message is None:
            await interaction.followup.send(embed=error_embed("신청 실패", "서버 추첨 패널에서만 신청할 수 있습니다."), ephemeral=True)
            return

        event = await self.repos.lottery.get_event_by_message(interaction.guild.id, interaction.message.id)
        if event is None:
            await interaction.followup.send(embed=error_embed("추첨 없음", "이 패널과 연결된 추첨을 찾을 수 없습니다."), ephemeral=True)
            return
        if event.get("status") != "open":
            await interaction.followup.send(embed=error_embed("신청 마감", "이미 마감된 추첨입니다."), ephemeral=True)
            return

        _, created = await self.repos.lottery.add_entry(event, interaction.user.id, interaction.user.display_name)
        entry_count = await self.repos.lottery.count_entries(interaction.guild.id, event["_id"])
        await interaction.message.edit(
            view=LotteryEntryView(self, event, entry_count),
            attachments=panel_attachments(interaction.message),
        )

        if created:
            await interaction.followup.send(
                embed=success_embed("복권 신청 완료", "마감 후 `/복권까기`로 결과를 확인할 수 있습니다."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=info_embed("이미 신청됨", "이미 이 추첨에 신청되어 있습니다."), ephemeral=True)

    def build_assignments(self, entries: list[dict], winner_count: int) -> tuple[dict[int, tuple[bool, list[str]]], list[int]]:
        draw_count = min(clamp_winner_count(winner_count), len(entries))
        winner_ids = set(self.rng.sample([entry["user_id"] for entry in entries], draw_count))
        assignments = {}
        for entry in entries:
            is_winner = entry["user_id"] in winner_ids
            shapes = draw_winning_shapes(self.rng) if is_winner else draw_losing_shapes(self.rng)
            assignments[entry["user_id"]] = (is_winner, shapes)
        return assignments, list(winner_ids)

    async def handle_ticket_reveal(
        self,
        interaction: discord.Interaction,
        event: dict,
        entry: dict,
        user_id: int,
        revealed_count: int,
    ):
        revealed_count = max(0, min(3, int(revealed_count)))
        if revealed_count >= 3:
            entry = await self.repos.lottery.mark_opened(event["guild_id"], event["_id"], user_id)

        kwargs = {"view": LotteryTicketRevealView(self, event, entry, user_id, revealed_count)}
        if interaction.message is not None:
            kwargs["attachments"] = panel_attachments(interaction.message)
        await interaction.response.edit_message(**kwargs)

    async def send_lottery_ticket_dm(self, event: dict, entry: dict) -> bool:
        user_id = int(entry["user_id"])
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(
                content=f"`{event.get('title', '도형 복권 추첨')}` 복권이 도착했습니다.",
                view=LotteryTicketRevealView(self, event, entry, user_id),
                files=branded_files(),
            )
        except discord.HTTPException:
            return False
        return True

    async def send_lottery_ticket_dms(self, event: dict, entries: list[dict]) -> tuple[int, list[int]]:
        sent = 0
        failed_user_ids = []
        for entry in entries:
            if await self.send_lottery_ticket_dm(event, entry):
                sent += 1
            else:
                failed_user_ids.append(int(entry["user_id"]))
        return sent, failed_user_ids

    @app_commands.command(name="추첨패널", description="도형 복권 추첨 신청 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        제목="추첨 제목",
        당첨인원="당첨될 인원 수. 최소 1명이며 신청자가 부족하면 신청자 수에 맞춰집니다.",
    )
    async def create_lottery_panel(self, interaction: discord.Interaction, 제목: str, 당첨인원: int = 1):
        await interaction.response.defer(ephemeral=True)
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return
        if 당첨인원 < 1:
            await interaction.followup.send(embed=error_embed("인원 오류", "당첨 인원은 1명 이상이어야 합니다."), ephemeral=True)
            return

        event = await self.repos.lottery.create_event(
            interaction.guild.id,
            제목,
            당첨인원,
            interaction.user.id,
        )
        message = await interaction.channel.send(
            view=LotteryEntryView(self, event, 0),
            files=branded_files(),
        )
        await self.repos.lottery.attach_panel(interaction.guild.id, event["_id"], interaction.channel.id, message.id)
        await interaction.followup.send(
            embed=success_embed("추첨 패널 생성 완료", f"추첨번호: `{event['_id']}` · 당첨 예정 `{당첨인원}`명"),
            ephemeral=True,
        )

    @app_commands.command(name="추첨당첨인원", description="열려있는 추첨의 당첨 인원을 변경합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        당첨인원="변경할 당첨 인원 수",
        추첨번호="비워두면 가장 최근 열린 추첨을 사용합니다.",
    )
    async def set_lottery_winner_count(self, interaction: discord.Interaction, 당첨인원: int, 추첨번호: str = ""):
        await interaction.response.defer(ephemeral=True)
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return
        if 당첨인원 < 1:
            await interaction.followup.send(embed=error_embed("인원 오류", "당첨 인원은 1명 이상이어야 합니다."), ephemeral=True)
            return

        event = await self.resolve_event(interaction, 추첨번호, statuses=("open",))
        if event is None or event.get("status") != "open":
            await interaction.followup.send(embed=error_embed("추첨 없음", "변경할 수 있는 열린 추첨을 찾을 수 없습니다."), ephemeral=True)
            return

        event = await self.repos.lottery.update_winner_count(interaction.guild.id, event["_id"], 당첨인원)
        await self.refresh_panel(interaction.guild, event)
        await interaction.followup.send(
            embed=success_embed("당첨 인원 변경 완료", f"추첨번호 `{event['_id']}` · 당첨 예정 `{event['winner_count']}`명"),
            ephemeral=True,
        )

    @app_commands.command(name="추첨마감", description="신청을 마감하고 복권 결과를 확정합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(추첨번호="비워두면 가장 최근 열린 추첨을 사용합니다.")
    async def close_lottery(self, interaction: discord.Interaction, 추첨번호: str = ""):
        await interaction.response.defer(ephemeral=True)
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return

        event = await self.resolve_event(interaction, 추첨번호, statuses=("open",))
        if event is None or event.get("status") != "open":
            await interaction.followup.send(embed=error_embed("추첨 없음", "마감할 열린 추첨을 찾을 수 없습니다."), ephemeral=True)
            return

        entries = await self.repos.lottery.list_entries(interaction.guild.id, event["_id"])
        if not entries:
            await interaction.followup.send(embed=error_embed("신청자 없음", "신청자가 없어 추첨을 마감할 수 없습니다."), ephemeral=True)
            return

        assignments, winner_ids = self.build_assignments(entries, int(event.get("winner_count", 1) or 1))
        event = await self.repos.lottery.save_draw(event, assignments, winner_ids)
        await self.refresh_panel(interaction.guild, event)
        drawn_entries = await self.repos.lottery.list_entries(interaction.guild.id, event["_id"])
        dm_sent, dm_failed_user_ids = await self.send_lottery_ticket_dms(event, drawn_entries)

        winner_mentions = []
        for user_id in winner_ids:
            member = interaction.guild.get_member(user_id)
            winner_mentions.append(member.mention if member else f"`{user_id}`")

        embed = success_embed(
            "추첨 마감 완료",
            f"신청자 `{len(entries)}`명 중 `{len(winner_ids)}`명이 당첨 복권을 받았습니다.\n"
            f"DM 복권 발송 `{dm_sent}`명 완료, 실패 `{len(dm_failed_user_ids)}`명입니다.\n"
            "DM을 못 받은 참여자는 `/복권까기`로도 열 수 있습니다.",
        )
        embed.add_field(name="추첨번호", value=f"`{event['_id']}`", inline=True)
        embed.add_field(name="당첨자", value=", ".join(winner_mentions)[:1024], inline=False)
        if dm_failed_user_ids:
            failed_mentions = []
            for user_id in dm_failed_user_ids:
                member = interaction.guild.get_member(user_id)
                failed_mentions.append(member.mention if member else f"`{user_id}`")
            embed.add_field(name="DM 실패", value=", ".join(failed_mentions)[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="복권발송", description="마감된 추첨의 복권 개봉 DM을 다시 발송합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(추첨번호="비워두면 가장 최근 마감 추첨을 사용합니다.")
    async def resend_lottery_tickets(self, interaction: discord.Interaction, 추첨번호: str = ""):
        await interaction.response.defer(ephemeral=True)
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return

        event = await self.resolve_event(interaction, 추첨번호, statuses=("drawn",))
        if event is None or event.get("status") != "drawn":
            await interaction.followup.send(embed=error_embed("추첨 없음", "복권을 발송할 마감된 추첨을 찾을 수 없습니다."), ephemeral=True)
            return

        entries = await self.repos.lottery.list_entries(interaction.guild.id, event["_id"])
        if not entries:
            await interaction.followup.send(embed=error_embed("신청자 없음", "발송할 복권이 없습니다."), ephemeral=True)
            return

        dm_sent, dm_failed_user_ids = await self.send_lottery_ticket_dms(event, entries)
        embed = success_embed(
            "복권 DM 발송 완료",
            f"추첨번호 `{event['_id']}` · 발송 `{dm_sent}`명 · 실패 `{len(dm_failed_user_ids)}`명",
        )
        if dm_failed_user_ids:
            failed_mentions = []
            for user_id in dm_failed_user_ids:
                member = interaction.guild.get_member(user_id)
                failed_mentions.append(member.mention if member else f"`{user_id}`")
            embed.add_field(name="DM 실패", value=", ".join(failed_mentions)[:1024], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="복권까기", description="마감된 추첨의 도형 복권을 엽니다.")
    @app_commands.describe(추첨번호="비워두면 내가 참여한 가장 최근 마감 추첨을 사용합니다.")
    async def open_lottery_ticket(self, interaction: discord.Interaction, 추첨번호: str = ""):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return

        if 추첨번호.strip():
            event = await self.repos.lottery.get_event(interaction.guild.id, 추첨번호.strip())
            entry = await self.repos.lottery.get_entry(interaction.guild.id, 추첨번호.strip(), interaction.user.id)
        else:
            event, entry = await self.repos.lottery.latest_drawn_for_user(interaction.guild.id, interaction.user.id)

        if event is None:
            open_event, open_entry = await self.repos.lottery.latest_open_for_user(interaction.guild.id, interaction.user.id)
            if open_event is not None and open_entry is not None:
                await interaction.followup.send(embed=info_embed("아직 마감 전", "신청은 완료됐고, 관리자가 추첨을 마감하면 복권을 열 수 있습니다."), ephemeral=True)
                return
            await interaction.followup.send(embed=error_embed("복권 없음", "열 수 있는 복권을 찾을 수 없습니다."), ephemeral=True)
            return
        if event.get("status") != "drawn":
            await interaction.followup.send(embed=info_embed("아직 마감 전", "관리자가 추첨을 마감하면 복권을 열 수 있습니다."), ephemeral=True)
            return
        if entry is None:
            await interaction.followup.send(embed=error_embed("신청 기록 없음", "이 추첨에 신청한 기록이 없습니다."), ephemeral=True)
            return

        await interaction.followup.send(
            view=LotteryTicketRevealView(self, event, entry, interaction.user.id),
            files=branded_files(),
            ephemeral=True,
        )

    @app_commands.command(name="추첨현황", description="추첨 신청자와 상태를 확인합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(추첨번호="비워두면 가장 최근 추첨을 사용합니다.")
    async def lottery_status(self, interaction: discord.Interaction, 추첨번호: str = ""):
        await interaction.response.defer(ephemeral=True)
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return

        event = await self.resolve_event(interaction, 추첨번호, statuses=("open", "drawn"))
        if event is None:
            await interaction.followup.send(embed=error_embed("추첨 없음", "확인할 추첨을 찾을 수 없습니다."), ephemeral=True)
            return

        entries = await self.repos.lottery.list_entries(interaction.guild.id, event["_id"])
        opened = sum(1 for entry in entries if entry.get("opened"))
        winners = sum(1 for entry in entries if entry.get("is_winner"))
        embed = info_embed("추첨 현황", event.get("title", "도형 복권 추첨"))
        embed.add_field(name="추첨번호", value=f"`{event['_id']}`", inline=True)
        embed.add_field(name="상태", value="신청중" if event.get("status") == "open" else "마감됨", inline=True)
        embed.add_field(name="신청자", value=f"{len(entries)}명", inline=True)
        embed.add_field(name="당첨 예정/확정", value=f"{event.get('winner_count', 1)}명 / {winners}명", inline=True)
        embed.add_field(name="복권 오픈", value=f"{opened}명", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LotteryCog(bot))
