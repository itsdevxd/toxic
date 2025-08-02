import os
import logging
import tempfile
import random
from datetime import datetime
import config
import pytz
import requests
from gtts import gTTS
from langdetect import detect
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.helpers import escape_markdown
from config import BANNED_USERS, BOT_TOKEN


GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=AIzaSyAfO5_KYLBcpz14JBEt-gxgggX56dCIyrQ"

logging.basicConfig(level=logging.INFO)

def ask_gemini(question: str) -> str:
    data = {
        "contents": [{"parts": [{"text": question}]}],
        "generationConfig": {"maxOutputTokens": 300}
    }
    response = requests.post(GEMINI_API_URL, json=data)
    response.raise_for_status()
    text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    return text.strip()

def text_to_ogg(text: str, lang: str = "hi") -> str:
    tts = gTTS(text=text, lang=lang)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
        mp3_path = f.name
        tts.save(mp3_path)
    ogg_path = mp3_path.replace(".mp3", ".ogg")
    os.system(f'ffmpeg -y -i "{mp3_path}" -c:a libopus -b:a 48k -vbr on "{ogg_path}"')
    os.remove(mp3_path)
    return ogg_path

def get_kolkata_datetime() -> str:
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    return now.strftime("Aaj ki tareekh: %d-%m-%Y, samay: %I:%M %p")

def add_flirty_tone(answer: str) -> str:
    moods = [
        f"{answer}",
        f"kitne cute ho tum,\n\n{answer}",
        f"Bas itna hi samajh lo 😌 \n\n{answer}",
        f"Accha laga tumse baat karke, \n\n{answer}"
    ]
    return random.choice(moods)

async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = " ".join(context.args)
    if not question:
        await update.message.reply_text("Kya poochna hai ? Sawal ke phle /ask likho 😋")
        return

    if "owner" in question.lower():
        answer = "Mera owner Toxic hai 🫶"
    elif "time" in question.lower() or "date" in question.lower():
        answer = get_kolkata_datetime()
    else:
        raw_answer = ask_gemini(question)
        is_code_request = any(word in question.lower() for word in ["code", "script", "tool", "notes", "give", "do", "write", "how to"])
        if is_code_request:
            answer = raw_answer
        else:
            answer = add_flirty_tone(raw_answer)

    lang = detect(answer)
    logging.info(f"Detected language: {lang}")

    if "code" in question.lower() or "script" in question.lower() or "tool" in question.lower():
        if len(answer) > 3500:
            await update.message.reply_text("Sorry 😔, Code ka response bahut bada hai, isliye main isko bhej nahi sakti!")
            return

        escaped_code = escape_markdown(answer, version=2)
        formatted = f"```\n{escaped_code}\n```"

        if len(formatted) > 4096:
            await update.message.reply_text("Sorry 😔, Code ka response bahut bada hai, isliye main isko bhej nahi sakti!")
        else:
            await update.message.reply_text(formatted, parse_mode="MarkdownV2")
    else:
        await update.message.chat.send_action(action=ChatAction.RECORD_VOICE)
        ogg_path = text_to_ogg(answer, lang="hi" if lang == "hi" else "en")
        await update.message.reply_voice(voice=open(ogg_path, "rb"))
        os.remove(ogg_path)

#async def random_group_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
   # if update.message and not update.message.text:
      #  return
   # if random.random() < 0.1:
      #  text = update.message.text
      #  logging.info(f"Randomly analyzing: {text}")
    #    raw_reply = ask_gemini(text)
      #  reply = add_flirty_tone(raw_reply)
      #  ogg_path = text_to_ogg(reply, lang="hi")
      #  await update.message.chat.send_action(action=ChatAction.RECORD_VOICE)
    #    await update.message.reply_voice(voice=open(ogg_path, "rb"))
      #  os.remove(ogg_path)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("ask", ask_handler))
  #  app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), random_group_reply))
    logging.info("Bot started with error-free MarkdownV2 🚀")
    app.run_polling()
