from dataclasses import dataclass

from Options import PerGameCommonOptions, Toggle


class RandomTier4Weapon(Toggle):
    """Replace the Ship Upgrade reward with one random level 4 rare weapon."""
    display_name = "Random Tier 4 Weapon"


@dataclass
class StarboundOptions(PerGameCommonOptions):
    random_tier4_weapon: RandomTier4Weapon