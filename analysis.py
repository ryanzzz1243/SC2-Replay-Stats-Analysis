from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

DEFAULT_REPLAY_PATH = Path(__file__).resolve().parent / "replays.json"
RACE_ORDER = {"Z": 0, "P": 1, "T": 2}
BARCODE_NAMES = {"IIIIIIIIIIII", "llllllllllll", "IllllllllllI"}


def load_replays(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load replay records from a JSON file."""
    replay_path = Path(path or DEFAULT_REPLAY_PATH).expanduser()
    with replay_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, list):
        raise ValueError("Replay data must be a list of replay objects")

    return payload


def is_ranked_replay(replay: dict[str, Any]) -> bool:
    """Return True for replay records that are ranked and do not include unranked players."""
    league = replay.get("league")
    if isinstance(league, str) and league == "unranked-4":
        return False

    for player_key in ("player1", "player2"):
        player = replay.get(player_key)
        if isinstance(player, dict) and player.get("league") == "unranked-4":
            return False

    return True


def iter_players(replays: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield player dictionaries from each ranked replay."""
    for replay in replays:
        if not is_ranked_replay(replay):
            continue
        for player_key in ("player1", "player2"):
            player = replay.get(player_key)
            if isinstance(player, dict):
                yield player


def count_replays(replays: list[dict[str, Any]]) -> int:
    """Return the number of replay records."""
    return len(replays)


def count_ranked_replays(replays: list[dict[str, Any]]) -> int:
    """Return the number of ranked replay records."""
    return sum(1 for replay in replays if is_ranked_replay(replay))


def count_players_by_name(replays: list[dict[str, Any]]) -> Counter[str]:
    """Count how often each player name appears across ranked replay records."""
    counts: Counter[str] = Counter()
    for player in iter_players(replays):
        name = player.get("name")
        if not isinstance(name, str):
            continue

        cleaned_name = name.strip()
        if not cleaned_name or cleaned_name in BARCODE_NAMES:
            continue

        counts[cleaned_name] += 1
    return counts


def normalize_race(race: Any) -> str | None:
    """Normalize a race value to one of Z, P, T."""
    if not isinstance(race, str):
        return None

    normalized = race.strip().lower()
    race_map = {
        "z": "Z",
        "zerg": "Z",
        "p": "P",
        "protoss": "P",
        "t": "T",
        "terran": "T",
    }
    return race_map.get(normalized)


def normalize_matchup(race_a: str | None, race_b: str | None) -> str | None:
    """Return a canonical matchup key such as ZvT or PvZ."""
    if not race_a or not race_b:
        return None

    ordered = sorted([race_a, race_b], key=lambda race: RACE_ORDER[race])
    return f"{ordered[0]}v{ordered[1]}"


def get_player_race(player: dict[str, Any] | None) -> str | None:
    """Get a normalized race for a player dictionary."""
    if not isinstance(player, dict):
        return None
    return normalize_race(player.get("race"))


def get_winner_race(replay: dict[str, Any]) -> str | None:
    """Return the normalized race of the winning player, if present."""
    for player_key in ("player1", "player2"):
        player = replay.get(player_key)
        if isinstance(player, dict) and player.get("winner") is True:
            race = get_player_race(player)
            if race:
                return race
    return None


def get_matchup_key(replay: dict[str, Any]) -> str | None:
    """Return the canonical matchup key for a replay."""
    player1 = replay.get("player1")
    player2 = replay.get("player2")
    return normalize_matchup(get_player_race(player1), get_player_race(player2))


def count_matchups(replays: list[dict[str, Any]]) -> Counter[str]:
    """Count how many ranked replays happened for each matchup."""
    counts: Counter[str] = Counter()
    for replay in replays:
        if not is_ranked_replay(replay):
            continue
        matchup = get_matchup_key(replay)
        if matchup:
            counts[matchup] += 1
    return counts


def calculate_matchup_win_rates(replays: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Calculate win rates for each race within each ranked matchup."""
    matchup_stats: dict[str, dict[str, int]] = {}
    for replay in replays:
        if not is_ranked_replay(replay):
            continue

        matchup = get_matchup_key(replay)
        if not matchup:
            continue

        entry = matchup_stats.setdefault(matchup, {"games": 0})
        entry["games"] += 1

        winner_race = get_winner_race(replay)
        if winner_race:
            entry[winner_race] = entry.get(winner_race, 0) + 1

    win_rates: dict[str, dict[str, float]] = {}
    for matchup, stats in matchup_stats.items():
        games = stats.get("games", 0)
        if games == 0:
            continue

        race_names = matchup.split("v")
        rates = {}
        for race in race_names:
            wins = stats.get(race, 0)
            rates[race] = wins / games
        win_rates[matchup] = rates

    return win_rates


def parse_replay_day(replay: dict[str, Any]) -> str | None:
    """Parse the replay day from the replay datetime string."""
    datetime_text = replay.get("datetime")
    if not isinstance(datetime_text, str):
        return None

    try:
        parsed = datetime.strptime(datetime_text, "%d %b, %Y %I:%M %p")
    except ValueError:
        return None

    return parsed.strftime("%Y-%m-%d")


def build_player_race_mmr_by_day(replays: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate player race MMR by day for trend analysis."""
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    all_days: set[str] = set()

    for replay in replays:
        if not is_ranked_replay(replay):
            continue

        day = parse_replay_day(replay)
        if not day:
            continue
        all_days.add(day)

        for player_key in ("player1", "player2"):
            player = replay.get(player_key)
            if not isinstance(player, dict):
                continue

            player_name = player.get("name")
            race = get_player_race(player)
            mmr = player.get("mmr")
            if not isinstance(player_name, str):
                continue

            cleaned_name = player_name.strip()
            if not cleaned_name or cleaned_name in BARCODE_NAMES:
                continue
            if not race:
                continue
            if not isinstance(mmr, int):
                continue

            grouped[(cleaned_name, race, day)].append({
                "replay_id": replay.get("view_url"),
                "mmr": mmr,
                "winner": bool(player.get("winner")),
            })

    if not all_days:
        return []

    global_start = min(all_days)
    global_end = max(all_days)
    global_start_date = datetime.strptime(global_start, "%Y-%m-%d")
    global_end_date = datetime.strptime(global_end, "%Y-%m-%d")

    rows: list[dict[str, Any]] = []
    player_day_keys = sorted(grouped)
    player_days: dict[tuple[str, str], list[str]] = defaultdict(list)
    for player_name, race, day in player_day_keys:
        player_days[(player_name, race)].append(day)

    for (player_name, race), days in player_days.items():
        sorted_days = sorted(set(days))
        first_known_day = sorted_days[0]
        first_known_entries = grouped[(player_name, race, first_known_day)]
        first_known_mmr = sum(entry["mmr"] for entry in first_known_entries) / len(first_known_entries)

        day = global_start_date
        current_mmr: float | None = None
        current_wins = 0
        current_losses = 0

        while day <= global_end_date:
            day_str = day.strftime("%Y-%m-%d")
            entries = grouped.get((player_name, race, day_str), [])

            if entries:
                mmrs = [entry["mmr"] for entry in entries]
                current_mmr = sum(mmrs) / len(mmrs)
                current_wins = sum(1 for entry in entries if entry["winner"])
                current_losses = sum(1 for entry in entries if not entry["winner"])
                replay_count = len(entries)
            else:
                replay_count = 0
                current_wins = 0
                current_losses = 0
                if current_mmr is None and day_str <= first_known_day:
                    current_mmr = first_known_mmr

            rows.append({
                "player_name": player_name,
                "race": race,
                "day": day_str,
                "replay_count": replay_count,
                "avg_mmr": current_mmr if current_mmr is not None else 0.0,
                "min_mmr": current_mmr,
                "max_mmr": current_mmr,
                "wins": current_wins,
                "losses": current_losses,
            })

            day += timedelta(days=1)

    rows.sort(key=lambda row: (row["player_name"], row["race"], row["day"]))
    return rows


def calculate_top_race_mmr_movers(
    rows: list[dict[str, Any]],
    top_n: int = 10,
    min_games: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """Identify the top player-race MMR movers by net movement."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["replay_count"] <= 0:
            continue
        groups[(row["player_name"], row["race"])].append(row)

    movers: list[dict[str, Any]] = []
    for (player_name, race), entries in groups.items():
        sorted_entries = sorted(entries, key=lambda row: row["day"])
        games = sum(row["replay_count"] for row in sorted_entries)
        if games < min_games:
            continue

        first = sorted_entries[0]
        last = sorted_entries[-1]
        total_delta = float(last["avg_mmr"]) - float(first["avg_mmr"])
        percentage_change = (total_delta / float(first["avg_mmr"])) * 100 if first["avg_mmr"] else None
        movers.append({
            "player_name": player_name,
            "race": race,
            "first_day": first["day"],
            "last_day": last["day"],
            "first_mmr": float(first["avg_mmr"]),
            "last_mmr": float(last["avg_mmr"]),
            "total_delta": total_delta,
            "percentage_change": percentage_change,
            "games": games,
        })

    movers.sort(key=lambda item: item["total_delta"], reverse=True)
    upward = movers[:top_n]
    downward = sorted(movers, key=lambda item: item["total_delta"])[:top_n]
    return {"upward": upward, "downward": downward}


def compare_patch_periods(rows: list[dict[str, Any]], patch_day: str) -> dict[str, Any]:
    """Compare average MMR before and after a patch day for each player-race group."""
    before: dict[tuple[str, str], list[float]] = defaultdict(list)
    after: dict[tuple[str, str], list[float]] = defaultdict(list)

    for row in rows:
        key = (row["player_name"], row["race"])
        if row["day"] < patch_day:
            before[key].append(float(row["avg_mmr"]))
        elif row["day"] >= patch_day:
            after[key].append(float(row["avg_mmr"]))

    comparison: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        player_name, race = key
        before_avg = sum(before[key]) / len(before[key]) if before[key] else None
        after_avg = sum(after[key]) / len(after[key]) if after[key] else None
        delta = None if before_avg is None or after_avg is None else after_avg - before_avg
        comparison[f"{player_name} ({race})"] = {
            "before_avg_mmr": before_avg,
            "after_avg_mmr": after_avg,
            "delta": delta,
        }

    return comparison


def build_replay_summary(replays: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a small data-only summary for later display or notebook use."""
    rows = build_player_race_mmr_by_day(replays)
    return {
        "replay_count": count_replays(replays),
        "ranked_replay_count": count_ranked_replays(replays),
        "player_count_by_name": count_players_by_name(replays),
        "matchup_counts": count_matchups(replays),
        "matchup_win_rates": calculate_matchup_win_rates(replays),
        "player_race_mmr_by_day": rows,
        "top_race_mmr_movers": calculate_top_race_mmr_movers(rows, top_n=10, min_games=10),
    }


def print_summary(summary: dict[str, Any], limit: int = 10) -> None:
    """Print a human-readable summary of the analysis results."""
    replay_count = summary.get("replay_count", 0)
    player_counts = summary.get("player_count_by_name", Counter())
    matchup_counts = summary.get("matchup_counts", Counter())
    matchup_win_rates = summary.get("matchup_win_rates", {})
    player_race_mmr_by_day = summary.get("player_race_mmr_by_day", [])

    print(f"Replay count: {replay_count}")
    print("Top players by appearance count:")
    for name, count in player_counts.most_common(limit):
        print(f"- {name}: {count}")

    print("\nTop matchups by replay count:")
    for matchup, count in matchup_counts.most_common(limit):
        print(f"- {matchup}: {count}")

    print("\nMatchup win rates:")
    for matchup, rates in sorted(matchup_win_rates.items()):
        if matchup[0] == matchup[-1]:
            continue

        count = matchup_counts.get(matchup, 0)
        formatted_rates = ", ".join(f"{race}: {rate:.1%}" for race, rate in rates.items())
        print(f"- {matchup}: games={count} {formatted_rates}")

    if player_race_mmr_by_day:
        print("\nDaily player-race MMR sample:")
        sample_rows = sorted(player_race_mmr_by_day, key=lambda row: row["replay_count"], reverse=True)
        for row in sample_rows[:limit]:
            print(
                f"- {row['player_name']} | {row['race']} | {row['day']} | "
                f"games={row['replay_count']} avg_mmr={row['avg_mmr']:.1f} "
                f"wins={row['wins']} losses={row['losses']}"
            )

    top_movers = summary.get("top_race_mmr_movers", {})
    upward = top_movers.get("upward", [])
    downward = top_movers.get("downward", [])
    if upward or downward:
        print("\nTop race-MMR movers:")
        if upward:
            print(f"\nTop {min(limit, len(upward))} upward movers:")
            for mover in upward[:limit]:
                change = mover["total_delta"]
                pct = mover["percentage_change"]
                pct_text = f"{pct:.1f}%" if pct is not None else "N/A"
                print(
                    f"- {mover['player_name']} ({mover['race']}): "
                    f"{mover['first_day']}->{mover['last_day']} "
                    f"{mover['first_mmr']:.1f}->{mover['last_mmr']:.1f} "
                    f"delta={change:+.1f} "
                    f"({pct_text}) "
                    f"games={mover['games']}"
                )
        if downward:
            print(f"\nTop {min(limit, len(downward))} downward movers:")
            for mover in downward[:limit]:
                change = mover["total_delta"]
                pct = mover["percentage_change"]
                pct_text = f"{pct:.1f}%" if pct is not None else "N/A"
                print(
                    f"- {mover['player_name']} ({mover['race']}): "
                    f"{mover['first_day']}->{mover['last_day']} "
                    f"{mover['first_mmr']:.1f}->{mover['last_mmr']:.1f} "
                    f"delta={change:+.1f} "
                    f"({pct_text}) "
                    f"games={mover['games']}"
                )


def main() -> None:
    """Load replay data and print an analysis summary."""
    replays = load_replays()
    summary = build_replay_summary(replays)
    print_summary(summary)


if __name__ == "__main__":
    main()
