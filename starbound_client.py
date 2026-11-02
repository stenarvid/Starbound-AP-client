"""Archipelago client with a vanilla Starbound file bridge."""
import asyncio
import logging
import uuid
from pathlib import Path

import Utils
from CommonClient import ClientCommandProcessor, CommonContext, get_base_parser
from NetUtils import ClientStatus, NetworkItem
from starbound_bridge import FileBridge, bridge_lock

logger = logging.getLogger("StarboundClient")
DEFAULT_BRIDGE = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Starbound\mods\Archipelago\bridge")


def plain_name(text):
    # Player aliases are content, not Starbound formatting codes.
    return str(text).replace("^", "").replace("\n", " ").replace("\r", " ")[:200]


def found_text(ctx, item, receiving):
    finder = "You" if item.player == ctx.slot else ctx.player_names.get(item.player, f"Player {item.player}")
    owner = "your" if receiving == ctx.slot else ctx.player_names.get(receiving, f"Player {receiving}") + "'s"
    name = ctx.item_names.lookup_in_slot(item.item, receiving)
    if item.location < 0:
        location = "Starting Inventory" if item.location == -2 else "Server"
    else:
        location = ctx.location_names.lookup_in_slot(item.location, item.player)
    return f"{plain_name(finder)} found {plain_name(owner)} {plain_name(name)} ({plain_name(location)})."


class StarboundCommands(ClientCommandProcessor):
    def _cmd_bridge(self) -> bool:
        """Show the bridge directory, bound character, and acknowledged item count."""
        bridge = self.ctx.bridge
        self.output(f"Bridge: {bridge.directory}\nCharacter: {bridge.owner or 'waiting'}\n"
                    f"Acknowledged items: {bridge.acknowledged}")
        return True

    def _cmd_locations(self) -> bool:
        """List missing Starbound checks."""
        for location in sorted(self.ctx.missing_locations):
            self.output(f"{location}: {self.ctx.location_names.lookup_in_game(location)}")
        return True

    def _cmd_check(self, location: str) -> bool:
        """Development only: send one location ID manually, e.g. /check 1."""
        if not location.isdigit() or int(location) not in self.ctx.missing_locations:
            self.output("Enter a currently missing location ID.")
            return False
        Utils.async_start(self.ctx.check_locations({int(location)}), name="manual check")
        return True

    def _cmd_data(self) -> bool:
        """List Starbound item and location IDs."""
        for label, table in (("Item", self.ctx.item_names), ("Location", self.ctx.location_names)):
            for code, name in sorted(table["Starbound"].items()):
                self.output(f"{label} {code}: {name}")
        return True


