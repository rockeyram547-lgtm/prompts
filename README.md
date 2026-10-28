# Faymas prompt scraper

Scrapes prompts from https://faymas.in and saves them as CSV files grouped into
`men`, `women`, `kids`, `couple` (and `other`), 100 prompts per file.

    output/men/men_1-100.csv
    output/men/men_101-200.csv
    output/women/women_1-100.csv
    ...

## Run locally

    pip install -r requirements.txt
    python scrape_faymas.py --limit 5        # small test
    python scrape_faymas.py                  # full run

Useful flags: `--render` (real browser, needs `playwright install chromium`),
`--build-only` (rebuild CSVs from `data/prompts.jsonl`), `--group-by site`,
`--inspect <prompt-url>` (debug one page), `--flatten`, `--delay 2`.

## Run on GitHub

Actions tab -> "Scrape Faymas prompts" -> Run workflow. It also runs every Monday.
Results are committed to `data/` and `output/`.

Check the site's terms before republishing prompts publicly; they belong to their creators.
