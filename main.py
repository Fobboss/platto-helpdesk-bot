from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
import openai
from openai import error as openai_error
from telegram import Update, Message
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# -------------------- Env & Config --------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")

# Cost & resilience guards
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "512"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))  # seconds
RETRIES = int(os.getenv("RETRIES", "2"))  # additional attempts on transient errors

ORG_NAME = os.getenv("ORG_NAME", "Your Company")
BOT_NAME = os.getenv("BOT_NAME", "Helpdesk Assistant")

SHEETS_JSON = os.getenv("SHEETS_SERVICE_JSON")
SHEETS_DOC = os.getenv("SHEETS_DOC_NAME")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("helpdesk-bot")

# -------------------- Optional: Google Sheets --------------------
_gs = None
_ws = None

def _init_sheets() -> None:
    global _gs, _ws
    if not (SHEETS_JSON and SHEETS_DOC):
        log.info("Sheets logging disabled (missing creds or doc name)")
        return
    try:
        import gspread  # type: ignore
        from oauth2client.service_account import ServiceAccountCredentials  # type: ignore
        scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(SHEETS_JSON, scope)
        _gs = gspread.authorize(creds)
        try:
            _ws = _gs.open(SHEETS_DOC).sheet1
        except Exception:
            sh = _gs.create(SHEETS_DOC)
            _ws = sh.sheet1
            _ws.append_row(["ts", "user_id", "username", "text", "reply", "tokens", "latency_ms"])
        log.info("Sheets logging enabled: %s", SHEETS_DOC)
    except Exception as e:
        log.warning("Sheets init failed: %s", e)

async def log_to_sheet(ts: float, user_id: int, username: str, text: str, reply: str, tokens: int, latency_ms: int):
    if not _ws:
        return
    try:
        _ws.append_row([int(ts), str(user_id), username or "", text, reply, tokens, latency_ms])
    except Exception as e:
        log.warning("Sheets append failed: %s", e)

# -------------------- Memory --------------------
@dataclass
class MemoryItem:
    role: str
    content: str

@dataclass
class UserMemory:
    items: List[MemoryItem] = field(default_factory=list)
    max_len: int = 10
    def add(self, role: str, content: str):
        self.items.append(MemoryItem(role, content))
        if len(self.items) > self.max_len:
            self.items = self.items[-self.max_len:]
    def as_chat(self) -> List[dict]:
        return [{"role": m.role, "content": m.content} for m in self.items[-self.max_len:]]

USER_MEMORY: Dict[int, UserMemory] = {}

# -------------------- Strings (EN only) --------------------
START_TEXT = f"Hi! I'm {BOT_NAME}. Ask a question — I'll help or guide you to the next step.\nUseful commands: /help, /reset, /faq, /stats"
HELP_TEXT = "Tell me what you need. Commands: /reset — clear context; /faq — show quick answers; /stats — session stats."
RESET_TEXT = "Context and counters cleared."
FAQ_HEADER = "FAQ — quick answers:"
STATS_FMT = "Stats: you — {user} msgs, bot — {bot} msgs."
FALLBACK_TEXT = "Sorry, my AI engine is busy right now. Please try again in a minute."

# -------------------- FAQ (EN only) --------------------
FAQ: List[Tuple[str, str]] = [
    ("hours|working hours|what time", "We're open daily 9 AM – 7 PM PT."),
    ("price|pricing", "Basic support plan is $49/mo. Tell us what you need — we'll suggest an option."),
    ("refund", "For a refund, reply with your order number and a short description. We resolve most cases within 48 hours."),
]

def try_faq(text: str) -> Optional[str]:
    t = (text or "").lower()
    for pattern, answer in FAQ:
        for key in pattern.split("|"):
            if key and key in t:
                return answer
    return None

def build_faq_text() -> str:
    lines = [FAQ_HEADER + "\n"]
    for pattern, answer in FAQ:
        examples = pattern.replace("|", ", ")
        lines.append(f"• {examples} → {answer}")
    return "\n".join(lines)

# -------------------- Auto-tagging (EN only) --------------------
TAG_RULES: List[Tuple[str, List[str]]] = [
    ("billing", ["price", "pricing", "refund", "invoice", "payment"]),
    ("tech", ["error", "bug", "install", "setup", "integration", "timeout"]),
    ("sales", ["buy", "purchase", "demo", "trial"]),
]
def classify_tags(text: str) -> List[str]:
    t = (text or "").lower()
    return [tag for tag, keys in TAG_RULES if any(k in t for k in keys)]

# -------------------- System Prompt --------------------
SYSTEM_PROMPT = f"""
You are {BOT_NAME}, a concise, helpful first-line support agent for {ORG_NAME}.
Rules:
- Greet briefly, ask one clarifying question if needed.
- Offer concrete next steps. Use numbered lists for procedures.
- If you don't know, say so and propose how to find out.
- Keep answers under 8 sentences unless asked for details.
- If the user is frustrated, acknowledge and solve.
- Always reply in English.
"""

