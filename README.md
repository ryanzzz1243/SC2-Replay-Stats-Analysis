# SC2 Replay Scraper

This project scrapes replay data from sc2replaystats and saves it as JSON.

## Setup

1. Create a virtual environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy the example environment file and fill in your values:

   ```powershell
   Copy-Item .env.example .env
   ```

3. Edit [.env](.env) and set:

   ```env
   SC2REPLAY_API_KEY=your_api_key_here
   SC2REPLAY_COOKIE=your_cookie_header_value_here
   ```

## Run

```powershell
python sc2replaystats_replay_scraper.py
```

The script will write the parsed data to [replays.json](replays.json).

## Notes

- Do not commit your real [.env](.env) file.
- The repository ignores [.env](.env) and generated replay pages.
- If you want to avoid installing dependencies manually, you can use the package manager of your choice to install the required libraries listed in [requirements.txt](requirements.txt).
