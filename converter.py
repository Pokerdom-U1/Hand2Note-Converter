#!/usr/bin/env python3
import argparse
import dataclasses
import pathlib
import re
import sys
from datetime import datetime
from typing import List, Dict, Optional, Tuple

# --------- Data models ---------
@dataclasses.dataclass
class Seat:
    num: int
    name: str
    stack: Optional[float] = None

@dataclasses.dataclass
class Action:
    street: str  # PREFLOP/FLOP/TURN/RIVER/SHOWDOWN/SUMMARY
    actor: str
    verb: str      # folds/calls/raises/bets/checks/collected/...
    amount: Optional[float] = None
    to_amount: Optional[float] = None
    cards: Optional[str] = None
    raw: str = ""  # original line for safety

@dataclasses.dataclass
class Hand:
    site: str = "Pokerdom"
    hand_id: Optional[str] = None
    game: str = "Hold'em No Limit"
    stakes: Tuple[float, float] = (0.0, 0.0)
    currency: str = "$"
    date_utc: Optional[datetime] = None
    table_name: Optional[str] = None
    max_players: Optional[int] = None
    button_seat: Optional[int] = None
    seats: List[Seat] = dataclasses.field(default_factory=list)
    blinds: Dict[str, Tuple[str, float]] = dataclasses.field(default_factory=dict)  # {'SB': (name, 0.1), 'BB': (...)}
    hero: Optional[str] = None
    hero_hole: Optional[str] = None  # like "Ah Kh"
    board: Dict[str, str] = dataclasses.field(default_factory=dict)  # FLOP/TURN/RIVER -> "[...]"
    actions: List[Action] = dataclasses.field(default_factory=list)
    pots: Dict[str, float] = dataclasses.field(default_factory=dict)  # main/side etc.

# --------- Utilities ---------
CURRENCY_SIGNS = {"$": "$", "€": "€", "£": "£", "¥": "¥"}

def parse_money(token: str) -> Tuple[str, float]:
    token = token.strip()
    m = re.match(r"(?P<cur>[$€£¥]?)[ ]?(?P<num>-?\d+(?:\.\d+)?)", token)
    if not m:
        raise ValueError(f"Could not parse money: {token}")
    cur = m.group("cur") or "$"
    return cur, float(m.group("num"))

