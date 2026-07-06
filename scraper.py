import asyncio
import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


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

    @property
    def identity(self) -> str:
        if self.url:
            return self.url.rstrip("/").casefold()
        return re.sub(r"\s+", "-", self.name.strip().casefold())


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
    def __init__(self, base_url: str, enable_browser_fallback: bool = False) -> None:
        self.base_url = base_url
        self.enable_browser_fallback = enable_browser_fallback
        logger.info("WZStats scraper initialized with browser fallback disabled for meta list")

    async def fetch_meta_weapons(self) -> list[Weapon]:
        async with async_playwright() as playwright:
            html_weapons = await self._fetch_from_html(playwright)
            if html_weapons:
                return html_weapons

            logger.warning("No weapons extracted from WZStats HTML")
            return []

    async def _fetch_from_html(self, playwright: Any) -> list[Weapon]:
        request = await playwright.request.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        )
        try:
            response = await request.get(self.base_url, timeout=30_000)
            if not response.ok:
                logger.info("WZStats HTML request returned HTTP %s", response.status)
                return []

            html = await response.text()
            weapons = self._extract_from_html(html)
            if weapons:
                logger.info("Extracted %s weapons from WZStats HTML", len(weapons))
            return weapons
        except Exception:
            logger.info("Unable to extract WZStats HTML with Playwright request")
            return []
        finally:
            await request.dispose()

    def _extract_from_html(self, html: str) -> list[Weapon]:
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
            if not url or url.rstrip("/").casefold() in seen_links:
                continue

            window = parser.tokens[index + 1 : index + 18]
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
            seen_links.add(url.rstrip("/").casefold())

        return self._normalize_weapons(raw_weapons)

    async def enrich_weapon(self, weapon: Weapon) -> Weapon:
        async with async_playwright() as playwright:
            weapon.attachments = await self._fetch_attachments_from_html(playwright, weapon.url)
            if weapon.attachments or not self.enable_browser_fallback:
                return weapon

            browser = await playwright.chromium.launch(
                headless=True,
                args=self._browser_args(),
            )
            try:
                page = await self._new_page(browser, width=1280, height=900)
                try:
                    await page.goto(weapon.url, wait_until="domcontentloaded", timeout=30_000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except (PlaywrightTimeoutError, PlaywrightError):
                        logger.info("Detail page did not reach network idle for %s", weapon.name)
                    weapon.attachments = await self._extract_attachments(page)
                finally:
                    await page.context.close()
            finally:
                await browser.close()

        return weapon

    async def _fetch_attachments_from_html(self, playwright: Any, url: str) -> list[str]:
        request = await playwright.request.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        )
        try:
            response = await request.get(url, timeout=30_000)
            if not response.ok:
                return []
            html = await response.text()
            return self._extract_attachments_from_html(html)
        except Exception:
            logger.info("Unable to extract weapon details from WZStats HTML")
            return []
        finally:
            await request.dispose()

    def _extract_attachments_from_html(self, html: str) -> list[str]:
        parser = WZStatsHtmlParser()
        parser.feed(html)

        labels = [
            "bouche",
            "canon",
            "laser",
            "lunette",
            "crosse",
            "chargeur",
            "munitions",
            "poignée",
            "accessoire",
            "muzzle",
            "barrel",
            "optic",
            "stock",
            "magazine",
            "ammunition",
            "underbarrel",
            "rear grip",
        ]
        found: list[str] = []

        for token in parser.tokens:
            if token.kind not in {"text", "link"}:
                continue
            text = self._clean_text(token.value)
            if len(text) < 3 or len(text) > 90:
                continue
            if not any(label in text.casefold() for label in labels):
                continue
            if text not in found:
                found.append(text)

        return found[:10]

    def _browser_args(self) -> list[str]:
        return [
            "--disable-background-networking",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-sync",
            "--disable-web-security",
            "--js-flags=--max-old-space-size=128",
            "--no-first-run",
            "--no-sandbox",
        ]

    async def _new_page(self, browser: Browser, width: int, height: int) -> Page:
        context = await browser.new_context(
            viewport={"width": width, "height": height},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        async def block_heavy_resources(route):
            if route.request.resource_type in {"font", "image", "media"}:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", block_heavy_resources)
        return await context.new_page()

    async def _wait_for_dynamic_content(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            logger.info("Network idle timeout reached; continuing with visible content")
        except PlaywrightError:
            logger.info("Network idle wait failed; continuing with loaded DOM")

        candidates = [
            "a[href*='loadout']",
            "a[href*='weapon']",
            "a[href*='arme']",
            "img",
            "text=/META|Meta|meta/",
        ]

        for selector in candidates:
            try:
                await page.wait_for_selector(selector, timeout=8_000)
                return
            except (PlaywrightTimeoutError, PlaywrightError):
                continue

    async def _extract_from_page(self, page: Page) -> list[dict[str, Any]]:
        return await page.evaluate(
            """
            () => {
              const absoluteUrl = (value) => {
                if (!value) return "";
                try { return new URL(value, window.location.href).toString(); }
                catch { return ""; }
              };

              const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const tierMatch = (text) => {
                const value = clean(text);
                if (/^Warzone Meta$/i.test(value)) return "META";
                const match = value.match(/\\b(S\\+?|A\\+?|B\\+?|C\\+?|D\\+?)\\s*Tier\\b/i)
                  || value.match(/(?:Tier|Niveau|Rang)\\s*[:\\-]?\\s*(S\\+?|A\\+?|B\\+?|C\\+?|D\\+?)/i);
                if (match) return match[1].toUpperCase();
                return "";
              };

              const typeWords = [
                "Fusil d'assaut", "Mitraillette", "Fusil de précision", "Fusil tactique",
                "Fusil de combat", "Fusil à pompe", "Mitrailleuse", "Pistolet",
                "Assault Rifle", "SMG", "Sniper Rifle", "Marksman Rifle", "Battle Rifle",
                "Shotgun", "LMG", "Handgun", "Melee"
              ];
              const findType = (text) => typeWords.find((word) => clean(text).toLowerCase().includes(word.toLowerCase())) || "";

              const headings = [...document.querySelectorAll("h1, h2, h3")].filter((heading) => {
                const text = clean(heading.innerText || heading.textContent || "");
                return /^Warzone Meta$/i.test(text) || /\\b(S\\+?|A\\+?|B\\+?|C\\+?|D\\+?)\\s*Tier\\b/i.test(text);
              });

              const tierFor = (element) => {
                let tier = "";
                for (const heading of headings) {
                  const position = heading.compareDocumentPosition(element);
                  if (position & Node.DOCUMENT_POSITION_FOLLOWING) {
                    tier = tierMatch(heading.innerText || heading.textContent || "");
                  }
                }
                return tier;
              };

              const bestContainerFor = (anchor) => {
                let node = anchor;
                let best = anchor;
                for (let depth = 0; node && depth < 8; depth += 1) {
                  const text = clean(node.innerText || node.textContent || "");
                  const hasImage = Boolean(node.querySelector?.("img"));
                  const hasRank = /#\\d+\\s+/.test(text);
                  const hasType = Boolean(findType(text));
                  if (hasImage && (hasRank || hasType) && text.length < 900) best = node;
                  node = node.parentElement;
                }
                return best;
              };

              const nameFromLines = (lines, imageAlt) => {
                const blacklist = /^(mise à jour|new|nouveau|###)$/i;
                const candidates = lines.filter((line) => (
                  line.length > 1 &&
                  line.length < 48 &&
                  !blacklist.test(line) &&
                  !/^#\\d+/.test(line) &&
                  !/meilleures configurations/i.test(line) &&
                  !findType(line)
                ));
                const fromText = candidates.find((line) => /[A-Za-zÀ-ÿ0-9]/.test(line));
                if (fromText) return fromText;
                if (imageAlt) {
                  return imageAlt
                    .split("-")
                    .filter(Boolean)
                    .map((part) => part.length <= 3 ? part.toUpperCase() : part[0].toUpperCase() + part.slice(1))
                    .join(" ");
                }
                return "";
              };

              const cards = [];
              const anchors = [...document.querySelectorAll("a[href]")];
              for (const anchor of anchors) {
                const href = anchor.getAttribute("href") || "";
                const surrounding = bestContainerFor(anchor);
                const text = clean(surrounding.innerText || anchor.innerText || "");
                const image = surrounding.querySelector("img") || anchor.querySelector("img");
                const imageUrl = absoluteUrl(image?.currentSrc || image?.src || image?.getAttribute("src") || "");
                const nameFromImage = clean(image?.alt || "");
                const lines = text.split("\\n").map(clean).filter(Boolean);
                const name = nameFromLines(lines, nameFromImage);
                const url = absoluteUrl(href);
                const tier = tierMatch(text) || tierFor(surrounding);
                const weaponType = findType(text);

                if (!name || !url) continue;
                if (!imageUrl && !tier && !weaponType) continue;
                if (/(discord|twitter|x\\.com|youtube|privacy|login|connexion)/i.test(url)) continue;

                cards.push({ name, tier, weaponType, imageUrl, url, text });
              }

              return cards;
            }
            """
        )

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

            weapon = Weapon(
                name=name,
                tier=tier,
                weapon_type=self._clean_text(str(item.get("weaponType", ""))),
                image_url=urljoin(self.base_url, str(item.get("imageUrl", ""))),
                url=url,
                rank=len(weapons) + 1,
            )

            seen.add(identity)
            weapons.append(weapon)

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
        type_words = [
            "Fusil d'Assaut",
            "Fusil d'assaut",
            "Mitraillette",
            "Fusil de précision",
            "Fusil tactique",
            "Fusil de combat",
            "Fusil à pompe",
            "Mitrailleuse",
            "Pistolet",
            "Marksman",
            "Spécial",
            "Assault Rifle",
            "SMG",
            "Sniper Rifle",
            "Marksman Rifle",
            "Battle Rifle",
            "Shotgun",
            "LMG",
            "Handgun",
            "Melee",
        ]
        normalized = value.casefold()
        return next((word for word in type_words if word.casefold() in normalized), "")

    def _name_from_slug(self, value: str) -> str:
        slug = self._clean_text(value).strip("/").split("/")[-1]
        slug = re.sub(r"\.(png|jpg|jpeg|webp|avif)$", "", slug, flags=re.IGNORECASE)
        parts = [part for part in re.split(r"[-_]+", slug) if part]
        return " ".join(part.upper() if len(part) <= 3 else part.capitalize() for part in parts)

    async def _enrich_weapon_details(self, browser: Browser, weapons: list[Weapon]) -> None:
        for weapon in weapons:
            try:
                await self._enrich_weapon_with_browser(browser, weapon)
            except Exception:
                logger.info("Unable to enrich weapon details for %s", weapon.name, exc_info=True)
            await asyncio.sleep(0.2)

    async def _enrich_weapon_with_browser(self, browser: Browser, weapon: Weapon) -> None:
        page = await self._new_page(browser, width=1280, height=900)
        try:
            await page.goto(weapon.url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except (PlaywrightTimeoutError, PlaywrightError):
                logger.info("Detail page did not reach network idle for %s", weapon.name)
            weapon.attachments = await self._extract_attachments(page)
        finally:
            await page.context.close()

    async def _extract_attachments(self, page: Page) -> list[str]:
        values = await page.evaluate(
            """
            () => {
              const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const labels = [
                "bouche", "canon", "laser", "lunette", "crosse", "chargeur",
                "munitions", "poignée", "accessoire", "muzzle", "barrel", "optic",
                "stock", "magazine", "ammunition", "underbarrel", "rear grip"
              ];
              const blocks = [...document.querySelectorAll("li, article, section, div, span, p")];
              const found = [];
              for (const block of blocks) {
                const text = clean(block.innerText || block.textContent || "");
                if (text.length < 3 || text.length > 90) continue;
                if (!labels.some((label) => text.toLowerCase().includes(label))) continue;
                if (!found.includes(text)) found.push(text);
              }
              return found.slice(0, 10);
            }
            """
        )
        return [self._clean_text(value) for value in values if self._clean_text(value)]

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
