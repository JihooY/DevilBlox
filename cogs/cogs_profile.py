from __future__ import annotations

from discord.ext import commands

from cogs.profile_views import build_profile_view, profile_send_kwargs, send_profile


class ProfileCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="profile", description="기본 언어와 공지·티켓 상태·입고 알림을 설정합니다.")
    @commands.guild_only()
    async def profile(self, ctx: commands.Context):
        if ctx.interaction is not None:
            await send_profile(ctx.interaction, self.bot.repos)
            return
        view = await build_profile_view(self.bot.repos, ctx.guild, ctx.author)
        view.message = await ctx.send(**profile_send_kwargs(view))


async def setup(bot):
    await bot.add_cog(ProfileCog(bot))
