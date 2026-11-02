from BaseClasses import Item, ItemClassification, Location, Region
from worlds.AutoWorld import World
from .options import StarboundOptions


class StarboundItem(Item):
    game = "Starbound"


class StarboundLocation(Location):
    game = "Starbound"


class StarboundWorld(World):
    game = "Starbound"
    options_dataclass = StarboundOptions

    item_name_to_id = {
        "Double Jump": 1,
        "Dash": 2,
        "Ship Upgrade": 3,
        "Random Tier 4 Weapon": 4,
    }

    location_name_to_id = {
        "Starting Chest": 1,
        "Tech Chest": 2,
        "Erchius Horror": 3,
    }

    def create_item(self, name: str) -> StarboundItem:
        return StarboundItem(
            name,
            ItemClassification.progression if name in {"Double Jump", "Dash"} else ItemClassification.useful,
            self.item_name_to_id[name],
            self.player,
        )

    def create_regions(self) -> None:
        menu = Region(
            "Menu",
            self.player,
            self.multiworld,
        )

        start = Region(
            "Starting Area",
            self.player,
            self.multiworld,
        )

        tech = Region(
            "Tech Area",
            self.player,
            self.multiworld,
        )

        mission = Region(
            "Mission Area",
            self.player,
            self.multiworld,
        )

        start.locations.append(
            StarboundLocation(
                self.player,
                "Starting Chest",
                self.location_name_to_id["Starting Chest"],
                start,
            )
        )

        tech.locations.append(
            StarboundLocation(
                self.player,
                "Tech Chest",
                self.location_name_to_id["Tech Chest"],
                tech,
            )
        )

        mission.locations.append(
            StarboundLocation(
                self.player,
                "Erchius Horror",
                self.location_name_to_id["Erchius Horror"],
                mission,
            )
        )

        self.multiworld.regions.extend([
            menu,
            start,
            tech,
            mission,
        ])

        menu.connect(start)

        start.connect(
            tech,
            "Double Jump",
            lambda state: state.has("Double Jump", self.player)
        )

        tech.connect(
            mission,
            "Dash",
            lambda state: state.has("Dash", self.player)
        )

        victory = StarboundLocation(self.player, "Defeat Erchius Horror", None, mission)
        victory.place_locked_item(
            StarboundItem("Victory", ItemClassification.progression, None, self.player)
        )
        mission.locations.append(victory)

    def create_items(self) -> None:
        reward = "Random Tier 4 Weapon" if self.options.random_tier4_weapon else "Ship Upgrade"
        for item_name in ("Double Jump", "Dash", reward):
            self.multiworld.itempool.append(
                self.create_item(item_name)
            )

    def set_rules(self) -> None:
        self.set_completion_rule(lambda state: state.has("Victory", self.player))

