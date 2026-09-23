# Flight Check

This project runs a headless Chromium browser, opens a live China Southern
round-trip search, captures the rendered page and network responses, and asks an
OpenAI model to normalize only the observed fare evidence. It can send the
result to Telegram and store the previous result in Google Cloud Storage. It is
designed to run as a scheduled Cloud Run Job while your Mac is off or locked.

## Install on macOS

Open Terminal in this folder and run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Do not use `--break-system-packages`. The virtual environment avoids modifying
Homebrew's managed Python installation.

## Local `.env` configuration

The program automatically loads a `.env` file located beside
`flight_check.py`. Copy `.env.example` to `.env`, then replace the secret
placeholders. You do not need to run `export` commands:

```bash
cp .env.example .env
```

The included `.gitignore` excludes `.env`. Never commit or share this file.
Environment variables supplied by GCP or your shell take precedence over `.env`.

## Search without an LLM

```bash
source .venv/bin/activate
python flight_check.py \
  --origin SFO \
  --destination WUH \
  --departure 2027-01-04 \
  --return-date 2027-02-08 \
  --show-browser
```

The command saves `results/result.json`, along with the page text, HTML,
screenshot, and captured network responses. Remove `--show-browser` for
headless operation.

## Search with OpenAI normalization

Set the API key only in your shell; do not paste it into the Python file:

```bash
export OPENAI_API_KEY="your-key-here"
export OPENAI_MODEL="gpt-4o-mini"  # optional; choose a model available to you

python flight_check.py \
  --origin SFO \
  --destination WUH \
  --departure 2027-01-04 \
  --return-date 2027-02-08 \
  --show-browser \
  --use-llm
```

The OpenAI call uses Structured Outputs. If the captured evidence does not prove
a total round-trip price, the LLM is instructed to return `null` instead of
inventing a value.

## Telegram notifications

1. In Telegram, message `@BotFather`, create a bot, and copy its bot token.
2. Send one message to your new bot.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy the
   numeric `chat.id` value.
4. Export the values and run the checker:

```bash
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
export OPENAI_API_KEY="your-openai-key"

python flight_check.py \
  --origin SFO --destination WUH \
  --departure 2027-01-04 --return-date 2027-02-08 \
  --use-llm --notify --notify-mode change
```

Notification modes:

- `always`: send every successful check, including an unconfirmed price.
- `change`: send on the first confirmed price and whenever it changes.
- `drop`: send on the first confirmed price and whenever it decreases.
- `threshold`: send when the price is at or below `--price-threshold`.

Without `STATE_BUCKET`, local runs compare against
`results/previous_result.json`. Cloud Run should use a GCS bucket because its
local filesystem is temporary.

## Deploy as a scheduled GCP Cloud Run Job

Install and initialize the Google Cloud CLI, then enable billing for the
project. Create the three secrets without putting their values in shell history:

```bash
printf %s "$OPENAI_API_KEY" | gcloud secrets create openai-api-key --data-file=-
printf %s "$TELEGRAM_BOT_TOKEN" | gcloud secrets create telegram-bot-token --data-file=-
printf %s "$TELEGRAM_CHAT_ID" | gcloud secrets create telegram-chat-id --data-file=-
```

Set deployment variables and run the deployment script:

```bash
export GCP_PROJECT="your-project-id"
export GCP_REGION="us-west1"
export STATE_BUCKET="your-globally-unique-flight-check-bucket"
export FLIGHT_ORIGIN="SFO"
export FLIGHT_DESTINATION="WUH"
export FLIGHT_DEPARTURE="2027-01-04"
export FLIGHT_RETURN="2027-02-08"

chmod +x deploy_gcp.sh
./deploy_gcp.sh
gcloud run jobs execute flight-check --region "$GCP_REGION" --wait
```

The deployment script also creates or updates a Cloud Scheduler job named
`flight-check-tuesday-9am`. It runs every Tuesday at 9:00 AM in the
`America/Los_Angeles` time zone, including daylight-saving-time changes. You
can override the defaults before deployment with `SCHEDULER_JOB_NAME`,
`SCHEDULER_SCHEDULE`, or `SCHEDULER_TIME_ZONE`.

The deployed job runs headlessly. `--show-browser` is only a local debugging
option and is not used by the container.

## Search another route or country

Use three-letter IATA airport codes. For example:

```bash
python flight_check.py \
  --origin LAX \
  --destination CAN \
  --departure 2027-03-10 \
  --return-date 2027-03-25
```

## Limitations

- Airline sites can change their HTML and private APIs at any time.
- CAPTCHA, bot protection, region restrictions, cookies, or unavailable future
  schedules may prevent an automated result.
- A displayed amount may be one-way, per passenger, or a fare component. Use
  `--use-llm` to classify captured evidence, and verify the final booking page
  before purchasing.
- This project reads results only; it does not book or purchase tickets.
- OpenAI, Cloud Run, Cloud Build, Artifact Registry, Cloud Storage, Secret
  Manager, and Scheduler may incur charges.
