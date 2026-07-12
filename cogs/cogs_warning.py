from __future__ import annotations

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from utils.embeds import BRAND_LOGO_URL, branded_files, error_embed, info_embed, success_embed
from utils.roles import has_role


COLOR_WARNING = 0xF1C40F
COLOR_BLOCK = 0xE5484D
COLOR_CLEAR = 0x2ECC71

ACTION_LABELS = {
    "add": "경고 추가",
    "subtract": "경고 삭감",
    "block": "차단",
    "unblock": "차단 해제",
}


def timestamp_text(value) -> str:
    if not isinstance(value, datetime):
        return "-"
    timestamp = int(value.timestamp())
    return f"<t:{timestamp}:F> (<t:{timestamp}:R>)"


class WarningNoticeView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        action: str,
        target_id: int,
        actor_id: int,
        count_after: int,
        blocked: bool,
        amount: int = 0,
        reason: str = "",
    ):
        super().__init__(timeout=None)

        if action == "add":
            accent_color = COLOR_WARNING
            change_text = f"+{amount}회"
        elif action == "subtract":
            accent_color = COLOR_CLEAR
            change_text = f"{amount}회"
        elif action == "block":
            accent_color = COLOR_BLOCK
            change_text = "구매 차단"
        else:
            accent_color = COLOR_CLEAR
            change_text = "차단 해제"

        lines = [
            "## 경고 알림",
            f"처리: **{ACTION_LABELS.get(action, action)}** (`{change_text}`)",
            f"대상: <@{target_id}> (`{target_id}`)",
            f"처리자: <@{actor_id}> (`{actor_id}`)",
            f"현재 경고: `{count_after}`회",
            f"차단 상태: `{'차단됨' if blocked else '정상'}`",
        ]
        if reason.strip():
            lines.append(f"사유: {reason.strip()[:300]}")

        container = discord.ui.Container(accent_color=accent_color)
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay("\n".join(lines)),
                accessory=discord.ui.Thumbnail(BRAND_LOGO_URL, description="DevilBlox logo"),
            )
        )
        self.add_item(container)


