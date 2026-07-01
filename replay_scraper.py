import argparse
import os
import re
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
import json
from pathlib import Path
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent / '.env')

API_KEY = os.getenv('SC2REPLAY_API_KEY', '').strip()
COOKIE = os.getenv('SC2REPLAY_COOKIE', '').strip()

if not API_KEY or not COOKIE:
    raise SystemExit('Set SC2REPLAY_API_KEY and SC2REPLAY_COOKIE before running this script.')

url = 'https://sc2replaystats.com/search'

headers = {
    'User-Agent': 'insomnia/13.0.2',
    'Connection': 'keep-alive',
    'Accept-Encoding': 'gzip, deflate, br',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Authorization': API_KEY,
    'Cookie': COOKIE,
}

params = {
    "format": "1v1",
    "game_type": "AutoMM",
    "matchup": "-",
    "map": "-",
    "division": "-",
    "server": "-",
    "players_name": "",
    "min_mmr": 2000,
    "max_mmr": "",
    "min_game_length": "-",
    "max_game_length": "-",
    "season": "67",
    "page": 0,
}

cache_dir = Path("replay_pages")
cache_dir.mkdir(exist_ok=True)

PLAYER_MMR_RE = re.compile(r'(?P<name>.*) \((?P<mmr>\d+)\)')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Scrape SC2 replay data from sc2replaystats.com')
    parser.add_argument(
        '--use-cache',
        action='store_true',
        help='Use cached HTML files when available instead of fetching new pages',
    )
    parser.add_argument(
        '--max-pages',
        type=int,
        default=400,
        help='Maximum number of result pages to process',
    )
    parser.add_argument(
        '--cutoff-days',
        type=int,
        default=28,
        help='Stop when the oldest replay on a page is older than this many days',
    )
    return parser.parse_args()

def parse_player_cell(cell):
    player_name_span = cell.find('span', class_='player-name')
    raw_text = player_name_span.get_text(' ', strip=True) if player_name_span else cell.get_text(' ', strip=True)
    winner = bool(cell.find('span', class_=lambda value: value and 'fa-trophy' in value))

    race = None
    league = None
    for span in cell.find_all('span'):
        classes = span.get('class', [])
        if 'races' in classes:
            for part in classes:
                if part not in ('races', 'size-16'):
                    race = part
        if 'leagues' in classes:
            for part in classes:
                if part not in ('leagues', 'size-16'):
                    league = part.replace('league-', '')

    name = raw_text
    mmr = None
    mmr_match = PLAYER_MMR_RE.search(raw_text)
    if mmr_match:
        name = mmr_match.group('name').strip()
        mmr_text = mmr_match.group('mmr').replace(',', '')
        mmr = int(mmr_text)

    return {
        'name': name,
        'race': race,
        'league': league,
        'mmr': mmr,
        'winner': winner,
    }


def parse_row(row):
    cells = row.find_all('td')
    if len(cells) < 5:
        return None

    date_cell = cells[0]
    replay = {
        'datetime': date_cell.get_text(strip=True),
        'player1': parse_player_cell(cells[1]),
        'player2': parse_player_cell(cells[2]),
        'duration': cells[3].get_text(strip=True),
        'view_url': None,
        'download_url': None,
    }

    view_link = cells[4].find('a')
    if view_link and view_link.has_attr('href'):
        replay['view_url'] = view_link['href']

    if len(cells) > 5:
        download_link = cells[5].find('a')
        if download_link and download_link.has_attr('href'):
            replay['download_url'] = download_link['href']

    return replay


def main() -> None:
    args = parse_args()
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=args.cutoff_days)
    all_replays = []

    for page in range(args.max_pages):
        params['page'] = page
        cache_file = cache_dir / f"replays_page_{page}.html"

        if not args.use_cache or not cache_file.exists():
            response = requests.get(url, headers=headers, params=params)
            print(f"page={page} status={response.status_code}")
            if response.status_code != 200:
                print(f"Stopping: HTTP {response.status_code}")
                break
            page_html = response.text
            cache_file.write_text(page_html, encoding='utf-8')
        else:
            print(f"page={page} loading from cache")
            page_html = cache_file.read_text(encoding='utf-8')

        soup = BeautifulSoup(page_html, 'html.parser')
        if page == 0 and not args.use_cache:
            total_span = soup.select_one('section.sc2-panel.results-panel .results-toolbar span strong')
            if total_span:
                try:
                    total_replays = int(total_span.get_text(strip=True).replace(',', ''))
                    print(f"total_replays={total_replays}")
                except ValueError:
                    total_replays = None

        page_rows = soup.select('section.sc2-panel.results-panel table.table tbody tr')
        if not page_rows:
            print(f"Stopping: no rows found on page {page}")
            break

        page_oldest = None
        for row in page_rows:
            replay_data = parse_row(row)
            if not replay_data:
                continue

            replay_date = None
            replay_date_text = replay_data.get('datetime') or replay_data.get('date')
            try:
                replay_date = datetime.strptime(replay_date_text, '%d %b, %Y %I:%M %p').replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

            if replay_date is not None:
                if page_oldest is None or replay_date < page_oldest:
                    page_oldest = replay_date

            all_replays.append(replay_data)

        print(f"page={page} parsed {len(page_rows)} rows, total {len(all_replays)}")

        if page_oldest is not None and page_oldest < cutoff_date:
            print(f"Stopping: oldest replay on page {page} is {page_oldest.isoformat()}, older than cutoff {cutoff_date.isoformat()}")
            break

    print(f"Parsed {len(all_replays)} total replay rows")

    with open("replays.json", "w", encoding="utf-8") as f:
        json.dump(all_replays, f, indent=2, ensure_ascii=False)

    print("Saved structured data to replays.json")


if __name__ == "__main__":
    main()