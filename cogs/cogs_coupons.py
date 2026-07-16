from __future__ import annotations

import random
import re

import discord
from discord import app_commands
from discord.ext import commands

from database.coupons import normalize_code
from utils.embeds import branded_files, error_embed, info_embed, success_embed
from utils.roles import has_role


COLOR = 0x9B59B6


def invite_code(value: str) -> str | None:
    match = re.search(r"(?:discord\.gg/|discord(?:app)?\.com/invite/)([\w-]+)", value, re.I)
    return match.group(1) if match else (value.strip() or None)


class CouponCreateModal(discord.ui.Modal, title="쿠폰 생성"):
    code = discord.ui.TextInput(label="쿠폰 코드", max_length=40)
    name = discord.ui.TextInput(label="쿠폰 이름", max_length=100)
    discount = discord.ui.TextInput(label="할인값", placeholder="10 또는 5000", max_length=10)
    kind = discord.ui.TextInput(label="종류", placeholder="일반 또는 특별", max_length=10)
    discount_type = discord.ui.TextInput(label="할인 방식", placeholder="퍼센트 또는 고정", max_length=10)

    def __init__(self, cog):
        super().__init__(); self.cog = cog

    async def on_submit(self, interaction):
        await self.cog.create_coupon_from_input(interaction, str(self.code), str(self.name), str(self.kind), str(self.discount), str(self.discount_type))


class PromotionCreateModal(discord.ui.Modal, title="프로모션 코드 생성"):
    code = discord.ui.TextInput(label="프로모션 코드", max_length=40)
    name = discord.ui.TextInput(label="프로모션 이름 / 유튜버", max_length=100)
    invite = discord.ui.TextInput(label="전용 초대 링크", max_length=200)
    discount = discord.ui.TextInput(label="할인율 (%)", placeholder="10", max_length=3)

    def __init__(self, cog):
        super().__init__(); self.cog = cog

    async def on_submit(self, interaction):
        await self.cog.create_promotion_from_input(interaction, str(self.code), str(self.name), str(self.invite), str(self.discount))


class DeleteModal(discord.ui.Modal, title="쿠폰 / 프로모션 삭제"):
    code = discord.ui.TextInput(label="삭제할 코드", max_length=40)
    def __init__(self, cog): super().__init__(); self.cog = cog
    async def on_submit(self, interaction): await self.cog.delete_code(interaction, str(self.code))


class CouponAdminView(discord.ui.LayoutView):
    def __init__(self, cog, coupons=None, promotions=None):
        super().__init__(timeout=None); self.cog = cog
        coupons, promotions = coupons or [], promotions or []
        lines = ["## COUPON CONTROL", "쿠폰과 초대 전용 프로모션 코드를 관리합니다.", "",
                 f"활성 쿠폰 `{len(coupons)}`개 · 프로모션 `{len(promotions)}`개"]
        for item in coupons[:6]:
            kind = "일반" if item.get("kind") == "general" else "특별"
            value = f"{int(item['discount']):,}원" if item.get("discount_type") == "fixed" else f"{item['discount']}%"
            lines.append(f"- `{item['code']}` · {item['name']} · {kind} {value}")
        for item in promotions[:4]:
            lines.append(f"- `{item['code']}` · {item['name']} · 프로모션 {item['discount']}%")
        box = discord.ui.Container(accent_color=COLOR)
        box.add_item(discord.ui.TextDisplay("\n".join(lines)))
        box.add_item(discord.ui.Separator())
        create = discord.ui.Button(label="쿠폰 생성", style=discord.ButtonStyle.success, custom_id="devilblox:coupon:create")
        promo = discord.ui.Button(label="프로모션 생성", style=discord.ButtonStyle.primary, custom_id="devilblox:coupon:promotion")
        delete = discord.ui.Button(label="삭제", style=discord.ButtonStyle.danger, custom_id="devilblox:coupon:delete")
        refresh = discord.ui.Button(label="새로고침", style=discord.ButtonStyle.secondary, custom_id="devilblox:coupon:refresh")
        create.callback = self.create; promo.callback = self.promo; delete.callback = self.delete; refresh.callback = self.refresh
        box.add_item(discord.ui.ActionRow(create, promo, delete, refresh)); self.add_item(box)

    async def allowed(self, interaction):
        if await self.cog.admin_allowed(interaction): return True
        await interaction.response.send_message(embed=error_embed("권한 없음", "관리자 권한이 필요합니다."), ephemeral=True); return False
    async def create(self, interaction):
        if await self.allowed(interaction): await interaction.response.send_modal(CouponCreateModal(self.cog))
    async def promo(self, interaction):
        if await self.allowed(interaction): await interaction.response.send_modal(PromotionCreateModal(self.cog))
    async def delete(self, interaction):
        if await self.allowed(interaction): await interaction.response.send_modal(DeleteModal(self.cog))
    async def refresh(self, interaction):
        if not await self.allowed(interaction): return
        await interaction.response.defer(ephemeral=True); await self.cog.refresh_admin_message(interaction)


