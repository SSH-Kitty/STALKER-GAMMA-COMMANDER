#!/usr/bin/env python3
"""Post a GitHub release's notes to the Discord #releases channel.

Dev/CI-only tool, not shipped with the app. Discord's built-in GitHub
webhook endpoint (``.../github``) posts an empty message for releases, so
the release workflow calls this instead: it turns the release body into
rich embeds (the same style as earlier release posts) and sends them
through a plain Discord webhook.

Long notes are split on their ``###`` sections so every embed stays under
Discord's 4096-character description limit and every message under the
6000-character total limit.

Posting as the STALKER COMMANDER bot (like earlier release posts) needs
DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID; otherwise DISCORD_WEBHOOK_URL is
used, with the bot's name as the webhook username.

Usage:
    DISCORD_BOT_TOKEN=... DISCORD_CHANNEL_ID=... python scripts/post_release_discord.py v1.3.0
    DISCORD_WEBHOOK_URL=... python scripts/post_release_discord.py v1.3.0
    DISCORD_WEBHOOK_URL=... python scripts/post_release_discord.py --event "$GITHUB_EVENT_PATH"
    python scripts/post_release_discord.py v1.3.0 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = "SSH-Kitty/STALKER-GAMMA-COMMANDER"
COLOR = 9430108
FOOTER = "STALKER GAMMA COMMANDER"
USERNAME = "STALKER COMMANDER"

DESC_LIMIT = 4096
MESSAGE_LIMIT = 6000
EMBEDS_PER_MESSAGE = 10


def load_release(tag: str | None, event_path: str | None) -> dict:
    """Return ``{name, tag_name, html_url, body, published_at}``."""
    if event_path:
        with open(event_path, encoding="utf-8") as f:
            return json.load(f)["release"]
    out = subprocess.run(
        ["gh", "api", f"repos/{REPO}/releases/tags/{tag}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def to_discord_markdown(body: str) -> str:
    """Drop the notes' own ``##`` title lines (the embed has a title) and
    turn ``###`` headings into bold lines, matching earlier posts."""
    lines = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if re.match(r"^##\s", line):
            continue
        m = re.match(r"^#{3,}\s+(.*)$", line)
        lines.append(f"**{m.group(1).strip()}**" if m else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def split_chunk(text: str, limit: int) -> list[str]:
    """Split ``text`` on line boundaries into pieces of at most ``limit``."""
    pieces, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single absurdly long line
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            pieces.append(current)
            current = line
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def build_messages(release: dict) -> list[list[dict]]:
    """Return a list of messages, each a list of embeds."""
    name = release.get("name") or release["tag_name"]
    url = release["html_url"]
    text = to_discord_markdown(release.get("body") or "")
    download = f"\U0001f4e5 **[Download {name}]({url})**"

    # One chunk per section: a section starts at a bold heading line.
    sections = re.split(r"\n(?=\*\*[^\n]+\*\*\n)", text) if text else []
    chunks: list[str] = []
    for section in sections:
        chunks.extend(split_chunk(section.strip(), DESC_LIMIT))
    if not chunks or len(chunks[-1]) + len(download) + 2 > DESC_LIMIT:
        chunks.append(download)
    else:
        chunks[-1] = f"{chunks[-1]}\n\n{download}"

    embeds = [{"description": c, "color": COLOR} for c in chunks]
    embeds[0].update(title=name, url=url)
    embeds[-1]["footer"] = {"text": FOOTER}
    if release.get("published_at"):
        embeds[-1]["timestamp"] = release["published_at"]

    def size(e: dict) -> int:
        return (len(e.get("title", "")) + len(e["description"])
                + len(e.get("footer", {}).get("text", "")))

    messages: list[list[dict]] = [[]]
    for e in embeds:
        msg = messages[-1]
        if msg and (len(msg) >= EMBEDS_PER_MESSAGE
                    or sum(map(size, msg)) + size(e) > MESSAGE_LIMIT):
            messages.append([])
        messages[-1].append(e)
    return messages


def send(embeds: list[dict]) -> None:
    payload: dict = {"embeds": embeds, "allowed_mentions": {"parse": []}}
    headers = {"Content-Type": "application/json",
               "User-Agent": "COMMANDER-release-poster"}
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel = os.environ.get("DISCORD_CHANNEL_ID")
    if token and channel:
        url = f"https://discord.com/api/v10/channels/{channel}/messages"
        headers["Authorization"] = f"Bot {token}"
    else:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL")
        if not webhook:
            sys.exit("set DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID, "
                     "or DISCORD_WEBHOOK_URL")
        url = webhook.removesuffix("/github") + "?wait=true"
        payload["username"] = USERNAME
        if os.environ.get("DISCORD_AVATAR_URL"):
            payload["avatar_url"] = os.environ["DISCORD_AVATAR_URL"]
    # url/webhook come from CI-configured env vars, not a remote party, but
    # keep the same http(s)-only rule commander_gui.network enforces on
    # every other URL this project opens.
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        sys.exit(f"refusing to open a non-http(s) URL: {url!r}")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise SystemExit(f"Discord rejected the post: HTTP {e.code} "
                                 f"{e.read().decode(errors='replace')}") from e
            time.sleep(float(json.loads(e.read()).get("retry_after", 1)) + 0.5)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tag", nargs="?", help="release tag, e.g. v1.3.0")
    ap.add_argument("--event", help="path to a GitHub release event JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the payloads instead of posting")
    args = ap.parse_args()
    if not (args.tag or args.event):
        ap.error("give a tag or --event")

    messages = build_messages(load_release(args.tag, args.event))
    if args.dry_run:
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        return
    for embeds in messages:
        send(embeds)
    print(f"Posted {len(messages)} message(s) to Discord.")


if __name__ == "__main__":
    main()
