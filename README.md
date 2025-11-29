# Platto Helpdesk Bot (Telegram + OpenAI)

English-only helpdesk bot for Upwork demos: quick FAQ replies, AI answers, auto-tagging (billing/tech/sales), optional Google Sheets logging, simple stats, and cost guards (token/timeout/retries).

## Features
- /faq — quick answers list
- /stats — per-user counters
- AI via openai.ChatCompletion.create
- Auto-tags: billing, tech, sales
- Self-tests: FAKE_OPENAI=1 python main.py --selftest
- Cost guards via .env: MAX_TOKENS, REQUEST_TIMEOUT, RETRIES

## Requirements
- Python 3.10+

## .env example
TELEGRAM_BOT_TOKEN=your_botfather_token
OPENAI_API_KEY=sk-your-openai-key
OPENAI_MODEL=gpt-3.5-turbo
ORG_NAME=Your Company
BOT_NAME=Helpdesk Assistant

## Run tests (no external calls)
FAKE_OPENAI=1 python main.py --selftest

## Run bot
python main.py