class CouponSelect(discord.ui.Select):
    def __init__(self, cog, items, context, channel_id=None):
        self.cog, self.context, self.channel_id = cog, context, channel_id
        options = [discord.SelectOption(label="쿠폰 사용 안 함", value="none")]
        for owned in items[:24]:
            c = owned["coupon"]
            options.append(discord.SelectOption(label=f"{c['name']} ({owned['quantity']}장)", value=c["code"],
                                                description=f"{c['discount']}% 할인 · {c['code']}"))
        super().__init__(placeholder="보유 쿠폰을 선택하세요 (selection2)", options=options)
    async def callback(self, interaction):
        code = None if self.values[0] == "none" else self.values[0]
        await self.cog.select_coupon(interaction, self.context, code, self.channel_id)


class CouponSelectView(discord.ui.LayoutView):
    def __init__(self, cog, items, context, channel_id=None):
        super().__init__(timeout=300)
        box = discord.ui.Container(accent_color=COLOR)
        box.add_item(discord.ui.TextDisplay("## 보유 쿠폰\n사용할 쿠폰을 선택하면 구매에 적용됩니다."))
        box.add_item(discord.ui.ActionRow(CouponSelect(cog, items, context, channel_id)))
        self.add_item(box)


class CouponCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot; self.invite_cache = {}; self.bot.add_view(CouponAdminView(self))

    @property
    def repos(self): return self.bot.repos

    async def cog_load(self):
        if self.bot.is_ready(): await self.cache_invites()

    async def admin_allowed(self, interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member): return False
        if interaction.user.guild_permissions.administrator: return True
        settings = await self.repos.settings.get(interaction.guild.id)
        return has_role(interaction.user, settings["roles"].get("admin"))

    async def send_coupon_log(self, guild: discord.Guild, title: str, description: str, **fields):
        settings = await self.repos.settings.get(guild.id)
        channel = guild.get_channel(settings["channels"].get("coupon_log") or 0)
        if channel is None:
            return
        embed = info_embed(title, description)
        for name, value in fields.items():
            if value is not None:
                embed.add_field(name=name.replace("_", " "), value=str(value)[:1024], inline=False)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_ready(self): await self.cache_invites()

    async def cache_invites(self):
        for guild in self.bot.guilds:
            try: self.invite_cache[guild.id] = {x.code: x.uses or 0 for x in await guild.invites()}
            except discord.HTTPException: pass

    @commands.Cog.listener()
    async def on_member_join(self, member):
        try: current = await member.guild.invites()
        except discord.HTTPException: return
        before = self.invite_cache.get(member.guild.id, {})
        used = next((x for x in current if (x.uses or 0) > before.get(x.code, 0)), None)
        self.invite_cache[member.guild.id] = {x.code: x.uses or 0 for x in current}
        if used: await self.repos.coupons.attribute_invite(member.guild.id, member.id, used.code, used.inviter.id if used.inviter else None)

    async def create_coupon_from_input(self, interaction, code, name, kind, discount, discount_type):
        await interaction.response.defer(ephemeral=True)
        mapping = {"일반": "general", "특별": "special", "general": "general", "special": "special"}
        type_mapping = {"퍼센트": "percent", "비율": "percent", "percent": "percent", "고정": "fixed", "fixed": "fixed"}
        try: doc = await self.repos.coupons.create_coupon(interaction.guild.id, code, name, mapping[kind.strip().lower()], int(discount), interaction.user.id, type_mapping[discount_type.strip().lower()])
        except (ValueError, KeyError):
            await interaction.followup.send(embed=error_embed("입력 오류", "종류는 일반/특별, 방식은 퍼센트/고정으로 입력해주세요. 고정 할인은 특별 쿠폰만 가능합니다."), ephemeral=True); return
        value = f"{doc['discount']:,}원" if doc.get("discount_type") == "fixed" else f"{doc['discount']}%"
        await interaction.followup.send(embed=success_embed("쿠폰 생성 완료", f"`{doc['code']}` · {value}"), ephemeral=True)
        await self.send_coupon_log(interaction.guild, "COUPON CREATED", f"{interaction.user.mention}님이 쿠폰을 생성했습니다.", 코드=f"`{doc['code']}`", 이름=doc["name"], 할인=value)
        await self.refresh_admin_message(interaction)

    async def create_promotion_from_input(self, interaction, code, name, invite, discount):
        await interaction.response.defer(ephemeral=True); parsed = invite_code(invite)
        try: doc = await self.repos.coupons.create_promotion(interaction.guild.id, code, name, parsed, invite, int(discount), interaction.user.id)
        except ValueError:
            await interaction.followup.send(embed=error_embed("입력 오류", "초대 링크와 1~100 할인율을 확인해주세요."), ephemeral=True); return
        await interaction.followup.send(embed=success_embed("프로모션 생성 완료", f"`{doc['code']}` · 초대 `{doc['invite_code']}`"), ephemeral=True)
        await self.send_coupon_log(interaction.guild, "PROMOTION CREATED", f"{interaction.user.mention}님이 프로모션을 생성했습니다.", 코드=f"`{doc['code']}`", 이름=doc["name"], 초대_코드=f"`{doc['invite_code']}`", 할인=f"{doc['discount']}%")
        await self.refresh_admin_message(interaction)

    async def delete_code(self, interaction, code):
        await interaction.response.defer(ephemeral=True); ok = await self.repos.coupons.deactivate(interaction.guild.id, code)
        await interaction.followup.send(embed=success_embed("삭제 완료", f"`{normalize_code(code)}`") if ok else error_embed("코드 없음", "코드를 찾을 수 없습니다."), ephemeral=True)
        if ok:
            await self.send_coupon_log(interaction.guild, "COUPON DELETED", f"{interaction.user.mention}님이 코드를 비활성화했습니다.", 코드=f"`{normalize_code(code)}`")
        await self.refresh_admin_message(interaction)

    async def refresh_admin_message(self, interaction):
        coupons, promos = await self.repos.coupons.list_definitions(interaction.guild.id)
        if interaction.message:
            await interaction.message.edit(view=CouponAdminView(self, coupons, promos), attachments=[])

    async def select_coupon(self, interaction, context, code, channel_id=None):
        await interaction.response.defer(ephemeral=True)
        await self.repos.coupons.select(interaction.guild.id, interaction.user.id, context, code, channel_id=channel_id)
        await interaction.followup.send(embed=success_embed("쿠폰 선택 완료", f"선택: `{code}`" if code else "쿠폰을 사용하지 않습니다."), ephemeral=True)
        if context.startswith("ticket:") and interaction.channel:
            await interaction.channel.send(embed=success_embed("티켓 쿠폰 변경", f"{interaction.user.mention}님이 " + (f"`{code}` 쿠폰을 선택했습니다. 셀러가 확인할 수 있습니다." if code else "쿠폰 적용을 해제했습니다.")))

    async def show_selector(self, interaction, context, kind, channel_id=None):
        items = await self.repos.coupons.list_for_user(interaction.guild.id, interaction.user.id, kind)
        if not items:
            await interaction.response.send_message(embed=error_embed("보유 쿠폰 없음", "사용 가능한 쿠폰이 없습니다."), ephemeral=True); return
        await interaction.response.send_message(view=CouponSelectView(self, items, context, channel_id), files=branded_files(), ephemeral=True)

    @app_commands.command(name="쿠폰관리패널", description="쿠폰 관리 패널을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def panel(self, interaction):
        await interaction.response.defer(ephemeral=True); coupons, promos = await self.repos.coupons.list_definitions(interaction.guild.id)
        await interaction.channel.send(view=CouponAdminView(self, coupons, promos), files=branded_files())
        await interaction.followup.send(embed=success_embed("쿠폰 관리 패널 생성 완료"), ephemeral=True)

    @app_commands.command(name="쿠폰지급", description="유저에게 쿠폰을 지급합니다.")
    @app_commands.default_permissions(administrator=True)
    async def grant(self, interaction, 유저: discord.Member, 코드: str, 수량: app_commands.Range[int, 1, 100] = 1):
        await interaction.response.defer(ephemeral=True); doc = await self.repos.coupons.grant(interaction.guild.id, 유저.id, 코드, 수량, interaction.user.id)
        await interaction.followup.send(embed=success_embed("쿠폰 지급 완료", f"{유저.mention} · `{normalize_code(코드)}` {수량}장") if doc else error_embed("지급 실패", "활성 쿠폰 코드를 확인해주세요."), ephemeral=True)
        if doc:
            await self.send_coupon_log(interaction.guild, "COUPON GRANTED", f"{interaction.user.mention}님이 쿠폰을 지급했습니다.", 대상=f"{유저.mention} (`{유저.id}`)", 코드=f"`{normalize_code(코드)}`", 수량=f"{수량}장")

    @app_commands.command(name="쿠폰생성", description="일반 또는 특별 쿠폰을 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(종류=[app_commands.Choice(name="일반 (자판기)", value="general"), app_commands.Choice(name="특별 (티켓)", value="special")])
    @app_commands.choices(할인방식=[app_commands.Choice(name="퍼센트 할인", value="percent"), app_commands.Choice(name="고정 금액 할인 (특별 쿠폰)", value="fixed")])
    async def create_command(self, interaction, 코드: str, 이름: str, 종류: app_commands.Choice[str], 할인방식: app_commands.Choice[str], 할인값: app_commands.Range[int, 1, 1000000000]):
        await interaction.response.defer(ephemeral=True)
        try: doc = await self.repos.coupons.create_coupon(interaction.guild.id, 코드, 이름, 종류.value, 할인값, interaction.user.id, 할인방식.value)
        except ValueError:
            await interaction.followup.send(embed=error_embed("입력 오류", "퍼센트는 1~100, 고정 금액은 특별 쿠폰에서만 사용할 수 있습니다."), ephemeral=True); return
        value = f"{doc['discount']:,}원" if doc.get("discount_type") == "fixed" else f"{doc['discount']}%"
        await interaction.followup.send(embed=success_embed("쿠폰 생성 완료", f"`{doc['code']}` · {value}"), ephemeral=True)
        await self.send_coupon_log(interaction.guild, "COUPON CREATED", f"{interaction.user.mention}님이 쿠폰을 생성했습니다.", 코드=f"`{doc['code']}`", 이름=doc["name"], 할인=value)

    @app_commands.command(name="프로모션생성", description="초대 링크 전용 자판기 프로모션 코드를 생성합니다.")
    @app_commands.default_permissions(administrator=True)
    async def promotion_command(self, interaction, 코드: str, 이름: str, 초대링크: str, 할인율: app_commands.Range[int, 1, 100]):
        await interaction.response.defer(ephemeral=True); parsed = invite_code(초대링크)
        try: doc = await self.repos.coupons.create_promotion(interaction.guild.id, 코드, 이름, parsed, 초대링크, 할인율, interaction.user.id)
        except ValueError:
            await interaction.followup.send(embed=error_embed("입력 오류", "초대 링크와 할인율을 확인해주세요."), ephemeral=True); return
        await interaction.followup.send(embed=success_embed("프로모션 생성 완료", f"`{doc['code']}` · 초대 `{doc['invite_code']}`"), ephemeral=True)
        await self.send_coupon_log(interaction.guild, "PROMOTION CREATED", f"{interaction.user.mention}님이 프로모션을 생성했습니다.", 코드=f"`{doc['code']}`", 이름=doc["name"], 초대_코드=f"`{doc['invite_code']}`", 할인=f"{doc['discount']}%")

    @app_commands.command(name="쿠폰삭제", description="쿠폰 또는 프로모션 코드를 삭제합니다.")
    @app_commands.default_permissions(administrator=True)
    async def remove(self, interaction, 코드: str): await self.delete_code(interaction, 코드)

    @app_commands.command(name="쿠폰목록", description="보유 쿠폰을 확인합니다.")
    async def inventory(self, interaction):
        items = await self.repos.coupons.list_for_user(interaction.guild.id, interaction.user.id)
        text = "\n".join(f"`{x['code']}` · {x['coupon']['name']} · " + (f"{x['coupon']['discount']:,}원" if x['coupon'].get('discount_type') == 'fixed' else f"{x['coupon']['discount']}%") + f" · {x['quantity']}장" for x in items) or "보유 쿠폰이 없습니다."
        await interaction.response.send_message(embed=success_embed("보유 쿠폰", text), ephemeral=True)

    @app_commands.command(name="쿠폰이벤트", description="랜덤 인원에게 쿠폰을 배포합니다.")
    @app_commands.default_permissions(administrator=True)
    async def event(self, interaction, 코드: str, 당첨인원: app_commands.Range[int, 1, 100], 인당수량: app_commands.Range[int, 1, 20] = 1):
        await interaction.response.defer(ephemeral=True)
        pool = [m for m in interaction.guild.members if not m.bot]
        winners = random.sample(pool, min(당첨인원, len(pool)))
        granted = []
        for member in winners:
            if await self.repos.coupons.grant(interaction.guild.id, member.id, 코드, 인당수량, interaction.user.id): granted.append(member)
        if not granted: await interaction.followup.send(embed=error_embed("이벤트 실패", "쿠폰 코드 또는 참여 인원을 확인해주세요."), ephemeral=True); return
        await interaction.channel.send(embed=success_embed("COUPON EVENT", f"당첨: {' '.join(x.mention for x in granted)}\n`{normalize_code(코드)}` 쿠폰 {인당수량}장씩 지급"))
        await self.send_coupon_log(interaction.guild, "COUPON EVENT", f"{interaction.user.mention}님이 랜덤 쿠폰 이벤트를 실행했습니다.", 코드=f"`{normalize_code(코드)}`", 지급_수량=f"{인당수량}장 × {len(granted)}명", 당첨자="\n".join(f"{x.mention} (`{x.id}`)" for x in granted))
        await interaction.followup.send(embed=success_embed("이벤트 배포 완료", f"{len(granted)}명"), ephemeral=True)


async def setup(bot): await bot.add_cog(CouponCog(bot))
