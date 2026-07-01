import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import time
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
    "min_mmr": 100,
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
        default=0,
        help='Maximum number of result pages to process (0 means no limit)',
    )
    parser.add_argument(
        '--cutoff-days',
        type=int,
        default=28,
        help='Stop when the oldest replay on a page is older than this many days',
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=12,
        help='Number of worker threads used to fetch pages after page 0',
    )
    parser.add_argument(
        '--retries',
        type=int,
        default=2,
        help='Number of retries for failed page requests (max 2)',
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

    player1 = parse_player_cell(cells[1])
    player2 = parse_player_cell(cells[2])
    if player1.get('mmr') == 0 or player2.get('mmr') == 0:
        return None

    date_cell = cells[0]
    replay = {
        'datetime': date_cell.get_text(strip=True),
        'player1': player1,
        'player2': player2,
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


def fetch_page_html(page: int, use_cache: bool, retries: int) -> str | None:
    page_params = dict(params)
    page_params['page'] = page
    cache_file = cache_dir / f"replays_page_{page}.html"

    if not use_cache or not cache_file.exists():
        attempts = retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(url, headers=headers, params=page_params, timeout=30)
                print(f"page={page} status={response.status_code} attempt={attempt}/{attempts}")
                if response.status_code == 200:
                    page_html = response.text
                    cache_file.write_text(page_html, encoding='utf-8')
                    return page_html
            except requests.RequestException as exc:
                print(f"page={page} request error attempt={attempt}/{attempts}: {exc}")

            if attempt < attempts:
                backoff_seconds = 0.75 * (2 ** (attempt - 1))
                time.sleep(backoff_seconds)

        print(f"page={page} failed after {retries} retries")
        return None

    print(f"page={page} loading from cache")
    return cache_file.read_text(encoding='utf-8')


def parse_total_replays(soup: BeautifulSoup) -> int | None:
    total_span = soup.select_one('section.sc2-panel.results-panel .results-toolbar span strong')
    if not total_span:
        return None
    try:
        return int(total_span.get_text(strip=True).replace(',', ''))
    except ValueError:
        return None


def parse_page(page: int, page_html: str):
    soup = BeautifulSoup(page_html, 'html.parser')
    page_rows = soup.select('section.sc2-panel.results-panel table.table tbody tr')
    if not page_rows:
        return {
            'page': page,
            'rows': 0,
            'replays': [],
            'oldest': None,
        }

    page_replays = []
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

        page_replays.append(replay_data)

    return {
        'page': page,
        'rows': len(page_rows),
        'replays': page_replays,
        'oldest': page_oldest,
    }


def main() -> None:
    args = parse_args()
    args.max_pages = max(0, args.max_pages)
    args.workers = max(1, args.workers)
    args.retries = min(2, max(0, args.retries))
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=args.cutoff_days)
    all_replays = []

    first_page_html = fetch_page_html(0, args.use_cache, args.retries)
    if first_page_html is None:
        print('Stopping: unable to fetch page 0')
        return

    first_page_soup = BeautifulSoup(first_page_html, 'html.parser')
    total_replays = parse_total_replays(first_page_soup)
    if total_replays is None:
        print('Stopping: unable to determine total replay count from page 0')
        return

    total_pages = (total_replays + 49) // 50
    if args.max_pages > 0:
        total_pages = min(total_pages, args.max_pages)

    print(f"total_replays={total_replays} total_pages={total_pages}")

    parsed_pages = {}
    parsed_pages[0] = parse_page(0, first_page_html)

    remaining_pages = list(range(1, total_pages))
    if remaining_pages:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_page = {
                executor.submit(fetch_page_html, page, args.use_cache, args.retries): page for page in remaining_pages
            }
            for future in as_completed(future_to_page):
                page = future_to_page[future]
                page_html = future.result()
                if page_html is None:
                    continue
                parsed_pages[page] = parse_page(page, page_html)

    for page in sorted(parsed_pages):
        page_data = parsed_pages[page]
        if page_data['rows'] == 0:
            print(f"Stopping: no rows found on page {page}")
            break

        all_replays.extend(page_data['replays'])
        print(f"page={page} parsed {page_data['rows']} rows, total {len(all_replays)}")

        page_oldest = page_data['oldest']
        if page_oldest is not None and page_oldest < cutoff_date:
            print(f"Stopping: oldest replay on page {page} is {page_oldest.isoformat()}, older than cutoff {cutoff_date.isoformat()}")
            break

    print(f"Parsed {len(all_replays)} total replay rows")

    with open("replays.json", "w", encoding="utf-8") as f:
        json.dump(all_replays, f, indent=2, ensure_ascii=False)

    print("Saved structured data to replays.json")


if __name__ == "__main__":
    main()