Upwork Demo — Platto Helpdesk Bot

What it does
• Quick FAQ replies (/faq)
• AI answers with concise, step-by-step guidance
• Auto-tagging: billing / tech / sales
• Simple per-user stats (/stats)
• Optional Google Sheets logging

How to test (Telegram)
1) /start
2) Ask: "What time are your working hours?" (gets FAQ)
3) /faq
4) /stats
5) Send: "pricing" (should include [tags: billing])
6) Send: "refund" (should include [tags: billing])
7) Send: "integration error 500" (should include [tags: tech])

Tech stack
• Python 3.10, python-telegram-bot 21.4
• openai 0.28.1 (ChatCompletion API), python-dotenv 1.0.1
• Optional: gspread + OAuth service account (Sheets)
• LaunchAgent autostart on macOS + run.sh/stop.sh

Cost & resilience guards
• MAX_TOKENS, REQUEST_TIMEOUT, RETRIES via .env
• Graceful fallback on API rate/timeout errors

Customization options
• Edit FAQ in main.py (English)
• Edit TAG_RULES in main.py
• Sheets logging: set SHEETS_SERVICE_JSON + SHEETS_DOC_NAME in .env

Next steps for client
• Import existing FAQ/KB and intents
• Escalation routing + email/ticket integration
• Persistent memory (SQLite/Cloud DB)
• Docker/systemd deployment on server
