from .modlog import VantageModlog


async def setup(bot):
    await bot.add_cog(VantageModlog(bot))
