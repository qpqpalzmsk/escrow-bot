import os
import logging
from telegram import Update, Message
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import psycopg2
from tron_transfer import send_usdt

# 로그 설정
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# PostgreSQL 데이터베이스 연결 설정
DATABASE_URL = os.getenv('DATABASE_URL')
conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# 거래 관련 설정
TRANSACTION_FEE_RATE = 0.02  # 거래 수수료 (내 수익)
TRANSFER_FEE = 1.0  # TRC20 네트워크 송금 수수료 (고정)

# 📌 봇 시작 명령어
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text('에스크로 봇이 시작되었습니다! 사용 가능한 명령어: /가입, /판매등록, /구매, /거래완료, /배송등록, /수령완료')

# 📌 사용자 지갑 주소 등록 명령어
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    telegram_id = update.message.from_user.id
    if not context.args:
        await update.message.reply_text("지갑 주소를 입력해주세요. 예: /가입 your_wallet_address")
        return
    
    wallet_address = context.args[0]
    
    try:
        cursor.execute("""
            INSERT INTO users (telegram_id, wallet_address) 
            VALUES (%s, %s) 
            ON CONFLICT (telegram_id) DO UPDATE 
            SET wallet_address = EXCLUDED.wallet_address
        """, (telegram_id, wallet_address))
        conn.commit()
        await update.message.reply_text(f"지갑 주소 등록 완료! {wallet_address}")
    except Exception as e:
        logging.error(f"Error in register: {e}")
        await update.message.reply_text(f"오류 발생: {e}")

# 📌 판매자가 물품 등록
async def add_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    seller_id = update.message.from_user.id
    if len(context.args) < 2:
        await update.message.reply_text("예시: /판매등록 아이템이름 가격")
        return
    
    item_name = context.args[0]
    price = float(context.args[1])

    try:
        cursor.execute("""
            INSERT INTO items (seller_id, item_name, price) 
            VALUES (%s, %s, %s)
        """, (seller_id, item_name, price))
        conn.commit()
        await update.message.reply_text(f"물품 등록 완료! {item_name} - 가격: {price} USDT")
    except Exception as e:
        logging.error(f"Error in add_item: {e}")
        await update.message.reply_text(f"오류 발생: {e}")

# 📌 구매자가 거래 요청
async def purchase_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buyer_id = update.message.from_user.id
    if not context.args:
        await update.message.reply_text("거래할 물품 ID를 입력해주세요. 예: /구매 123")
        return
    
    item_id = int(context.args[0])

    # 거래 정보 가져오기
    cursor.execute("""
        SELECT items.id, items.item_name, items.price, users.telegram_id 
        FROM items 
        JOIN users ON items.seller_id = users.id 
        WHERE items.id = %s AND items.status = 'available'
    """, (item_id,))
    
    item = cursor.fetchone()
    if not item:
        await update.message.reply_text("구매 가능한 물품을 찾을 수 없습니다.")
        return

    item_id, item_name, price, seller_id = item

    # 거래를 '진행 중'으로 표시
    cursor.execute("UPDATE items SET status = 'in_progress' WHERE id = %s", (item_id,))
    conn.commit()

    await update.message.reply_text(f"{item_name} 구매를 시작합니다.\n판매자와의 채팅을 시작하려면 /채팅 {item_id}를 입력하세요.")

# 📌 중개 채팅 기능
async def relay_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    message_text = update.message.text

    cursor.execute("""
        SELECT items.id, items.seller_id, items.buyer_id 
        FROM items 
        WHERE status = 'in_progress' AND (seller_id = %s OR buyer_id = %s)
    """, (user_id, user_id))
    
    chat = cursor.fetchone()
    if not chat:
        await update.message.reply_text("현재 진행 중인 거래가 없습니다.")
        return

    item_id, seller_id, buyer_id = chat

    # 메시지를 상대방에게 중계
    target_id = buyer_id if user_id == seller_id else seller_id
    await context.bot.send_message(chat_id=target_id, text=f"[거래 #{item_id} 메시지] {message_text}")

# 📌 명령어 등록
def main():
    app = Application.builder().token(os.getenv('TELEGRAM_API_KEY')).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", register))
    app.add_handler(CommandHandler("add", add_item))
    app.add_handler(CommandHandler("buy", purchase_item))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, relay_message))
    
    app.run_polling()

if __name__ == '__main__':
    main()