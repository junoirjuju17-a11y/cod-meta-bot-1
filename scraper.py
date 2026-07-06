import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape
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
    range_role: str = "Polyvalente"
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


@dataclass(slots=True)
class HtmlNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["HtmlNode"] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    parent: "HtmlNode | None" = None

    def own_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts)).strip()

    def full_text(self) -> str:
        values = [self.own_text()]
        values.extend(child.full_text() for child in self.children)
        return re.sub(r"\s+", " ", " ".join(value for value in values if value)).strip()


class WZStatsDomParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("document")
        self._stack: list[HtmlNode] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        node = HtmlNode(tag=tag.lower(), attrs=values, parent=self._stack[-1])
        self._stack[-1].children.append(node)

        if tag.lower() not in {"br", "img", "input", "meta", "link"}:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if text:
            self._stack[-1].text_parts.append(text)


class WZStatsScraper:
    SELECTED_RANGE_ROLES = ("Longue portée", "Courte portée")
    SLOT_ALIASES = {
        "Bouche": ("bouche", "muzzle", "muzzle attachment"),
        "Canon": ("canon", "barrel", "barrel attachment"),
        "Lunette": ("lunette", "optic", "scope", "viseur", "optic attachment"),
        "Crosse": ("crosse", "stock", "stock attachment"),
        "Sous-canon": ("sous canon", "sous-canon", "underbarrel", "under barrel", "underbarrel attachment"),
        "Chargeur": ("chargeur", "magazine", "mag", "magazine attachment"),
        "Poignée arrière": ("poignee arriere", "poignée arrière", "rear grip", "grip arriere", "rear grip attachment"),
        "Poignée": ("poignee", "poignée", "grip", "grip attachment"),
        "Laser": ("laser", "laser attachment"),
        "Conversion": ("conversion", "conversion kit", "accessoire de conversion"),
        "Munitions": ("munitions", "ammunition", "ammo", "ammunition attachment"),
        "Accessoire": ("accessoire", "perk", "comb", "bolt", "fire mod", "mod"),
    }

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        logger.info("WZStats scraper initialized in request-only Playwright mode")

    async def fetch_meta_weapons(self) -> list[Weapon]:
        async with async_playwright() as playwright:
            weapons = await self._fetch_weapons_from_html(playwright)
            if not weapons:
                logger.warning("No weapons extracted from WZStats HTML")
                return []

            selected_weapons = self._select_best_range_weapons(weapons)
            await self._enrich_weapons_with_builds(playwright, selected_weapons)
            return selected_weapons

    async def enrich_weapon(self, weapon: Weapon) -> Weapon:
        async with async_playwright() as playwright:
            weapon.build = await self._fetch_build_from_html(playwright, weapon.url)
            weapon.attachments = self._build_to_attachment_lines(weapon.build)
        return weapon

    async def _enrich_weapons_with_builds(self, playwright: Any, weapons: list[Weapon]) -> None:
        for weapon in weapons:
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
            return weapons
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

            window = parser.tokens[index + 1 : index + 35]
            text_values = [item.value for item in window if item.kind in {"text", "link"}]
            image = next((item for item in window if item.kind == "image" and (item.src or item.alt)), None)
            displayed_name = self._best_name_from_html_window(name, text_values, image.alt if image else "")
            weapon_type = self._find_weapon_type(" ".join(text_values))
            range_role, raw_range_text = self._find_range_role_from_sources(
                [token.value, *text_values[:14]],
                displayed_name or name,
            )

            raw_weapons.append(
                {
                    "name": displayed_name or name,
                    "tier": current_tier,
                    "weaponType": weapon_type,
                    "rangeRole": range_role,
                    "rangeRaw": raw_range_text,
                    "imageUrl": image.src if image else "",
                    "url": url,
                }
            )
            seen_links.add(identity)

        return self._normalize_weapons(raw_weapons)

    def _select_best_range_weapons(self, weapons: list[Weapon]) -> list[Weapon]:
        selected: list[Weapon] = []
        used_identities: set[str] = set()

        for range_role in self.SELECTED_RANGE_ROLES:
            weapon = next(
                (
                    item
                    for item in weapons
                    if item.range_role == range_role and item.identity not in used_identities
                ),
                None,
            )

            if weapon is None:
                weapon = self._fallback_weapon_for_range(weapons, range_role, used_identities)

            if weapon is None:
                logger.warning("Unable to select a %s META weapon from WZStats", range_role)
                continue

            weapon.range_role = range_role
            selected.append(weapon)
            used_identities.add(weapon.identity)
            logger.debug(
                "Selected WZStats META weapon: range=%s | rank=%s | weapon=%s | type=%s",
                range_role,
                weapon.rank,
                weapon.name,
                weapon.weapon_type,
            )

        return selected

    def _fallback_weapon_for_range(
        self,
        weapons: list[Weapon],
        range_role: str,
        used_identities: set[str],
    ) -> Weapon | None:
        if range_role == "Longue portée":
            preferred_types = {"SNIPER", "MARKSMAN", "LMG"}
        elif range_role == "Courte portée":
            preferred_types = {"SMG", "SHOTGUN", "PISTOL"}
        else:
            preferred_types = set()

        for weapon in weapons:
            if weapon.identity in used_identities:
                continue
            if weapon.weapon_type.upper() in preferred_types:
                return weapon

        for weapon in weapons:
            if weapon.identity not in used_identities:
                return weapon

        return None

    def _extract_build_from_html(self, html: str) -> dict[str, str]:
        build = self._extract_build_from_json_scripts(html)
        if build:
            return build

        return self._extract_build_from_text_tokens(html)

    def _extract_build_from_json_scripts(self, html: str) -> dict[str, str]:
        scripts = re.findall(
            r"<script[^>]+(?:id=[\"']__NEXT_DATA__[\"']|type=[\"']application/json[\"'])[^>]*>(.*?)</script>",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        build: dict[str, str] = {}

        for script in scripts:
            try:
                payload = json.loads(unescape(script))
            except json.JSONDecodeError:
                continue

            self._collect_build_from_json(payload, build)
            if build:
                logger.debug("WZStats build extracted from JSON: %s", build)
                return build

        return {}

    def _collect_build_from_json(self, node: Any, build: dict[str, str], parent_slot: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                self._collect_build_from_json(item, build)
            return

        if not isinstance(node, dict):
            return

        slot = self._slot_from_json_node(node)
        name = self._attachment_name_from_json_node(node)
        level = self._attachment_level_from_json_node(node)

        if slot:
            logger.debug("Raw WZStats attachment JSON for %s: %s", slot, self._compact_json_context(node))
            if name:
                self._store_attachment(build, slot, name, level)
            elif level:
                self._store_attachment(build, slot, "Nom introuvable", level)

        for key, value in node.items():
            nested_slot = self._canonical_attachment_label(str(key))
            if nested_slot and isinstance(value, str):
                logger.debug("Raw WZStats attachment JSON value for %s: %s", nested_slot, value)
                name, level = self._split_attachment_name_and_level(value)
                if self._is_valid_attachment_name(name):
                    self._store_attachment(build, nested_slot, name, level)
                elif level:
                    self._store_attachment(build, nested_slot, "Nom introuvable", level)
                continue

            self._collect_build_from_json(value, build)

    def _slot_from_json_node(self, node: dict[str, Any]) -> str:
        for key in ("type", "slot", "category", "attachmentType", "attachment_type", "key"):
            value = node.get(key)
            if isinstance(value, str):
                slot = self._canonical_attachment_label(value)
                if slot:
                    return slot
        return ""

    def _attachment_name_from_json_node(self, node: dict[str, Any]) -> str:
        for key in ("name", "title", "label", "attachmentName", "attachment_name", "displayName"):
            value = node.get(key)
            if not isinstance(value, str):
                continue
            name, _ = self._split_attachment_name_and_level(value)
            if self._is_valid_attachment_name(name):
                return name
        return ""

    def _attachment_level_from_json_node(self, node: dict[str, Any]) -> str:
        for key in ("level", "unlockLevel", "unlock_level", "requiredLevel", "required_level"):
            value = node.get(key)
            if isinstance(value, int):
                return f"Niveau {value}"
            if isinstance(value, str):
                _, level = self._split_attachment_name_and_level(value)
                if level:
                    return level
                if value.strip().isdigit():
                    return f"Niveau {value.strip()}"
        return ""

    def _compact_json_context(self, node: dict[str, Any]) -> str:
        try:
            value = json.dumps(node, ensure_ascii=False, sort_keys=True)
        except TypeError:
            value = str(node)
        return value[:1000]

    def _extract_build_from_text_tokens(self, html: str) -> dict[str, str]:
        parser = WZStatsDomParser()
        parser.feed(html)

        build: dict[str, str] = {}
        candidates = self._attachment_block_candidates(parser.root)
        candidates.sort(key=lambda item: item[0])

        for _, label, value, level, raw_text in candidates:
            if label in build:
                continue
            logger.debug("WZStats attachment DOM block raw: %s", raw_text)
            self._store_attachment(build, label, value or "Nom introuvable", level)

        return build

    def _attachment_block_candidates(self, root: HtmlNode) -> list[tuple[int, str, str, str, str]]:
        candidates: list[tuple[int, str, str, str, str]] = []

        for node in self._walk_nodes(root):
            raw_text = node.full_text()
            if not raw_text:
                continue

            labels = self._labels_in_node(node)
            if len(labels) != 1:
                continue

            label = labels[0]
            value, level = self._attachment_value_from_node(node, label)
            if not value and not level:
                continue

            score = self._attachment_node_score(node, label, value)
            candidates.append((score, label, value, level, raw_text[:500]))

        return candidates

    def _labels_in_node(self, node: HtmlNode) -> list[str]:
        labels: set[str] = set()

        for text in self._node_text_units(node):
            label = self._canonical_attachment_label(text)
            if label:
                labels.add(label)
                continue

            labels.update(self._labels_in_text(text))

        return list(labels)

    def _walk_nodes(self, node: HtmlNode) -> list[HtmlNode]:
        nodes = [node]
        for child in node.children:
            nodes.extend(self._walk_nodes(child))
        return nodes

    def _labels_in_text(self, value: str) -> list[str]:
        words = set()
        for part in re.split(r"[\n\r|/]+", value):
            clean_part = self._clean_text(part)
            label = self._canonical_attachment_label(clean_part)
            if label:
                words.add(label)
                continue

            inline_match = re.match(r"^([^:：-]{3,35})\s*[:：-]\s*.+$", clean_part)
            if inline_match:
                label = self._canonical_attachment_label(inline_match.group(1))
                if label:
                    words.add(label)

        normalized = self._normalize_label(value)
        for label, aliases in self.SLOT_ALIASES.items():
            if any(re.search(rf"(^|\s){re.escape(alias)}\s*:", normalized) for alias in aliases):
                words.add(label)

        return list(words)

    def _attachment_value_from_node(self, node: HtmlNode, label: str) -> tuple[str, str]:
        texts = self._sibling_value_texts_for_label(node, label) or self._node_text_units(node)
        raw_text = node.full_text()

        inline_value = self._value_after_label(raw_text)
        if inline_value:
            name, level = self._split_attachment_name_and_level(inline_value)
            if self._is_valid_attachment_name(name):
                return name, level

        level = ""
        best_name = ""

        for text in texts:
            if self._canonical_attachment_label(text):
                continue

            candidate_text = self._strip_label_from_text(text, label)
            name, possible_level = self._split_attachment_name_and_level(candidate_text)
            if possible_level and not level:
                level = possible_level
            if self._is_valid_attachment_name(name):
                best_name = name
                break

        return best_name, level

    def _sibling_value_texts_for_label(self, node: HtmlNode, label: str) -> list[str]:
        values: list[str] = []
        label_seen = False

        for child in node.children:
            text = child.full_text()
            if not text:
                continue
            if self._canonical_attachment_label(text) == label:
                label_seen = True
                continue
            if label_seen:
                values.append(text)

        return values

    def _strip_label_from_text(self, value: str, label: str) -> str:
        value = self._clean_text(value)
        aliases = self.SLOT_ALIASES.get(label, ())
        for alias in aliases:
            flexible_alias = re.escape(alias).replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]+")
            pattern = rf"^\s*{flexible_alias}\s*[:：-]?\s*"
            stripped = re.sub(pattern, "", value, flags=re.IGNORECASE)
            if stripped != value:
                return self._clean_text(stripped)
        return value

    def _node_text_units(self, node: HtmlNode) -> list[str]:
        values: list[str] = []
        own = node.own_text()
        if own:
            values.append(own)
        for child in node.children:
            values.extend(self._node_text_units(child))
        return values

    def _attachment_node_score(self, node: HtmlNode, label: str, value: str) -> int:
        text_count = len(self._node_text_units(node))
        score = len(node.full_text()) + text_count * 15
        if node.own_text() and self._canonical_attachment_label(node.own_text()) == label:
            score -= 40
        if value:
            score -= 20
        return score

    def _store_attachment(self, build: dict[str, str], label: str, name: str, level: str) -> None:
        value = self._format_attachment_value(name, level)
        if not value:
            return
        build.setdefault(label, value)
        logger.debug("WZStats attachment extracted: %s -> %s", label, value)

    def _build_to_attachment_lines(self, build: dict[str, str]) -> list[str]:
        return [f"{label}: {value}" for label, value in build.items()]

    def _next_attachment_name_and_level(self, values: list[str], start_index: int) -> tuple[str, str]:
        attachment_name = ""
        level = ""

        for offset, value in enumerate(values[start_index : start_index + 8]):
            if self._canonical_attachment_label(value):
                continue

            name_part, level_part = self._split_attachment_name_and_level(value)
            if level_part and not level:
                level = level_part

            if not self._is_valid_attachment_name(name_part):
                continue

            attachment_name = name_part
            if not level:
                absolute_index = start_index + offset
                _, next_level = self._split_attachment_name_and_level(" ".join(values[absolute_index + 1 : absolute_index + 3]))
                level = next_level
            break

        return attachment_name, level

    def _split_inline_attachment(self, value: str) -> tuple[str, str]:
        match = re.match(r"^([^:：]{3,35})\s*[:：]\s*(.{2,120})$", value)
        if not match:
            return "", ""

        label = self._canonical_attachment_label(match.group(1))
        if not label:
            return "", ""

        name, level = self._split_attachment_name_and_level(match.group(2))
        if self._is_valid_attachment_name(name):
            return label, self._format_attachment_value(name, level)
        if level:
            return label, self._format_attachment_value("Nom introuvable", level)
        return "", ""

    def _value_after_label(self, value: str) -> str:
        for separator in (":", "："):
            if separator not in value:
                continue
            _, attachment = value.split(separator, 1)
            attachment = self._clean_text(attachment)
            if attachment and not self._canonical_attachment_label(attachment):
                return attachment
        return ""

    def _split_attachment_name_and_level(self, value: str) -> tuple[str, str]:
        value = self._clean_text(value)
        if not value:
            return "", ""

        level_patterns = [
            r"\(?\s*(Niveau|Level|Lvl|Lv\.?)\s*(\d+)\s*\)?",
            r"\(?\s*(Déblocage|Unlock(?:ed)?(?: at)?)\s*(?:au|at)?\s*(?:Niveau|Level|Lvl)?\s*(\d+)\s*\)?",
        ]
        level = ""

        for pattern in level_patterns:
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if not match:
                continue
            level = f"Niveau {match.group(2)}"
            value = self._clean_text(value[: match.start()] + " " + value[match.end() :])
            break

        value = re.sub(r"^[•\-\u2013\u2014:：\s]+", "", value)
        value = re.sub(r"[•\-\u2013\u2014:：\s]+$", "", value)
        value = self._clean_text(value)

        if self._is_level_only(value):
            return "", level or self._normalize_level(value)

        return value, level

    def _format_attachment_value(self, name: str, level: str) -> str:
        name = self._clean_text(name)
        level = self._clean_text(level)

        if not name or self._is_level_only(name):
            return ""

        if level and level.casefold() not in name.casefold():
            return f"{name} ({level})"

        return name

    def _is_valid_attachment_name(self, value: str) -> bool:
        value = self._clean_text(value)
        if not value:
            return False
        if len(value) < 2 or len(value) > 90:
            return False
        if value.startswith("#"):
            return False
        if self._is_level_only(value):
            return False
        if self._canonical_attachment_label(value):
            return False
        if value.casefold() in {"meta", "warzone meta", "mise à jour", "new", "nouveau", "code", "unknown"}:
            return False
        return True

    def _is_level_only(self, value: str) -> bool:
        return bool(
            re.fullmatch(
                r"\(?\s*(Niveau|Level|Lvl|Lv\.?)\s*\d+\s*\)?",
                self._clean_text(value),
                flags=re.IGNORECASE,
            )
        )

    def _normalize_level(self, value: str) -> str:
        match = re.search(r"\d+", value)
        return f"Niveau {match.group(0)}" if match else ""

    def _canonical_attachment_label(self, value: str) -> str:
        normalized = self._normalize_label(value).strip(" :：-")

        for label, aliases in self.SLOT_ALIASES.items():
            if any(normalized == alias for alias in aliases):
                return label

        return ""

    def _normalize_label(self, value: str) -> str:
        value = self._clean_text(value)
        value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value).casefold()
        value = value.replace("_", " ").replace("-", " ")
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
        return re.sub(r"\s+", " ", value).strip()

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

            final_range_role = self._resolve_range_role(
                str(item.get("rangeRole", "")),
                str(item.get("weaponType", "")),
            )
            logger.debug(
                "WZStats range detection: weapon=%s | type=%s | raw_range=%s | final_range=%s",
                name,
                self._clean_text(str(item.get("weaponType", ""))) or "Inconnu",
                self._clean_text(str(item.get("rangeRaw", ""))) or "Aucun",
                final_range_role,
            )

            weapons.append(
                Weapon(
                    name=name,
                    tier=tier,
                    weapon_type=self._clean_text(str(item.get("weaponType", ""))),
                    image_url=urljoin(self.base_url, str(item.get("imageUrl", ""))),
                    url=url,
                    rank=len(weapons) + 1,
                    range_role=final_range_role,
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

    def _find_range_role_from_sources(self, values: list[str], loadout_name: str) -> tuple[str, str]:
        for value in values:
            range_role = self._range_role_from_wzstats_text(value)
            if range_role:
                return range_role, value

        range_role = self._range_role_from_wzstats_text(loadout_name)
        if range_role:
            return range_role, loadout_name

        return "", ""

    def _range_role_from_wzstats_text(self, value: str) -> str:
        normalized = self._normalize_range_text(value)
        if not normalized:
            return ""

        patterns = [
            (r"\bsniper\s+support\b", "Moyenne portée"),
            (r"\blong\s+range\b|\blongue\s+portee\b|^long$", "Longue portée"),
            (r"\bclose\s+range\b|\bcourte\s+portee\b|^close$", "Courte portée"),
            (r"\bsupport\b", "Moyenne portée"),
            (r"\bflex\b|\bbalanced\b|\bversatile\b|\bpolyvalente?\b", "Polyvalente"),
        ]

        for pattern, range_role in patterns:
            if re.search(pattern, normalized):
                return range_role

        return ""

    def _resolve_range_role(self, explicit_range: str, weapon_type: str) -> str:
        explicit_range = self._clean_text(explicit_range)
        if explicit_range:
            return explicit_range

        normalized_type = self._clean_text(weapon_type).casefold()
        if normalized_type in {"smg", "shotgun", "pistol"}:
            return "Courte portée"
        if normalized_type in {"sniper", "marksman", "lmg"}:
            return "Longue portée"
        if normalized_type in {"ar", "assault rifle", "br", "battle rifle"}:
            return "Polyvalente"

        return "Polyvalente"

    def _normalize_range_text(self, value: str) -> str:
        value = self._normalize_label(value)
        value = value.replace("/", " ").replace("_", " ").replace("-", " ")
        return re.sub(r"\s+", " ", value).strip()

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
