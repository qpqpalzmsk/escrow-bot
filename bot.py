import os
import logging
import time
from datetime import datetime, timedelta
from decimal import Decimal
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    CallbackQueryHandler, 
    MessageHandler, 
    filters, 
    ContextTypes
)
import sqlite3

# 환경 변수 설정
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# 수수료 설정
ESCROW_FEE_PERCENTAGE = Decimal('0.05')  # 5% 중개 수수료
TRANSFER_FEE = Decimal('1.0')  # 송금 수수료 (TRON 기준)

# 데이터베이스 초기화
conn = sqlite3.connect('escrow.db')
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    price DECIMAL,
    seller_id INTEGER,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    buyer_id INTEGER,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (item_id) REFERENCES items (id)
)
''')

conn.commit()

# 로깅 설정
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# /start 명령어
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("안녕하세요! 에스크로 거래 봇입니다. 판매할 물품은 /sell, 구매할 물품은 /list를 입력해주세요.")

# /sell 명령어 (판매 물품 등록)
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("판매할 물품의 이름을 입력해주세요.")
    return "WAITING_FOR_ITEM_NAME"

# 판매 물품 이름 입력
async def set_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data['item_name'] = update.message.text
    await update.message.reply_text(f"'{update.message.text}'의 가격을 트론(USDT)으로 입력해주세요.")
    return "WAITING_FOR_ITEM_PRICE"

# 판매 물품 가격 입력
async def set_item_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        price = Decimal(update.message.text)
        item_name = context.user_data.get('item_name')
        seller_id = update.message.from_user.id

        cursor.execute('INSERT INTO items (name, price, seller_id, status) VALUES (?, ?, ?, ?)',
                       (item_name, price, seller_id, 'available'))
        conn.commit()

        await update.message.reply_text(f"'{item_name}'을(를) {price} USDT에 판매 등록하였습니다.")
    except Exception as e:
        await update.message.reply_text("유효한 가격을 입력해주세요.")

# /list 명령어 (판매 물품 목록)
async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cursor.execute('SELECT id, name, price FROM items WHERE status="available"')
    items = cursor.fetchall()

    if not items:
        await update.message.reply_text("판매 중인 물품이 없습니다.")
        return

    message = "판매 중인 물품 목록:\n"
    for item in items:
        message += f"{item[0]}. {item[1]} - {item[2]} USDT\n"
    
    await update.message.reply_text(message)

# /search 명령어 (물품 검색)
async def search_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ' '.join(context.args)
    cursor.execute('SELECT id, name, price FROM items WHERE name LIKE ? AND status="available"', ('%' + query + '%',))
    items = cursor.fetchall()

    if not items:
        await update.message.reply_text(f"'{query}'에 해당하는 물품이 없습니다.")
        return

    message = "검색 결과:\n"
    for item in items:
        message += f"{item[0]}. {item[1]} - {item[2]} USDT\n"
    
    await update.message.reply_text(message)

# 구매자가 물품 선택 후 오퍼 보내기
async def send_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        item_id = int(update.message.text)
        buyer_id = update.message.from_user.id

        cursor.execute('SELECT name, seller_id FROM items WHERE id=? AND status="available"', (item_id,))
        item = cursor.fetchone()

        if not item:
            await update.message.reply_text("유효하지 않은 물품 ID입니다.")
            return

        cursor.execute('INSERT INTO offers (item_id, buyer_id, status) VALUES (?, ?, ?)', (item_id, buyer_id, 'pending'))
        conn.commit()

        await update.message.reply_text(f"{item[0]}에 대한 구매 오퍼를 보냈습니다. 판매자가 수락할 때까지 기다려주세요.")
    except Exception as e:
        await update.message.reply_text("유효한 물품 ID를 입력해주세요.")

# 거래 완료 확인 (/ok)
async def confirm_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("거래 완료 확인! 판매자에게 정산을 진행합니다.")

# 메인 함수
def main():
    application = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("sell", sell))
    application.add_handler(CommandHandler("list", list_items))
    application.add_handler(CommandHandler("search", search_items))
    application.add_handler(CommandHandler("ok", confirm_purchase))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, send_offer))

    application.run_polling()

if __name__ == '__main__':
    main()