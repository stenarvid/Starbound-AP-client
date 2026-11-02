"""Versioned data-only mailboxes shared with the vanilla Starbound player script."""
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path


@dataclass(frozen=True)
class GameState:
    character: str
    checks: frozenset[int]
    acknowledged: int
    goal: bool
    notice_acknowledged: int = 0


def session_id(seed: str, team: int, slot: int) -> str:
    return hashlib.sha256(json.dumps([seed, team, slot]).encode()).hexdigest()


def read_game_state(path: Path, session: str) -> GameState | None:
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    if len(text) > 1024 * 1024 or not text.endswith("\nEND\n"):
        return None
    lines = text.splitlines()
    header = lines[0].split("\t")
    if len(header) != 3 or header[:2] != ["AP1", session]:
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{32}", header[2]):
        return None
    checks, acknowledged, goal, notice_acknowledged = set(), None, False, 0
    for line in lines[1:-1]:
        fields = line.split("\t")
        if len(fields) != 2 or not fields[1].isdigit():
            return None
        value = int(fields[1])
        if fields[0] == "CHECK" and value in {1, 2, 3}:
            checks.add(value)
        elif fields[0] == "ACK" and acknowledged is None:
            acknowledged = value
        elif fields == ["GOAL", "1"]:
            goal = True
        elif fields[0] == "NOTICEACK":
            notice_acknowledged = value
        else:
            return None
    if acknowledged is None or (goal and 3 not in checks):
        return None
    return GameState(header[2], frozenset(checks), acknowledged, goal, notice_acknowledged)


def atomic_write(path: Path, text: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="ascii", newline="\n")
    os.replace(temporary, path)


@contextmanager
def bridge_lock(directory: Path):
    """Hold an OS lock so two clients cannot publish competing mailboxes."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "client.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Another Starbound client is already using this bridge directory.") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class FileBridge:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.session = None
        self.owner = None
        self.acknowledged = 0
        self.game_state = None
        self.notices = []
        self.notice_acknowledged = 0

    def bind_session(self, seed: str, team: int, slot: int) -> None:
        session = session_id(seed, team, slot)
        if self.session == session:
            return
        self.session = session
        self.acknowledged = 0
        self.game_state = None
        self.notice_acknowledged = 0
        notice_path = self.directory / (session + ".notices.json")
        self.notices = json.loads(notice_path.read_text(encoding="ascii")) if notice_path.exists() else []
        if not isinstance(self.notices, list) or any(
                not isinstance(entry, dict) or not isinstance(entry.get("key"), str)
                or not isinstance(entry.get("text"), str) for entry in self.notices):
            raise ValueError("Invalid notification history: " + str(notice_path))
        path = self.directory / (session + ".owner")
        self.owner = path.read_text(encoding="ascii").strip() if path.exists() else None
        if self.owner and not re.fullmatch(r"[0-9a-fA-F]{32}", self.owner):
            raise ValueError("Invalid bridge owner file: " + str(path))

    def consume(self, item_count: int) -> GameState | None:
        if not self.session:
            return None
        state = read_game_state(self.directory / "from_game.txt", self.session)
        if (state is None or state.acknowledged > item_count
                or state.notice_acknowledged > len(self.notices)):
            return None
        if self.owner is None:
            self.owner = state.character
            atomic_write(self.directory / (self.session + ".owner"), self.owner + "\n")
        if state.character != self.owner:
            return None
        # A restored character save may legitimately have fewer delivered items.
        self.acknowledged = state.acknowledged
        self.notice_acknowledged = state.notice_acknowledged
        self.game_state = state
        return state

    def queue_notice(self, key: str, text: str) -> None:
        if self.session is None or any(entry["key"] == key for entry in self.notices):
            return
        notices = self.notices + [{"key": key, "text": text}]
        atomic_write(self.directory / (self.session + ".notices.json"), json.dumps(notices))
        self.notices = notices

    def publish(self, item_codes: list[int], active: bool = True, *,
                item_texts: dict[int, str] | None = None, checked_locations=(), journal=False,
                location_rewards: dict[int, dict] | None = None) -> None:
        if self.session is None:
            return
        lease = int(time.time()) if active else 0
        lines = [f"AP1\t{self.session}\t{lease}\t{self.owner or '-'}"]
        if journal:
            lines.append("JOURNAL\t1")
        # Bounded batches keep the Lua update short, even with a large item backlog.
        for offset in range(self.acknowledged, min(len(item_codes), self.acknowledged + 32)):
            index = offset + 1
            seed = int(hashlib.sha256(f"{self.session}:{index}".encode()).hexdigest()[:8], 16)
            lines.append(f"ITEM\t{index}\t{item_codes[offset]}\t{seed}")
            if item_texts and index in item_texts:
                lines.append(f"ITEMTEXT\t{index}\t{item_texts[index].encode('utf-8').hex()}")
        for location in sorted(set(checked_locations) & {1, 2, 3}):
            lines.append(f"CHECKED\t{location}")
            reward = (location_rewards or {}).get(location)
            if reward:
                item = reward["item"].encode("utf-8").hex()
                recipient = reward["recipient"].encode("utf-8").hex()
                mine = int(bool(reward["mine"]))
                lines.append(f"RESULT\t{location}\t{item}\t{recipient}\t{mine}")
        for offset in range(self.notice_acknowledged, min(len(self.notices), self.notice_acknowledged + 8)):
            text = self.notices[offset]["text"].encode("utf-8").hex()
            lines.append(f"NOTICE\t{offset + 1}\t{text}")
        atomic_write(self.directory / "to_game.txt", "\n".join(lines + ["END", ""]))

    def deactivate(self) -> None:
        self.publish([], active=False)