def safe_float(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except Exception:
        return default

def normalize_name(name: str) -> str:
    return name.strip().rstrip(":")

# --------- Pokerdom parser (на базе прежней логики) ---------
class PokerdomParser:
    # Regex-паттерны остались прежними (заточены под типичный формат).
    PATTERNS = {
        "hand_header": re.compile(
            r"^Game\s*[#:]?\s*(?P<hid>\d+).*(Hold'em|Holdem).*(?P<sb>\d+(?:\.\d+)?)[/ ](?P<bb>\d+(?:\.\d+)?).*(?P<date>\d{4}[-/]\d{2}[-/]\d{2}.*\d{2}:\d{2}:\d{2})",
            re.IGNORECASE),
        "alt_header": re.compile(
            r"^(?P<date>\d{4}[./-]\d{2}[./-]\d{2}).*?-\s*(?P<game>Hold'em|Holdem).*(?P<sb>\d+(?:\.\d+)?)[^\d](?P<bb>\d+(?:\.\d+)?)",
            re.IGNORECASE),
        "table_btn": re.compile(r"^Table\s+'?(?P<table>[^']+)'?.*Seat\s+#(?P<button>\d+)", re.IGNORECASE),
        "seat": re.compile(r"^Seat\s+(?P<num>\d+):\s+(?P<name>.+?)\s+\((?P<cur>[$€£¥]?)(?P<stack>\d+(?:\.\d+)?)\)", re.IGNORECASE),
        "posts": re.compile(r"^(?P<name>.+?):\s+posts\s+(?P<which>small blind|big blind)\s+(?P<cur>[$€£¥]?)(?P<amt>\d+(?:\.\d+)?)", re.IGNORECASE),
        "dealt": re.compile(r"^Dealt to\s+(?P<name>.+?)\s+\[(?P<cards>[2-9TJQKA][cdhs]\s+[2-9TJQKA][cdhs])\]", re.IGNORECASE),
        "street": re.compile(r"^\*{3}\s+(?P<street>HOLE CARDS|FLOP|TURN|RIVER|SHOW DOWN|SHOWDOWN|SUMMARY)\s+\*{3}(?:\s+\[(?P<board>[^\]]+)\])?", re.IGNORECASE),
        "action": re.compile(r"^(?P<name>.+?):\s+(?P<verb>folds|calls|checks|bets|raises to|raises|collected)(?:\s+(?P<cur>[$€£¥]?)(?P<amt>\d+(?:\.\d+)?))?", re.IGNORECASE),
        "show": re.compile(r"^(?P<name>.+?)\s+showed\s+\[(?P<cards>[^\]]+)\]", re.IGNORECASE),
        "collected": re.compile(r"^(?P<name>.+?)\s+collected\s+(?P<cur>[$€£¥]?)(?P<amt>\d+(?:\.\d+)?)", re.IGNORECASE),
        "max_players": re.compile(r"(-|—)\s*(?P<max>[0-9])max", re.IGNORECASE),
    }

    def parse(self, text: str) -> List['Hand']:
        # Делим на раздачи по эвристике: блоки с *** HOLE CARDS ***
        chunks: List[str] = []
        buf: List[str] = []
        for ln in text.splitlines():
            if ln.strip() == "" and buf and any("*** HOLE CARDS ***" in b for b in buf):
                chunks.append("\n".join(buf).strip())
                buf = []
            else:
                buf.append(ln)
        if buf:
            chunks.append("\n".join(buf).strip())

        hands: List[Hand] = []
        for chunk in chunks:
            h = self._parse_one(chunk)
            if h:
                hands.append(h)
        return hands

    def _parse_one(self, hand_text: str) -> Optional['Hand']:
        h = Hand()
        lines = [l.rstrip() for l in hand_text.splitlines() if l.strip() != ""]
        if not lines:
            return None

        # Заголовок раздачи
        m = self.PATTERNS["hand_header"].search(lines[0])
        if not m:
            m = self.PATTERNS["alt_header"].search(lines[0])
        if m:
            h.hand_id = m.groupdict().get("hid")
            sb = m.group("sb")
            bb = m.group("bb")
            h.stakes = (safe_float(sb), safe_float(bb))
            date_raw = m.group("date")
            h.date_utc = self._try_parse_dt(date_raw)
            if "game" in m.groupdict() and m.group("game"):
                h.game = "Hold'em No Limit"

        # Стол/баттон/макс-игроков
        for ln in lines[:8]:
            tm = self.PATTERNS["table_btn"].search(ln)
            if tm:
                h.table_name = tm.group("table").strip()
                h.button_seat = int(tm.group("button"))
            mm = self.PATTERNS["max_players"].search(ln)
            if mm:
                h.max_players = int(mm.group("max"))

        # Сиденья
        for ln in lines:
            sm = self.PATTERNS["seat"].search(ln)
            if sm:
                h.seats.append(Seat(int(sm.group("num")), normalize_name(sm.group("name")), safe_float(sm.group("stack"))))

        # Блайнды
        for ln in lines:
            pm = self.PATTERNS["posts"].search(ln)
            if pm:
                which = pm.group("which").lower()
                name = normalize_name(pm.group("name"))
                h.currency = pm.group("cur") or h.currency
                amt = safe_float(pm.group("amt"))
                if "small" in which:
                    h.blinds["SB"] = (name, amt)
                else:
                    h.blinds["BB"] = (name, amt)

        # Карты героя
        for ln in lines:
            dm = self.PATTERNS["dealt"].search(ln)
            if dm:
                h.hero = normalize_name(dm.group("name"))
                h.hero_hole = dm.group("cards").strip()

        # Улицы и действия
        street = "PREFLOP"
        for ln in lines:
            st = self.PATTERNS["street"].search(ln)
            if st:
                tag = st.group("street").upper()
                if "FLOP" in tag:
                    street = "FLOP"
                elif "TURN" in tag:
                    street = "TURN"
                elif "RIVER" in tag:
                    street = "RIVER"
                elif "SHOW" in tag:
                    street = "SHOWDOWN"
                elif "SUMMARY" in tag:
                    street = "SUMMARY"
                else:
                    street = "PREFLOP"
                if st.group("board"):
                    h.board[street] = f"[{st.group('board').strip()}]"
                continue

            am = self.PATTERNS["action"].search(ln)
            if am:
                name = normalize_name(am.group("name"))
                verb = am.group("verb").lower()
                amt = safe_float(am.group("amt")) if am.group("amt") else None
                if "raises to" in verb:
                    verb = "raises"
                h.actions.append(Action(street, name, verb, amount=amt, raw=ln))
                continue

            cm = self.PATTERNS["collected"].search(ln)
            if cm:
                name = normalize_name(cm.group("name"))
                amt = safe_float(cm.group("amt"))
                h.actions.append(Action("SUMMARY", name, "collected", amount=amt, raw=ln))
                continue

            sh = self.PATTERNS["show"].search(ln)
            if sh:
                name = normalize_name(sh.group("name"))
                cards = sh.group("cards")
                h.actions.append(Action("SHOWDOWN", name, "showed", cards=cards, raw=ln))
                continue

        return h

    @staticmethod
    def _try_parse_dt(s: str) -> Optional[datetime]:
        fmts = [
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y.%m.%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S %Z",
            "%Y/%m/%d %H:%M:%S %Z",
        ]
        for f in fmts:
            try:
                return datetime.strptime(s, f)
            except Exception:
                pass
        return None

# --------- PokerStars writer (basic) ---------
class PokerStarsWriter:
    def write(self, hand: Hand) -> str:
        cur = CURRENCY_SIGNS.get(hand.currency, "$")
        sb, bb = hand.stakes
        stakes = f"({cur}{sb:.2f}/{cur}{bb:.2f})" if bb else ""
        dt = hand.date_utc.strftime("%Y/%m/%d %H:%M:%S ET") if hand.date_utc else ""
        hid = hand.hand_id or "0000000000"
        hdr = f"PokerStars Hand #{hid}: {hand.game} {stakes} - {dt}".strip()

        table = hand.table_name or "Pokerdom Table"
        maxp = f"{hand.max_players}-max" if hand.max_players else ""
        btn = f"Seat #{hand.button_seat}" if hand.button_seat else "Seat #1"

        out = [hdr, f"Table '{table}' {maxp} {btn}"]
        # Seats
        for s in sorted(hand.seats, key=lambda x: x.num):
            stack = f"{cur}{s.stack:.2f}" if s.stack is not None else ""
            out.append(f"Seat {s.num}: {s.name} ({stack} in chips)")

        # Blinds
        if "SB" in hand.blinds:
            name, amt = hand.blinds["SB"]
            out.append(f"{name}: posts small blind {cur}{amt:.2f}")
        if "BB" in hand.blinds:
            name, amt = hand.blinds["BB"]
            out.append(f"{name}: posts big blind {cur}{amt:.2f}")

        # Hole cards
        out.append("*** HOLE CARDS ***")
        if hand.hero and hand.hero_hole:
            out.append(f"Dealt to {hand.hero} [{hand.hero_hole}]")

        # Actions by street
        def dump_street(tag: str, label: str):
            street_actions = [a for a in hand.actions if a.street == tag]
            if not street_actions and tag != "PREFLOP":
                return
            if tag == "PREFLOP":
                pass
            else:
                board = hand.board.get(tag, "")
                lab = f"*** {label} ***" if not board else f"*** {label} {board} ***"
                out.append(lab)
            for a in street_actions:
                line = self._format_action(a, cur)
                if line:
                    out.append(line)

        dump_street("PREFLOP", "HOLE CARDS")
        dump_street("FLOP", "FLOP")
        dump_street("TURN", "TURN")
        dump_street("RIVER", "RIVER")

        # Showdown
        sd = [a for a in hand.actions if a.street == "SHOWDOWN"]
        if sd:
            out.append("*** SHOW DOWN ***")
            for a in sd:
                if a.verb == "showed" and a.cards:
                    out.append(f"{a.actor}: shows [{a.cards}]")

        # Summary / collected
        coll = [a for a in hand.actions if a.verb == "collected"]
        if coll:
            out.append("*** SUMMARY ***")
            for a in coll:
                out.append(f"{a.actor} collected {cur}{a.amount:.2f}")

        return "\n".join(out) + "\n"

    @staticmethod
    def _format_action(a: Action, cur: str) -> Optional[str]:
        if a.verb in ("folds", "checks"):
            return f"{a.actor}: {a.verb}"
        if a.verb in ("calls", "bets"):
            if a.amount is not None:
                return f"{a.actor}: {a.verb} {cur}{a.amount:.2f}"
            return f"{a.actor}: {a.verb}"
        if a.verb == "raises":
            if a.amount is not None:
                return f"{a.actor}: raises to {cur}{a.amount:.2f}"
            return f"{a.actor}: raises"
        if a.verb == "collected":
            return None  # handled in summary
        # Fallback to raw line as a comment
        return f"# {a.raw}"

# --------- CLI ---------
def convert_file(path: pathlib.Path, writer: "PokerStarsWriter") -> List[str]:
    txt = path.read_text(encoding="utf-8", errors="ignore")
    parser = PokerdomParser()
    hands = parser.parse(txt)
    return [writer.write(h) for h in hands]

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Convert Pokerdom hand histories to other formats.")
    ap.add_argument("target", choices=["pokerstars"], help="Target format. (Currently only 'pokerstars' is implemented.)")
    ap.add_argument("input", help="Input .txt file or directory with Pokerdom histories")
    ap.add_argument("--out", "-o", help="Output directory (created if missing). Defaults to ./converted", default="converted")
    args = ap.parse_args(argv)

    in_path = pathlib.Path(args.input)
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    writer = PokerStarsWriter()

    if in_path.is_file():
        files = [in_path]
    else:
        files = [p for p in in_path.rglob("*.txt")]

    total_hands = 0
    for f in files:
        try:
            outputs = convert_file(f, writer)
            if not outputs:
                out_path = out_dir / (f.stem + ".ps.txt")
                out_path.write_text("# No hands parsed in this file. Keep the original for inspection.\n", encoding="utf-8")
                continue
            total_hands += len(outputs)
            out_path = out_dir / (f.stem + ".ps.txt")
            out_path.write_text("\n".join(outputs), encoding="utf-8")
        except Exception as e:
            out_path = out_dir / (f.stem + ".error.txt")
            out_path.write_text(f"# Error converting {f.name}: {e}\n", encoding="utf-8")

    print(f"Converted {total_hands} hands from {len(files)} files into '{out_dir}'.")
    return 0

if __name__ == "__main__":
    sys.exit(main())