class WarningCog(commands.Cog):
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

    async def get_notice_channel(self, interaction: discord.Interaction):
        if not interaction.guild:
            return None
        settings = await self.repos.settings.get(interaction.guild.id)
        channel = interaction.guild.get_channel(settings["channels"].get("warning_log") or 0)
        if channel is not None:
            return channel
        return interaction.channel if hasattr(interaction.channel, "send") else None

    async def send_warning_notice(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        target: discord.Member,
        doc: dict,
        amount: int = 0,
        reason: str = "",
    ):
        channel = await self.get_notice_channel(interaction)
        if channel is None:
            return

        await channel.send(
            view=WarningNoticeView(
                action=action,
                target_id=target.id,
                actor_id=interaction.user.id,
                count_after=int(doc.get("warning_count", 0) or 0),
                blocked=bool(doc.get("blocked")),
                amount=amount,
                reason=reason,
            ),
            files=branded_files(),
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )

    async def guard_admin(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return False
        if not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True)
            return False
        return True

    @app_commands.command(name="경고추가", description="유저에게 경고를 추가합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(대상="경고를 추가할 유저", 횟수="추가할 경고 수", 사유="경고 사유")
    async def add_warning(
        self,
        interaction: discord.Interaction,
        대상: discord.Member,
        횟수: int = 1,
        사유: str = "",
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self.guard_admin(interaction):
            return
        if 횟수 < 1 or 횟수 > 100:
            await interaction.followup.send(embed=error_embed("횟수 오류", "경고 수는 1~100 사이로 입력해주세요."), ephemeral=True)
            return

        doc = await self.repos.warnings.add_warnings(
            interaction.guild.id,
            대상.id,
            대상.display_name,
            횟수,
            interaction.user.id,
            사유,
        )
        await self.send_warning_notice(interaction, action="add", target=대상, doc=doc, amount=횟수, reason=사유)
        await interaction.followup.send(
            embed=success_embed("경고 추가 완료", f"{대상.mention} 현재 경고 `{doc.get('warning_count', 0)}`회"),
            ephemeral=True,
        )

    @app_commands.command(name="경고삭감", description="유저의 경고를 삭감합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(대상="경고를 삭감할 유저", 횟수="삭감할 경고 수", 사유="삭감 사유")
    async def subtract_warning(
        self,
        interaction: discord.Interaction,
        대상: discord.Member,
        횟수: int = 1,
        사유: str = "",
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self.guard_admin(interaction):
            return
        if 횟수 < 1 or 횟수 > 100:
            await interaction.followup.send(embed=error_embed("횟수 오류", "경고 수는 1~100 사이로 입력해주세요."), ephemeral=True)
            return

        doc = await self.repos.warnings.subtract_warnings(
            interaction.guild.id,
            대상.id,
            대상.display_name,
            횟수,
            interaction.user.id,
            사유,
        )
        await self.send_warning_notice(
            interaction,
            action="subtract",
            target=대상,
            doc=doc,
            amount=-횟수,
            reason=사유,
        )
        await interaction.followup.send(
            embed=success_embed("경고 삭감 완료", f"{대상.mention} 현재 경고 `{doc.get('warning_count', 0)}`회"),
            ephemeral=True,
        )

    @app_commands.command(name="경고차단", description="경고 시스템에서 유저 구매 차단 상태를 변경합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(
        상태=[
            app_commands.Choice(name="차단", value="block"),
            app_commands.Choice(name="차단 해제", value="unblock"),
        ]
    )
    @app_commands.describe(대상="차단 상태를 변경할 유저", 상태="차단 또는 차단 해제", 사유="차단/해제 사유")
    async def warning_block(
        self,
        interaction: discord.Interaction,
        대상: discord.Member,
        상태: app_commands.Choice[str],
        사유: str = "",
    ):
        await interaction.response.defer(ephemeral=True)
        if not await self.guard_admin(interaction):
            return

        blocked = 상태.value == "block"
        doc = await self.repos.warnings.set_blocked(
            interaction.guild.id,
            대상.id,
            대상.display_name,
            blocked,
            interaction.user.id,
            사유,
        )
        action = "block" if blocked else "unblock"
        await self.send_warning_notice(interaction, action=action, target=대상, doc=doc, reason=사유)
        await interaction.followup.send(
            embed=success_embed("차단 상태 변경 완료", f"{대상.mention} 상태: `{'차단됨' if blocked else '정상'}`"),
            ephemeral=True,
        )

    @app_commands.command(name="경고확인", description="본인 또는 유저의 경고와 차단 상태를 확인합니다.")
    @app_commands.default_permissions(send_messages=True)
    @app_commands.describe(대상="확인할 유저. 비우면 본인을 확인합니다.")
    async def warning_status(self, interaction: discord.Interaction, 대상: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild:
            await interaction.followup.send(embed=error_embed("처리 실패", "서버 안에서만 사용할 수 있습니다."), ephemeral=True)
            return

        target = 대상 or interaction.user
        if target.id != interaction.user.id and not await self.admin_allowed(interaction):
            await interaction.followup.send(embed=error_embed("권한 없음", "다른 유저의 경고는 관리자만 확인할 수 있습니다."), ephemeral=True)
            return

        doc = await self.repos.warnings.get(interaction.guild.id, target.id) or {}
        events = await self.repos.warnings.list_events(interaction.guild.id, target.id, limit=5) if doc else []
        embed = info_embed("경고 확인", f"{target.mention} 경고 상태입니다.")
        embed.add_field(name="경고 수", value=f"`{int(doc.get('warning_count', 0) or 0)}`회", inline=True)
        embed.add_field(name="차단 상태", value="`차단됨`" if doc.get("blocked") else "`정상`", inline=True)
        if doc.get("blocked"):
            embed.add_field(name="차단 사유", value=(doc.get("block_reason") or "사유 없음")[:1024], inline=False)
            embed.add_field(name="차단 시간", value=timestamp_text(doc.get("blocked_at")), inline=False)
        if events:
            lines = []
            for event in events:
                label = ACTION_LABELS.get(event.get("action"), event.get("action", "-"))
                delta = int(event.get("delta", 0) or 0)
                delta_text = f" ({delta:+d})" if delta else ""
                reason = event.get("reason") or "사유 없음"
                lines.append(
                    f"- {timestamp_text(event.get('created_at'))}: {label}{delta_text} · {reason[:80]}"
                )
            embed.add_field(name="최근 기록", value="\n".join(lines)[:1024], inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WarningCog(bot))
