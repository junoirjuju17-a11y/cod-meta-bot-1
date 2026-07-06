import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from config import settings
from database import Database
from scraper import Weapon, WZStatsScraper


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)

logger = logging.getLogger("cod-meta-bot")


class CodMetaBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = Database(settings.database_path)
        self.scraper = WZStatsScraper(
            settings.wzstats_url,
            enable_browser_fallback=settings.enable_browser_fallback,
        )
        self._latest_weapons: list[Weapon] = []
        self._startup_complete = asyncio.Event()

    async def setup_hook(self) -> None:
        self.db.initialize()
        register_commands(self)

        if settings.guild_id:
            guild = discord.Object(id=settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synchronized for guild %s", settings.guild_id)
        else:
            await self.tree.sync()
            logger.info("Slash commands synchronized globally")

        self.check_meta_weapons.start()

    async def on_ready(self) -> None:
        logger.info("Connected as %s (%s)", self.user, self.user.id if self.user else "unknown")
        self._startup_complete.set()

    async def close(self) -> None:
        self.check_meta_weapons.cancel()
        self.db.close()
        await super().close()

    async def fetch_current_weapons(self, force_refresh: bool = False) -> list[Weapon]:
        if self._latest_weapons and not force_refresh:
            return self._latest_weapons

        try:
            weapons = await self.scraper.fetch_meta_weapons()
        except Exception:
            logger.warning("Unable to refresh WZStats data; using cache when available")
            if self._latest_weapons:
                return self._latest_weapons
            return self.db.get_weapons()

        self._latest_weapons = weapons
        self.db.upsert_weapons(weapons)
        return weapons

    @tasks.loop(minutes=settings.check_interval_minutes)
    async def check_meta_weapons(self) -> None:
        await self._startup_complete.wait()
        channel = self.get_channel(settings.channel_id)

        if channel is None:
            try:
                channel = await self.fetch_channel(settings.channel_id)
            except discord.DiscordException:
                logger.exception("Unable to fetch Discord channel %s", settings.channel_id)
                return

        if not hasattr(channel, "send"):
            logger.error("Configured channel %s cannot receive messages", settings.channel_id)
            return

        try:
            weapons = await self.fetch_current_weapons(force_refresh=True)
        except Exception:
            logger.exception("WZStats check failed; bot will retry on the next cycle")
            return

        new_weapons = [weapon for weapon in weapons if not self.db.was_published(weapon.identity)]
        if not new_weapons:
            logger.info("No new META weapons found")
            return

        for weapon in new_weapons:
            try:
                await channel.send(embed=build_weapon_embed(weapon, title_prefix="Nouvelle arme META"))
                self.db.mark_published(weapon)
                logger.info("Published new META weapon: %s", weapon.name)
            except discord.DiscordException:
                logger.exception("Unable to publish weapon %s", weapon.name)

    @check_meta_weapons.before_loop
    async def before_check_meta_weapons(self) -> None:
        await self.wait_until_ready()


def build_weapon_embed(weapon: Weapon, title_prefix: Optional[str] = None) -> discord.Embed:
    title = f"{title_prefix} : {weapon.name}" if title_prefix else weapon.name
    embed = discord.Embed(
        title=title,
        url=weapon.url,
        color=discord.Color.gold() if weapon.tier.upper() in {"S", "S+"} else discord.Color.blue(),
    )
    embed.add_field(name="Tier", value=weapon.tier or "Inconnu", inline=True)
    embed.add_field(name="Type", value=weapon.weapon_type or "Inconnu", inline=True)
    embed.add_field(name="WZStats", value=f"[Voir la page]({weapon.url})", inline=False)

    if weapon.attachments:
        embed.add_field(name="Accessoires", value="\n".join(weapon.attachments[:10]), inline=False)

    if weapon.image_url:
        embed.set_image(url=weapon.image_url)

    embed.set_footer(text="Source : WZStats")
    return embed


def format_weapon_line(index: int, weapon: Weapon) -> str:
    return f"**{index}. {weapon.name}** | Tier {weapon.tier or '?'} | {weapon.weapon_type or 'Type inconnu'}"


def register_commands(bot: CodMetaBot) -> None:
    @bot.tree.command(name="meta", description="Affiche la liste actuelle des armes META.")
    async def meta(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        weapons = await bot.fetch_current_weapons(force_refresh=True)
        if not weapons:
            await interaction.followup.send("WZStats est momentanément inaccessible et aucun cache local n'est encore disponible.")
            return

        lines = [format_weapon_line(index, weapon) for index, weapon in enumerate(weapons, start=1)]
        embed = discord.Embed(title="Armes META actuelles", description="\n".join(lines[:25]))
        embed.set_footer(text="Source : WZStats")
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="top10", description="Affiche les 10 meilleures armes META.")
    async def top10(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        weapons = await bot.fetch_current_weapons(force_refresh=True)
        top_weapons = weapons[:10]
        if not top_weapons:
            await interaction.followup.send("WZStats est momentanément inaccessible et aucun cache local n'est encore disponible.")
            return

        lines = [format_weapon_line(index, weapon) for index, weapon in enumerate(top_weapons, start=1)]
        embed = discord.Embed(title="Top 10 WZStats", description="\n".join(lines))
        embed.set_footer(text="Source : WZStats")
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="arme", description="Affiche les détails d'une arme META.")
    @app_commands.describe(nom="Nom de l'arme à rechercher")
    async def arme(interaction: discord.Interaction, nom: str) -> None:
        await interaction.response.defer(thinking=True)
        weapons = await bot.fetch_current_weapons(force_refresh=True)
        if not weapons:
            await interaction.followup.send("WZStats est momentanément inaccessible et aucun cache local n'est encore disponible.")
            return

        normalized_query = nom.casefold().strip()

        weapon = next((item for item in weapons if item.name.casefold() == normalized_query), None)
        if weapon is None:
            weapon = next((item for item in weapons if normalized_query in item.name.casefold()), None)

        if weapon is None:
            await interaction.followup.send(f"Je n'ai pas trouvé d'arme correspondant à `{nom}`.")
            return

        if not weapon.attachments:
            try:
                weapon = await bot.scraper.enrich_weapon(weapon)
                bot.db.upsert_weapons([weapon])
            except Exception:
                logger.info("Unable to enrich weapon requested by /arme: %s", weapon.name, exc_info=True)

        await interaction.followup.send(embed=build_weapon_embed(weapon))


async def main() -> None:
    bot = CodMetaBot()
    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