class StarboundContext(CommonContext):
    game = "Starbound"
    items_handling = 0b111
    command_processor = StarboundCommands

    def __init__(self, server_address, password, bridge_directory=DEFAULT_BRIDGE, name="StarboundTester"):
        super().__init__(server_address, password)
        self.auth = name
        self.bridge = FileBridge(Path(bridge_directory))
        self.bridge_error = None
        self.goal_sent = False
        self.reward_scouts_requested = set()

    async def server_auth(self, password_requested=False):
        await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    async def disconnect(self, allow_autoreconnect=False):
        self.bridge.deactivate()
        await super().disconnect(allow_autoreconnect)
        if not allow_autoreconnect:
            self.seed_name = None

    def on_package(self, cmd, args):
        if cmd == "RoomInfo":
            self.seed_name = args["seed_name"]
        elif cmd == "Connected":
            self.goal_sent = False
            self.reward_scouts_requested.clear()
            if self.slot is not None and self.seed_name:
                self.bridge.bind_session(self.seed_name, self.team, self.slot)
            logger.info("Connected as %s. Start your Starbound character; /bridge shows delivery status.", self.auth)
        elif cmd == "PrintJSON" and args.get("type") == "ItemSend":
            item = NetworkItem(*args["item"])
            receiving = args["receiving"]
            if item.player == self.slot:
                # LocationInfo's player field is the recipient, unlike ReceivedItems.
                self.locations_info[item.location] = NetworkItem(item.item, item.location, receiving, item.flags)
            # Received items notify when actually delivered. This path covers items
            # found here for somebody else, which do not enter our received history.
            if item.player == self.slot and receiving != self.slot and self.seed_name:
                self.bridge.bind_session(self.seed_name, self.team, self.slot)
                key = f"found:{item.player}:{item.location}:{receiving}:{item.item}"
                self.bridge.queue_notice(key, found_text(self, item, receiving))

    async def bridge_tick(self):
        if not (self.server and self.server.socket.open and self.slot is not None and self.seed_name):
            self.bridge.deactivate()
            return
        self.bridge.bind_session(self.seed_name, self.team, self.slot)
        codes = [item.item for item in self.items_received]
        state = self.bridge.consume(len(codes))
        if state:
            # Retry until acknowledged by the server's missing_locations update.
            await self.check_locations(state.checks)
            if state.goal and not self.goal_sent:
                self.finished_game = True
                await self.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                self.goal_sent = True
        item_texts = {
            index: found_text(self, item, self.slot)
            for index, item in enumerate(self.items_received, 1)
            if index > self.bridge.acknowledged and hasattr(item, "player") and hasattr(item, "location")
        }
        checked = set(getattr(self, "checked_locations", ())) & {1, 2, 3}
        info = getattr(self, "locations_info", {})
        requested = getattr(self, "reward_scouts_requested", set())
        to_scout = checked - info.keys() - requested
        if to_scout:
            # Query only completed checks: no unearned item spoilers or public hints.
            await self.send_msgs([{"cmd": "LocationScouts", "locations": sorted(to_scout), "create_as_hint": 0}])
            requested.update(to_scout)
            self.reward_scouts_requested = requested
        rewards = {
            location: {
                "item": plain_name(self.item_names.lookup_in_slot(info[location].item, info[location].player)),
                "recipient": plain_name(self.player_names.get(info[location].player, f"Player {info[location].player}")),
                "mine": info[location].player == self.slot,
            }
            for location in checked & info.keys()
        }
        self.bridge.publish(codes, item_texts=item_texts,
                            checked_locations=checked, journal=True, location_rewards=rewards)

    async def bridge_loop(self):
        while not self.exit_event.is_set():
            try:
                await self.bridge_tick()
                self.bridge_error = None
            except (OSError, ValueError) as error:
                if str(error) != self.bridge_error:
                    logger.error("Bridge error: %s", error)
                    self.bridge_error = str(error)
            try:
                await asyncio.wait_for(self.exit_event.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass


async def test_reward(directory):
    bridge = FileBridge(directory)
    bridge.bind_session("tier4-test-" + uuid.uuid4().hex, 0, 0)
    logger.info("Tier 4 test running. Start your character; press Ctrl+C to stop.")
    announced = False
    try:
        while True:
            state = bridge.consume(1)
            bridge.publish([4], item_texts={1: "Received a Random Tier 4 Weapon (Bridge Test)."})
            if state and state.acknowledged == 1 and not announced:
                logger.info("The character acknowledged the tier 4 weapon. Test passed.")
                announced = True
            await asyncio.sleep(0.5)
    finally:
        bridge.deactivate()


async def main(args):
    if args.test_tier4:
        await test_reward(Path(args.bridge_dir))
        return
    ctx = StarboundContext(args.connect, args.password, args.bridge_dir, args.name)
    ctx.run_cli()
    bridge_task = asyncio.create_task(ctx.bridge_loop(), name="Starbound file bridge")
    try:
        if args.connect:
            await ctx.connect()
        await ctx.exit_event.wait()
    finally:
        ctx.exit_event.set()
        await bridge_task
        ctx.bridge.deactivate()
        await ctx.shutdown()


if __name__ == "__main__":
    parser = get_base_parser(description="Vanilla Starbound Archipelago Client")
    parser.add_argument("--name", default="StarboundTester", help="Archipelago slot name")
    parser.add_argument("--bridge-dir", type=Path, default=DEFAULT_BRIDGE,
                        help="Must match scripts/ap_bridge.config in the mod")
    parser.add_argument("--test-tier4", action="store_true", help="Deliver one test weapon without an AP server")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger().setLevel(logging.INFO)
    try:
        with bridge_lock(Path(args.bridge_dir)):
            asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError) as error:
        logger.error("Could not start Starbound client: %s", error)
        raise SystemExit(1)
