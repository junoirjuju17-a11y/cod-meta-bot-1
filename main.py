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
        self.scraper = WZStatsScraper(settings.wzstats_url)
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
            return self.db.get_weapons(limit=5)

        if not weapons:
            logger.warning("WZStats returned no Top 5 weapons; using cache when available")
            if self._latest_weapons:
                return self._latest_weapons
            return self.db.get_weapons(limit=5)

        top5_weapons = weapons[:5]
        self._latest_weapons = top5_weapons
        self.db.upsert_weapons(top5_weapons)
        return top5_weapons

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

        previous_top5 = set(self.db.get_current_top5_identities())
        current_top5 = {weapon.identity for weapon in weapons[:5]}
        entered_top5 = current_top5 - previous_top5
        left_top5 = previous_top5 - current_top5

        publishable_weapons = [
            weapon
            for weapon in weapons[:5]
            if self._should_publish_weapon_update(weapon, entered_top5)
        ]

        if not publishable_weapons and not entered_top5 and not left_top5:
            self.db.replace_latest_builds(weapons[:5])
            self.db.replace_current_top5(weapons[:5])
            logger.info("No Top 5 META or build change found")
            return

        if left_top5:
            logger.info("%s weapon(s) left the Top 5 META", len(left_top5))

        for weapon in publishable_weapons:
            try:
                await channel.send(embed=build_weapon_embed(weapon, title_prefix="🔥 NOUVELLE META WARZONE"))
                self.db.mark_published(weapon)
                self.db.mark_build_published(weapon)
                logger.info("Published Top 5 META update: %s", weapon.name)
            except discord.DiscordException:
                logger.exception("Unable to publish weapon %s", weapon.name)

        self.db.replace_latest_builds(weapons[:5])
        self.db.replace_current_top5(weapons[:5])

    @check_meta_weapons.before_loop
    async def before_check_meta_weapons(self) -> None:
        await self.wait_until_ready()

    def _should_publish_weapon_update(self, weapon: Weapon, entered_top5: set[str]) -> bool:
        is_new_top5_weapon = weapon.identity in entered_top5 and not self.db.was_published(weapon.identity)
        previous_build_signature = self.db.get_latest_build_signature(weapon.identity)
        build_changed = (
            previous_build_signature is not None
            and previous_build_signature != weapon.build_signature
            and not self.db.was_build_published(weapon.build_signature)
        )
        return is_new_top5_weapon or build_changed


def build_weapon_embed(weapon: Weapon, title_prefix: Optional[str] = None) -> discord.Embed:
    title = title_prefix or weapon.name
    embed = discord.Embed(
        title=title,
        url=weapon.url,
        color=discord.Color.gold() if weapon.tier.upper() in {"META", "S", "S+"} else discord.Color.blue(),
    )
    embed.description = "━━━━━━━━━━━━━━━━━━━━━━"
    embed.add_field(name="🔫 Arme :", value=weapon.name, inline=False)
    embed.add_field(name="⭐ Tier :", value=weapon.tier or "Inconnu", inline=True)
    embed.add_field(name="📂 Type :", value=weapon.weapon_type or "Inconnu", inline=True)
    embed.add_field(name="🔧 Build META", value=format_build(weapon), inline=False)
    embed.add_field(name="📊 Source :", value=weapon.url or settings.wzstats_url, inline=False)

    if weapon.image_url:
        embed.set_image(url=weapon.image_url)

    embed.set_footer(text="━━━━━━━━━━━━━━━━━━━━━━")
    return embed


def format_build(weapon: Weapon) -> str:
    if not weapon.build:
        return "Build non disponible pour le moment."

    preferred_order = [
        "Bouche",
        "Canon",
        "Lunette",
        "Sous-canon",
        "Chargeur",
        "Poignée arrière",
        "Poignée",
        "Crosse",
        "Laser",
        "Conversion",
        "Munitions",
        "Accessoire",
    ]
    lines: list[str] = []
    used: set[str] = set()

    for label in preferred_order:
        value = weapon.build.get(label)
        if value:
            lines.append(f"• {label} :\n{value}")
            used.add(label)

    for label, value in weapon.build.items():
        if label in used:
            continue
        lines.append(f"• {label} :\n{value}")

    return "\n\n".join(lines[:15])


def format_weapon_line(index: int, weapon: Weapon) -> str:
    return f"**{index}. {weapon.name}** | Tier {weapon.tier or '?'} | {weapon.weapon_type or 'Type inconnu'}"


def register_commands(bot: CodMetaBot) -> None:
    @bot.tree.command(name="meta", description="Affiche les 5 armes META les plus fortes.")
    async def meta(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        weapons = await bot.fetch_current_weapons(force_refresh=True)
        if not weapons:
            await interaction.followup.send("WZStats est momentanément inaccessible et aucun cache local n'est encore disponible.")
            return

        lines = [format_weapon_line(index, weapon) for index, weapon in enumerate(weapons, start=1)]
        embed = discord.Embed(title="Top 5 META WZStats", description="\n".join(lines[:5]))
        embed.set_footer(text="Source : WZStats")
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="top5", description="Affiche les 5 meilleures armes META.")
    async def top5(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        weapons = await bot.fetch_current_weapons(force_refresh=True)
        top_weapons = weapons[:5]
        if not top_weapons:
            await interaction.followup.send("WZStats est momentanément inaccessible et aucun cache local n'est encore disponible.")
            return

        lines = [format_weapon_line(index, weapon) for index, weapon in enumerate(top_weapons, start=1)]
        embed = discord.Embed(title="Top 5 WZStats", description="\n".join(lines))
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