# -------------------- OpenAI Chat with guards --------------------
async def llm_reply(messages: List[dict]) -> Tuple[str, int]:
    if os.getenv("FAKE_OPENAI") == "1":
        return "[FAKE] LLM stubbed response", 0
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is required (or set FAKE_OPENAI=1 for self-tests)")

    openai.api_key = OPENAI_API_KEY

    for attempt in range(RETRIES + 1):
        try:
            start = time.perf_counter()
            resp = openai.ChatCompletion.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=0.2,
                top_p=0.9,
                max_tokens=MAX_TOKENS,
                request_timeout=REQUEST_TIMEOUT,
            )
            choice = resp["choices"][0]
            text = (choice.get("message", {}) or {}).get("content", "")
            usage = resp.get("usage", {}) or {}
            tokens = int(usage.get("total_tokens", 0))
            log.debug("LLM latency: %sms, tokens: %s", int((time.perf_counter() - start) * 1000), tokens)
            return (text or "").strip(), tokens
        except (openai_error.RateLimitError,
                openai_error.Timeout,
                openai_error.APIConnectionError,
                openai_error.ServiceUnavailableError,
                openai_error.APIError) as e:
            if attempt < RETRIES:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return FALLBACK_TEXT, 0
        except Exception as e:
            log.warning("LLM unexpected error: %s", e)
            return FALLBACK_TEXT, 0

# -------------------- Stats & Handlers --------------------
STATS: Dict[int, Dict[str, int]] = {}
def _stats_for(uid: int) -> Dict[str, int]:
    if uid not in STATS:
        STATS[uid] = {"user": 0, "bot": 0}
    return STATS[uid]

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await update.effective_message.reply_text(START_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await update.effective_message.reply_text(HELP_TEXT)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    USER_MEMORY.pop(uid, None)
    STATS.pop(uid, None)
    await update.effective_message.reply_text(RESET_TEXT)

def _get_memory(uid: int) -> UserMemory:
    if uid not in USER_MEMORY:
        USER_MEMORY[uid] = UserMemory()
    return USER_MEMORY[uid]

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message: Message = update.effective_message
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    text = message.text or ""

    st = _stats_for(uid)
    st["user"] += 1

    faq = try_faq(text)
    if faq:
        await message.reply_text(faq)
        await log_to_sheet(time.time(), uid, username, text, faq, 0, 0)
        st["bot"] += 1
        return

    tags = classify_tags(text)
    await update.effective_chat.send_action(ChatAction.TYPING)

    mem = _get_memory(uid)
    mem.add("user", text)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + mem.as_chat()
    reply, tokens = await llm_reply(messages)

    if tags:
        reply = f"[tags: {', '.join(tags)}]\n" + reply

    mem.add("assistant", reply)
    await message.reply_text(reply, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    st["bot"] += 1
    await log_to_sheet(time.time(), uid, username, text, reply, tokens, 0)

async def unknown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Text messages only for now.")

async def faq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await update.effective_message.reply_text(build_faq_text())

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = _stats_for(uid)
    await update.effective_message.reply_text(STATS_FMT.format(user=st["user"], bot=st["bot"]))

# -------------------- App --------------------
def build_app():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Missing required env var: TELEGRAM_BOT_TOKEN")
    if not OPENAI_API_KEY and os.getenv("FAKE_OPENAI") != "1":
        raise SystemExit("Missing required env var: OPENAI_API_KEY")
    _init_sheets()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("faq", faq_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(~filters.TEXT, unknown_handler))
    return app

# -------------------- Tests (EN only) --------------------
class _SelfTests:
    @staticmethod
    def run() -> int:
        import unittest
        class Tests(unittest.TestCase):
            def test_try_faq_match(self):
                self.assertEqual(
                    try_faq("What time are your working hours?"),
                    "We're open daily 9 AM – 7 PM PT.",
                )
            def test_try_faq_nomatch(self):
                self.assertIsNone(try_faq("how do I reset password"))
            def test_user_memory_trim(self):
                mem = UserMemory(max_len=3)
                for i in range(5):
                    mem.add("user", f"m{i}")
                self.assertEqual([m.content for m in mem.items], ["m2", "m3", "m4"])
                chat = mem.as_chat()
                self.assertEqual(len(chat), 3)
                self.assertEqual(chat[0]["content"], "m2")
            def test_llm_fake(self):
                os.environ["FAKE_OPENAI"] = "1"
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    text, tokens = loop.run_until_complete(
                        llm_reply([
                            {"role": "system", "content": "sys"},
                            {"role": "user", "content": "ping"},
                        ])
                    )
                    self.assertTrue(text.startswith("[FAKE] "))
                    self.assertIsInstance(tokens, int)
                finally:
                    loop.close()
            def test_build_faq_text(self):
                s = build_faq_text()
                self.assertIn("FAQ — quick answers", s)
                self.assertIn("working hours", s)
            def test_classify_tags(self):
                self.assertIn("billing", classify_tags("I need a refund for my invoice"))
                self.assertIn("tech", classify_tags("integration error 500"))
                self.assertEqual([], classify_tags("hello"))
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

# -------------------- Entry --------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true", help="run built-in unit tests and exit")
    args = parser.parse_args()
    if args.selftest:
        code = _SelfTests.run()
        raise SystemExit(code)
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)
