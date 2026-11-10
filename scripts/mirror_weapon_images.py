"""Mirror weapon kill-feed icons for upload to our CDN.

Riot's match data names the gun behind each kill by its content UUID (`damage_item` in
`/matches/{id}/extra`), and Riot's val-content-v1 carries no images, so the icons come from
valorant-api.com's `killStreamIcon` (itself sourced from Riot game data). Riot also counts
Chamber's Headhunter and Tour De Force and Neon's Overdrive as guns; valorant-api.com lists
those only as abilities, so they take the ability's icon.

For each weapon this saves `{key}-killfeed.webp`, lossless, where `key` is the lowercased
alphanumeric name. A `weapons.json` manifest, keyed by the lowercase UUID as `/extra` gives it,
records each weapon's name and its file's source URL and dimensions. Rerun when Riot adds a weapon.

Usage: uv run --with pillow scripts/mirror_weapon_images.py [output-dir] [cdn-base-url]
"""

import io
import json
import re
import sys
from pathlib import Path

import httpx2
from PIL import Image

VALORANT_API_WEAPONS = "https://valorant-api.com/v1/weapons"
VALORANT_API_AGENTS = "https://valorant-api.com/v1/agents?isPlayableCharacter=true"
DEFAULT_CDN_BASE_URL = "https://files.akhilnarang.dev/cdn/valorant/"
ABILITY_GUNS = {
    "856d9a7e-4b06-dc37-15dc-9d809c37cb90": "Headhunter",
    "39099fb5-4293-def4-1e09-2e9080ce7456": "Tour De Force",
    "95336ae4-45d4-1032-cfaf-6bad01910607": "Overdrive",
}


def key(name: str) -> str:
    """Normalize a weapon name to its CDN key.

    :param name: Display name (e.g. "Tour De Force").
    :return: Lowercase alphanumeric key (e.g. "tourdeforce").
    """
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def icon_urls(client: httpx2.Client) -> dict[str, tuple[str, str]]:
    """Find each weapon's kill-feed icon on valorant-api.com.

    :param client: HTTP client.
    :return: Name and icon URL by lowercase weapon UUID.
    :raises ValueError: If an ability gun is missing from valorant-api.com's agents.
    """
    weapons = client.get(VALORANT_API_WEAPONS).raise_for_status().json()["data"]
    icons = {w["uuid"].lower(): (w["displayName"], w["killStreamIcon"]) for w in weapons if w.get("killStreamIcon")}
    abilities = {
        ability["displayName"]: ability["displayIcon"]
        for agent in client.get(VALORANT_API_AGENTS).raise_for_status().json()["data"]
        for ability in agent["abilities"]
        if ability.get("displayIcon")
    }
    for uuid, name in ABILITY_GUNS.items():
        if name not in abilities:
            raise ValueError(f"no ability named {name} on valorant-api.com")
        icons[uuid] = (name, abilities[name])
    return icons


def main() -> None:
    """Download each weapon's kill-feed icon into the output directory.

    :return: None.
    """
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "weapon-assets")
    cdn_base_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_CDN_BASE_URL
    output.mkdir(parents=True, exist_ok=True)
    manifest = {}
    with httpx2.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        for uuid, (name, url) in sorted(icon_urls(client).items(), key=lambda item: item[1][0]):
            image = Image.open(io.BytesIO(client.get(url).raise_for_status().content)).convert("RGBA")
            path = output / f"{key(name)}-killfeed.webp"
            image.save(path, "WEBP", lossless=True, method=6)
            manifest[uuid] = {
                "name": name,
                "killfeed": {
                    "url": f"{cdn_base_url.rstrip('/')}/{path.name}",
                    "file": path.name,
                    "width": image.width,
                    "height": image.height,
                    "upstream_source": url,
                },
            }
            print(f"{name}: {image.width}x{image.height}")
    (output / "weapons.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"{len(manifest)} weapons -> {output}")


if __name__ == "__main__":
    main()
