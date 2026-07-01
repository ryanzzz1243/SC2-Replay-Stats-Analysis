import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
import os
import re
import time
from datetime import datetime, timedelta, timezone
from threading import Event, Lock
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
CACHE_PAGE_RE = re.compile(r'^replays_page_(\d+)\.html$')


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


def fetch_page_html(
    page: int,
    use_cache: bool,
    retries: int,
    stop_event: Event | None = None,
    failure_state: dict | None = None,
) -> str | None:
    page_params = dict(params)
    page_params['page'] = page
    cache_file = cache_dir / f"replays_page_{page}.html"

    def reserve_attempt() -> bool:
        if failure_state is None:
            return True

        failure_lock = failure_state['lock']
        with failure_lock:
            remaining_budget = failure_state['limit'] - failure_state['failures'] - failure_state['reserved']
            if remaining_budget <= 0:
                if stop_event is not None:
                    stop_event.set()
                return False

            failure_state['reserved'] += 1
            return True

    def release_attempt(failed: bool) -> int:
        if failure_state is None:
            return 0

        failure_lock = failure_state['lock']
        with failure_lock:
            failure_state['reserved'] -= 1
            if failed:
                failure_state['failures'] += 1
            failure_count = failure_state['failures']

        if stop_event is not None and failure_count >= failure_state['limit']:
            stop_event.set()

        return failure_count

    def record_failure() -> int:
        if failure_state is None:
            return 0

        failure_lock = failure_state['lock']
        with failure_lock:
            failure_state['failures'] += 1
            failure_count = failure_state['failures']

        if stop_event is not None and failure_count >= failure_state['limit']:
            stop_event.set()

        return failure_count

    if stop_event is not None and stop_event.is_set():
        print(f"page={page} skipped because the failure limit was reached")
        return None

    if use_cache:
        if not cache_file.exists():
            print(f"page={page} missing cache file: {cache_file}")
            record_failure()
            return None

        print(f"page={page} loading from cache")
        return cache_file.read_text(encoding='utf-8')

    if not use_cache or not cache_file.exists():
        attempts = retries + 1
        for attempt in range(1, attempts + 1):
            if stop_event is not None and stop_event.is_set():
                print(f"page={page} stopped before attempt={attempt}/{attempts}")
                return None

            if not reserve_attempt():
                print(f"page={page} stopped before attempt={attempt}/{attempts}")
                return None

            try:
                response = requests.get(url, headers=headers, params=page_params, timeout=30)
                print(f"page={page} status={response.status_code} attempt={attempt}/{attempts}")
                if response.status_code == 200:
                    release_attempt(False)
                    page_html = response.text
                    cache_file.write_text(page_html, encoding='utf-8')
                    return page_html
                release_attempt(True)
            except requests.RequestException as exc:
                print(f"page={page} request error attempt={attempt}/{attempts}: {exc}")
                release_attempt(True)

            if attempt < attempts:
                if stop_event is not None and stop_event.is_set():
                    print(f"page={page} stopped after attempt={attempt}/{attempts}")
                    return None
                backoff_seconds = 0.75 * (2 ** (attempt - 1))
                time.sleep(backoff_seconds)

        print(f"page={page} failed after {retries} retries")
        return None


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


def load_cached_page(page: int, cache_file: Path):
    try:
        page_html = cache_file.read_text(encoding='utf-8')
    except OSError as exc:
        print(f"page={page} failed to read cache file {cache_file}: {exc}")
        return None

    return parse_page(page, page_html)


def main() -> None:
    args = parse_args()
    args.max_pages = max(0, args.max_pages)
    args.workers = max(1, args.workers)
    args.retries = min(2, max(0, args.retries))
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=args.cutoff_days)
    all_replays = []
    parsed_pages = {}

    if args.use_cache:
        cached_pages = []
        for cache_file in cache_dir.glob('replays_page_*.html'):
            match = CACHE_PAGE_RE.match(cache_file.name)
            if match is None:
                continue
            cached_pages.append((int(match.group(1)), cache_file))

        cached_pages.sort(key=lambda item: item[0])
        if args.max_pages > 0:
            cached_pages = [item for item in cached_pages if item[0] < args.max_pages]

        if not cached_pages:
            print(f"Stopping: no cached replay pages found in {cache_dir}")
            return

        cache_workers = min(len(cached_pages), max(1, os.cpu_count() or args.workers, args.workers))
        executor = ProcessPoolExecutor(max_workers=cache_workers)
        try:
            future_to_page = {
                executor.submit(load_cached_page, page, cache_file): page
                for page, cache_file in cached_pages
            }

            for future in as_completed(future_to_page):
                page = future_to_page[future]
                try:
                    page_data = future.result()
                except KeyboardInterrupt:
                    print('Stopping: interrupted by user')
                    return
                except Exception as exc:
                    print(f"page={page} unexpected error: {exc}")
                    continue

                if page_data is not None:
                    parsed_pages[page] = page_data
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    else:
        failure_state = {
            'failures': 0,
            'reserved': 0,
            'limit': 15,
            'lock': Lock(),
        }
        stop_event = Event()

        first_page_html = fetch_page_html(0, args.use_cache, args.retries, stop_event, failure_state)
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

        parsed_pages[0] = parse_page(0, first_page_html)

        remaining_pages = iter(range(1, total_pages))
        if total_pages > 1:
            executor = ThreadPoolExecutor(max_workers=args.workers)
            try:
                future_to_page = {}

                def submit_next_page() -> bool:
                    if stop_event.is_set():
                        return False

                    try:
                        page = next(remaining_pages)
                    except StopIteration:
                        return False

                    future = executor.submit(fetch_page_html, page, args.use_cache, args.retries, stop_event, failure_state)
                    future_to_page[future] = page
                    return True

                for _ in range(args.workers):
                    if not submit_next_page():
                        break

                while future_to_page:
                    done, _ = wait(tuple(future_to_page.keys()), return_when=FIRST_COMPLETED)

                    for future in done:
                        page = future_to_page.pop(future)
                        try:
                            page_html = future.result()
                        except KeyboardInterrupt:
                            print('Stopping: interrupted by user')
                            return
                        except Exception as exc:
                            print(f"page={page} unexpected error: {exc}")
                            page_html = None

                        if page_html is None:
                            if stop_event.is_set():
                                print(f"Stopping: reached {failure_state['failures']} total fetch failures")
                                for pending_future in list(future_to_page):
                                    pending_future.cancel()
                                future_to_page.clear()
                                break
                            continue

                        parsed_pages[page] = parse_page(page, page_html)

                        while len(future_to_page) < args.workers and submit_next_page():
                            pass

                    if stop_event.is_set():
                        break
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

    for page in sorted(parsed_pages):
        page_data = parsed_pages[page]
        if page_data['rows'] == 0:
            print(f"Stopping: no rows found on page {page}")
            break

        all_replays.extend(page_data['replays'])
        print(f"page={page} parsed {page_data['rows']} rows, total {len(all_replays)}")

        page_oldest = page_data['oldest']
        if (not args.use_cache) and page_oldest is not None and page_oldest < cutoff_date:
            print(f"Stopping: oldest replay on page {page} is {page_oldest.isoformat()}, older than cutoff {cutoff_date.isoformat()}")
            break

    print(f"Parsed {len(all_replays)} total replay rows")

    with open("replays.json", "w", encoding="utf-8") as f:
        json.dump(all_replays, f, indent=2, ensure_ascii=False)

    print("Saved structured data to replays.json")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print('Stopping: interrupted by user')