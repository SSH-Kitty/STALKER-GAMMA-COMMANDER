"""Anomaly's achievements, as GAMMA balances them.

The requirements and rewards below follow ``game_achievements.script`` from
the "G.A.M.M.A. Achievements Balance" mod (which wins over the copy in the
player-ranks mod in GAMMA's load order). Whether each one is unlocked comes
from the save (``game_stats.SaveStats.achievements``); for the ones that are
a count of something, progress comes from the same save's statistics.
"""

from __future__ import annotations

from dataclasses import dataclass

from .game_stats import SaveStats
from .i18n import tr


@dataclass(frozen=True)
class Achievement:
    key: str
    name: str
    how: str
    reward: str
    #: ``(actor_statistics key, goal)`` when progress is a simple count.
    counter: tuple[str, int] | None = None


ACHIEVEMENTS: tuple[Achievement, ...] = (
    Achievement(
        "tourist", "Tourist",
        "Visit every level in the Zone.",
        "Three hidden stashes are revealed.",
    ),
    Achievement(
        "geologist", "Geologist",
        "Detect 50 artefacts.",
        "Artefacts spawn more often.",
        ("artefacts_detected", 50),
    ),
    Achievement(
        "rag_and_bone", "Rag and Bone",
        "Find and loot 100 stashes.",
        "A chance of better loot in task reward stashes.",
        ("stashes_found", 100),
    ),
    Achievement(
        "infopreneur", "Infopreneur",
        "Deliver 50 PDAs.",
        "More money for delivering PDAs.",
        ("pdas_delivered", 50),
    ),
    Achievement(
        "well_dressed", "Well Dressed",
        "Kill 500 mutants, or field dress 250 mutant parts.",
        "20% chance of extra parts when field dressing.",
        ("killed_monsters", 500),
    ),
    Achievement(
        "silver_or_lead", "Silver or Lead",
        "Kill 500 stalkers, or have 50 surrender to you.",
        "33% chance of a second stash from surrendering stalkers.",
        ("killed_stalkers", 500),
    ),
    Achievement(
        "down_to_earth", "Down to Earth",
        "Bring down 3 helicopters, or 1 with an RPG-7.",
        "Weaker helicopters respawn.",
        ("helicopters_downed", 3),
    ),
    Achievement(
        "radiotherapy", "Radiotherapy",
        "Survive 25 emissions and 25 psi-storms.",
        "25% chance of surviving an emission or psi-storm.",
        ("emissions", 25),
    ),
    Achievement(
        "infantile_pleasure", "Infantile Pleasure",
        "Smash 200 boxes.",
        "25% chance of extra items in boxes.",
        ("boxes_smashed", 200),
    ),
    Achievement(
        "recycler", "Recycler",
        "Disassemble 200 items.",
        "33% chance of an extra part when disassembling.",
        ("items_disassembled", 200),
    ),
    Achievement(
        "artificer_eagerness", "Artificer Eagerness",
        "Craft 50 items.",
        "Crafting uses one part fewer.",
        ("items_crafted", 50),
    ),
    Achievement(
        "unforeseen_guest", "Unforeseen Guest",
        "Stay disguised among stalkers for 5 hours in total.",
        "Sudden actions raise less suspicion while disguised.",
        ("minutes_disguised", 300),
    ),
    Achievement(
        "patriarch", "Patriarch",
        "Reach the rank of Legend.",
        "Recruit larger squads.",
    ),
    Achievement(
        "heavy_pockets", "Heavy Pockets",
        "Carry 10,000,000 RU.",
        "Traders sell cheaper and rarer goods.",
    ),
    Achievement(
        "mechanized_warfare", "Mechanized Warfare",
        "Give any mechanic every tool kit.",
        "That mechanic can fully upgrade equipment.",
    ),
    Achievement(
        "bookworm_food", "Bookworm Food",
        "Unlock every article in the PDA guide.",
        "Memory sticks on dead stalkers become rare PDAs.",
    ),
    Achievement(
        "duga_free", "Duga Free",
        "Story mode: switch off the Brain Scorcher and the Miracle Machine.",
        "The Yantar and Radar psi-fields are gone.",
    ),
    Achievement(
        "wishful_thinking", "Wishful Thinking",
        "Story mode: finish the Living Legend storyline.",
        "Unlocks the Renegades faction.",
    ),
    Achievement(
        "absolver", "Absolver",
        "Story mode: finish the Mortal Sin storyline.",
        "Unlocks the Sin faction.",
    ),
    Achievement(
        "collaborator", "Collaborator",
        "Story mode: finish Operation Afterglow.",
        "Unlocks the ISG faction.",
    ),
    Achievement(
        "iron_curtain", "Iron Curtain",
        "Warfare mode: your faction takes over the Zone.",
        "40,000 RU.",
    ),
    Achievement(
        "murky_spirit", "Murky Spirit",
        "Story mode on Ironman: finish Operation Afterglow.",
        "Bragging rights.",
    ),
    Achievement(
        "invictus", "Invictus",
        "Murky Spirit without ever dying, on the hardest difficulty and "
        "economy, never changing either.",
        "Bragging rights.",
    ),
    Achievement(
        "completionist", "Completionist",
        "Unlock all the other achievements.",
        "Double rank points for this one.",
    ),
)


def unlocked(save: SaveStats | None, achievement: Achievement) -> bool:
    return bool(save is not None and save.achievements.get(achievement.key))


def progress(save: SaveStats | None, achievement: Achievement) -> tuple[int, int] | None:
    """``(current, goal)`` for a count-based achievement, else None."""
    if save is None or achievement.counter is None:
        return None
    key, goal = achievement.counter
    return min(save.get(key), goal), goal


def unlocked_count(save: SaveStats | None) -> int:
    return sum(1 for a in ACHIEVEMENTS if unlocked(save, a))


def status_text(save: SaveStats | None, achievement: Achievement) -> str:
    """"Unlocked", progress towards a count ("143 / 200"), or "Locked"."""
    if unlocked(save, achievement):
        return tr("Unlocked")
    counted = progress(save, achievement)
    if counted is not None:
        return f"{counted[0]:,} / {counted[1]:,}"
    return tr("Locked")
