import logging
import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import async_playwright


logger = logging.getLogger("cod-meta-bot.scraper")


@dataclass(slots=True)
class Weapon:
    name: str
    tier: str
    weapon_type: str
    image_url: str
    url: str
    rank: int
    attachments: list[str] = field(default_factory=list)
    build: dict[str, str] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        if self.url:
            return self.url.rstrip("/").casefold()
        return re.sub(r"\s+", "-", self.name.strip().casefold())

    @property
    def build_signature(self) -> str:
        payload = {
            "identity": self.identity,
            "build": {key: self.build[key] for key in sorted(self.build)},
        }
        value = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class HtmlToken:
    kind: str
    value: str
    href: str = ""
    src: str = ""
    alt: str = ""


class WZStatsHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[HtmlToken] = []
        self._anchor_stack: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}

        if tag.lower() == "a":
            self._anchor_stack.append(
                {
                    "href": values.get("href", ""),
                    "text": values.get("aria-label", "") or values.get("title", ""),
                }
            )
            return

        if tag.lower() == "img":
            self.tokens.append(
                HtmlToken(
                    kind="image",
                    value=values.get("alt", ""),
                    src=values.get("src", "") or values.get("data-src", ""),
                    alt=values.get("alt", ""),
                )
            )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._anchor_stack:
            anchor = self._anchor_stack.pop()
            text = self._clean(anchor.get("text", ""))
            if text and anchor.get("href"):
                self.tokens.append(HtmlToken(kind="link", value=text, href=anchor["href"]))

    def handle_data(self, data: str) -> None:
        text = self._clean(data)
        if not text:
            return

        if self._anchor_stack:
            current = self._anchor_stack[-1]
            current["text"] = self._clean(f"{current.get('text', '')} {text}")
            return

        self.tokens.append(HtmlToken(kind="text", value=text))

    def _clean(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()


class WZStatsScraper:
    TOP_META_LIMIT = 5

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        logger.info("WZStats scraper initialized in request-only Playwright mode")

    async def fetch_meta_weapons(self) -> list[Weapon]:
        async with async_playwright() as playwright:
            weapons = await self._fetch_weapons_from_html(playwright)
            if not weapons:
                logger.warning("No weapons extracted from WZStats HTML")
                return []

            await self._enrich_weapons_with_builds(playwright, weapons)
            return weapons[: self.TOP_META_LIMIT]

    async def enrich_weapon(self, weapon: Weapon) -> Weapon:
        async with async_playwright() as playwright:
            weapon.build = await self._fetch_build_from_html(playwright, weapon.url)
            weapon.attachments = self._build_to_attachment_lines(weapon.build)
        return weapon

    async def _enrich_weapons_with_builds(self, playwright: Any, weapons: list[Weapon]) -> None:
        for weapon in weapons[: self.TOP_META_LIMIT]:
            weapon.build = await self._fetch_build_from_html(playwright, weapon.url)
            weapon.attachments = self._build_to_attachment_lines(weapon.build)

    async def _fetch_weapons_from_html(self, playwright: Any) -> list[Weapon]:
        request = await self._new_request_context(playwright)
        try:
            response = await request.get(self.base_url, timeout=30_000)
            if not response.ok:
                logger.info("WZStats HTML request returned HTTP %s", response.status)
                return []

            html = await response.text()
            weapons = self._extract_weapons_from_html(html)
            if weapons:
                logger.info("Extracted %s weapons from WZStats HTML", len(weapons))
            return weapons[: self.TOP_META_LIMIT]
        except Exception:
            logger.info("Unable to extract WZStats HTML with Playwright request")
            return []
        finally:
            await request.dispose()

    async def _fetch_build_from_html(self, playwright: Any, url: str) -> dict[str, str]:
        request = await self._new_request_context(playwright)
        try:
            response = await request.get(url, timeout=30_000)
            if not response.ok:
                return {}

            html = await response.text()
            return self._extract_build_from_html(html)
        except Exception:
            logger.info("Unable to extract weapon details from WZStats HTML")
            return {}
        finally:
            await request.dispose()

    async def _new_request_context(self, playwright: Any) -> Any:
        return await playwright.request.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
            },
        )

    def _extract_weapons_from_html(self, html: str) -> list[Weapon]:
        parser = WZStatsHtmlParser()
        parser.feed(html)

        raw_weapons: list[dict[str, Any]] = []
        seen_links: set[str] = set()
        current_tier = ""

        for index, token in enumerate(parser.tokens):
            if token.kind == "text":
                tier = self._tier_from_heading(token.value)
                if tier:
                    current_tier = tier
                continue

            if token.kind != "link":
                continue

            name = self._weapon_name_from_link(token.value)
            if not name:
                continue

            url = urljoin(self.base_url, token.href).split("#", 1)[0]
            identity = url.rstrip("/").casefold()
            if not url or identity in seen_links:
                continue

            window = parser.tokens[index + 1 : index + 20]
            text_values = [item.value for item in window if item.kind in {"text", "link"}]
            image = next((item for item in window if item.kind == "image" and (item.src or item.alt)), None)
            displayed_name = self._best_name_from_html_window(name, text_values, image.alt if image else "")

            raw_weapons.append(
                {
                    "name": displayed_name or name,
                    "tier": current_tier,
                    "weaponType": self._find_weapon_type(" ".join(text_values)),
                    "imageUrl": image.src if image else "",
                    "url": url,
                }
            )
            seen_links.add(identity)

            if len(raw_weapons) >= self.TOP_META_LIMIT:
                break

        return self._normalize_weapons(raw_weapons)

    def _extract_build_from_html(self, html: str) -> dict[str, str]:
        parser = WZStatsHtmlParser()
        parser.feed(html)

        values = [self._clean_text(token.value) for token in parser.tokens if token.kind in {"text", "link"}]
        values = [value for value in values if value]
        build: dict[str, str] = {}

        for index, value in enumerate(values):
            label = self._canonical_attachment_label(value)
            if not label:
                label, inline_value = self._split_inline_attachment(value)
                if label and inline_value:
                    build.setdefault(label, inline_value)
                continue

            inline_value = self._value_after_label(value)
            if inline_value:
                build.setdefault(label, inline_value)
                continue

            next_value = self._next_attachment_value(values, index + 1)
            if next_value:
                build.setdefault(label, next_value)

        return build

    def _build_to_attachment_lines(self, build: dict[str, str]) -> list[str]:
        return [f"{label}: {value}" for label, value in build.items()]

    def _next_attachment_value(self, values: list[str], start_index: int) -> str:
        for value in values[start_index : start_index + 4]:
            if self._canonical_attachment_label(value):
                continue
            if len(value) < 2 or len(value) > 90:
                continue
            if value.startswith("#"):
                continue
            if value.casefold() in {"meta", "warzone meta", "mise à jour", "new", "nouveau"}:
                continue
            return value
        return ""

    def _split_inline_attachment(self, value: str) -> tuple[str, str]:
        match = re.match(r"^([^:：-]{3,35})\s*[:：-]\s*(.{2,90})$", value)
        if not match:
            return "", ""

        label = self._canonical_attachment_label(match.group(1))
        attachment = self._clean_text(match.group(2))
        return label, attachment

    def _value_after_label(self, value: str) -> str:
        for separator in (":", "：", "-"):
            if separator not in value:
                continue
            _, attachment = value.split(separator, 1)
            attachment = self._clean_text(attachment)
            if attachment and not self._canonical_attachment_label(attachment):
                return attachment
        return ""

    def _canonical_attachment_label(self, value: str) -> str:
        normalized = self._normalize_label(value)
        labels = [
            ("Bouche", ("bouche", "muzzle")),
            ("Canon", ("canon", "barrel")),
            ("Lunette", ("lunette", "optic", "scope", "viseur")),
            ("Crosse", ("crosse", "stock")),
            ("Sous-canon", ("sous canon", "sous-canon", "underbarrel")),
            ("Chargeur", ("chargeur", "magazine", "mag")),
            ("Poignée arrière", ("poignee arriere", "poignée arrière", "rear grip", "grip arriere")),
            ("Poignée", ("poignee", "poignée", "grip")),
            ("Laser", ("laser",)),
            ("Conversion", ("conversion", "conversion kit", "accessoire de conversion")),
            ("Munitions", ("munitions", "ammunition", "ammo")),
            ("Accessoire", ("accessoire", "perk", "comb", "bolt", "fire mod")),
        ]

        for label, aliases in labels:
            if any(alias in normalized for alias in aliases):
                return label

        return ""

    def _normalize_label(self, value: str) -> str:
        value = self._clean_text(value).casefold()
        replacements = {
            "é": "e",
            "è": "e",
            "ê": "e",
            "à": "a",
            "â": "a",
            "ù": "u",
            "û": "u",
            "î": "i",
            "ï": "i",
            "ô": "o",
            "ç": "c",
        }
        for source, target in replacements.items():
            value = value.replace(source, target)
        return value

    def _normalize_weapons(self, raw_weapons: list[dict[str, Any]]) -> list[Weapon]:
        weapons: list[Weapon] = []
        seen: set[str] = set()

        for item in raw_weapons:
            name = self._clean_name(str(item.get("name", "")))
            url = urljoin(self.base_url, str(item.get("url", ""))).split("#", 1)[0]
            if not name or not url:
                continue

            identity = url.rstrip("/").casefold()
            if identity in seen:
                continue

            tier = self._clean_tier(str(item.get("tier", "")))
            if tier and tier not in {"META", "S+", "S", "A+", "A", "B+", "B", "C+", "C", "D+", "D"}:
                tier = ""

            weapons.append(
                Weapon(
                    name=name,
                    tier=tier,
                    weapon_type=self._clean_text(str(item.get("weaponType", ""))),
                    image_url=urljoin(self.base_url, str(item.get("imageUrl", ""))),
                    url=url,
                    rank=len(weapons) + 1,
                    build=dict(item.get("build", {})) if isinstance(item.get("build"), dict) else {},
                )
            )
            seen.add(identity)

        return weapons

    def _tier_from_heading(self, value: str) -> str:
        value = self._clean_text(value)
        if value.casefold() == "warzone meta":
            return "META"

        match = re.search(r"\b(S\+?|A\+?|B\+?|C\+?|D\+?)\s*Tier\b", value, flags=re.IGNORECASE)
        return match.group(1).upper() if match else ""

    def _weapon_name_from_link(self, value: str) -> str:
        value = self._clean_text(value)
        patterns = [
            r"Obtenez toutes les meilleures configurations\s+(.+)",
            r"Get all the best\s+(.+?)\s+loadouts",
            r"Best\s+(.+?)\s+loadouts",
        ]

        for pattern in patterns:
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if match:
                return self._clean_name(match.group(1))

        return ""

    def _best_name_from_html_window(self, fallback: str, values: list[str], image_alt: str) -> str:
        blacklist = re.compile(r"^(mise à jour|new|nouveau|###)$", flags=re.IGNORECASE)

        for value in values:
            candidate = self._clean_text(value)
            if len(candidate) < 2 or len(candidate) > 48:
                continue
            if blacklist.match(candidate):
                continue
            if candidate.startswith("#"):
                continue
            if "meilleures configurations" in candidate.casefold():
                continue
            if self._find_weapon_type(candidate):
                continue
            return candidate

        if image_alt:
            return self._name_from_slug(image_alt)

        return fallback

    def _find_weapon_type(self, value: str) -> str:
        normalized = value.casefold()
        type_map = [
            (("fusil d'assaut", "assault rifle"), "AR"),
            (("mitraillette", "smg"), "SMG"),
            (("fusil de précision", "sniper rifle"), "SNIPER"),
            (("fusil tactique", "marksman rifle", "marksman"), "MARKSMAN"),
            (("fusil de combat", "battle rifle"), "BR"),
            (("fusil à pompe", "shotgun"), "SHOTGUN"),
            (("mitrailleuse", "lmg"), "LMG"),
            (("pistolet", "handgun"), "PISTOL"),
            (("spécial", "melee"), "SPECIAL"),
        ]

        for keywords, short_name in type_map:
            if any(keyword in normalized for keyword in keywords):
                return short_name

        return ""

    def _name_from_slug(self, value: str) -> str:
        slug = self._clean_text(value).strip("/").split("/")[-1]
        slug = re.sub(r"\.(png|jpg|jpeg|webp|avif)$", "", slug, flags=re.IGNORECASE)
        parts = [part for part in re.split(r"[-_]+", slug) if part]
        return " ".join(part.upper() if len(part) <= 3 else part.capitalize() for part in parts)

    def _clean_name(self, value: str) -> str:
        value = self._clean_text(value)
        value = re.sub(r"^(meta|tier|rang|niveau)\s+", "", value, flags=re.IGNORECASE)
        return value[:80]

    def _clean_tier(self, value: str) -> str:
        value = self._clean_text(value).upper()
        if value == "META":
            return "META"
        match = re.search(r"\b(S\+?|A\+?|B\+?|C\+?|D\+?)\b", value)
        return match.group(1) if match else ""

    def _clean_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()
