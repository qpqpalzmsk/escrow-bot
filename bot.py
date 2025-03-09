import os
import logging
from decimal import Decimal, InvalidOperation
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes,
    ConversationHandler
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# 환경 변수 설정
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# 수수료 설정
ESCROW_FEE_PERCENTAGE = Decimal('0.05')  # 5% 중개 수수료
TRANSFER_FEE = Decimal('1.0')  # 송금 수수료 (TRON 기준)

# 로깅 설정
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 데이터베이스 연결 설정 (PostgreSQL)
try:
    engine = create_engine(DATABASE_URL)
    conn = engine.connect()
    logging.info("Database connected successfully")

    # 데이터베이스 초기화
    conn.execute(text('''
    CREATE TABLE IF NOT EXISTS items (
        id SERIAL PRIMARY KEY,
        name TEXT,
        price DECIMAL,
        seller_id INTEGER,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    '''))

    conn.execute(text('''
    CREATE TABLE IF NOT EXISTS offers (
        id SERIAL PRIMARY KEY,
        item_id INTEGER REFERENCES items(id),
        buyer_id INTEGER,
        status TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    '''))
    conn.commit()
    logging.info("Database tables initialized successfully")

except SQLAlchemyError as e:
    logging.error(f"Database Initialization Error: {e}")

# 상태 정의
WAITING_FOR_ITEM_NAME = 1
WAITING_FOR_ITEM_PRICE = 2
WAITING_FOR_CANCEL_SELECTION = 3

# /start 명령어
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("안녕하세요! 에스크로 거래 봇입니다. 판매할 물품은 /sell, 구매할 물품은 /list를 입력해주세요.")

# /sell 명령어 (판매 물품 등록)
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("판매할 물품의 이름을 입력해주세요.")
    return WAITING_FOR_ITEM_NAME

# 판매 물품 이름 입력
async def set_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['item_name'] = update.message.text
    await update.message.reply_text(f"'{update.message.text}'의 가격을 트론(USDT)으로 입력해주세요.")
    return WAITING_FOR_ITEM_PRICE

# 판매 물품 가격 입력
async def set_item_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    price_input = update.message.text.strip()
    logging.info(f"Received price input: {price_input}")
    
    try:
        # 숫자로만 구성된지 확인
        if not price_input.replace('.', '', 1).isdigit():
            raise ValueError("입력 값이 숫자가 아님")
        
        price = Decimal(price_input)
        logging.info(f"Converted price: {price}")

        item_name = context.user_data.get('item_name')
        seller_id = update.message.from_user.id

        conn.execute(text('INSERT INTO items (name, price, seller_id, status) VALUES (:name, :price, :seller_id, :status)'),
                     {'name': item_name, 'price': price, 'seller_id': seller_id, 'status': 'available'})
        conn.commit()

        await update.message.reply_text(f"'{item_name}'을(를) {price} USDT에 판매 등록하였습니다.")
        return ConversationHandler.END
    except (InvalidOperation, ValueError) as e:
        logging.error(f"Error converting price: {e}")
        await update.message.reply_text("유효한 가격을 입력해주세요. 숫자로만 입력해 주세요.")
        return WAITING_FOR_ITEM_PRICE

# /list 명령어 (판매 물품 목록)
async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        items = conn.execute(text('SELECT id, name, price FROM items WHERE status=:status'), {'status': 'available'}).fetchall()

        if not items:
            await update.message.reply_text("판매 중인 물품이 없습니다.")
            return

        message = "판매 중인 물품 목록:\n"
        for item in items:
            message += f"{item.id}. {item.name} - {item.price} USDT\n"
        
        await update.message.reply_text(message)
    except SQLAlchemyError as e:
        logging.error(f"Error listing items: {e}")

# /cancel 명령어 (등록 물품 취소)
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        seller_id = update.message.from_user.id
        items = conn.execute(text('SELECT id, name FROM items WHERE seller_id=:seller_id AND status=:status'),
                             {'seller_id': seller_id, 'status': 'available'}).fetchall()

        if not items:
            await update.message.reply_text("취소할 물품이 없습니다.")
            return ConversationHandler.END

        keyboard = [[InlineKeyboardButton(item.name, callback_data=str(item.id))] for item in items]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text('취소할 물품을 선택해주세요.', reply_markup=reply_markup)
        return WAITING_FOR_CANCEL_SELECTION
    except SQLAlchemyError as e:
        logging.error(f"Error fetching items for cancellation: {e}")

# 물품 취소 처리
async def confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        query = update.callback_query
        await query.answer()
        item_id = int(query.data)

        conn.execute(text('UPDATE items SET status=:status WHERE id=:id'), {'status': 'cancelled', 'id': item_id})
        conn.commit()

        await query.edit_message_text(text="선택한 물품을 취소하였습니다.")
        return ConversationHandler.END
    except SQLAlchemyError as e:
        logging.error(f"Error confirming item cancellation: {e}")

# /ok 명령어 (거래 완료 확인)
async def confirm_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("거래 완료 확인! 판매자에게 정산을 진행합니다.")

# 메인 함수
def main():
    application = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    sell_handler = ConversationHandler(
        entry_points=[CommandHandler('sell', sell)],
        states={
            WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
            WAITING_FOR_ITEM_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_items))
    application.add_handler(CommandHandler("ok", confirm_purchase))
    application.add_handler(sell_handler)

    application.run_polling()

if __name__ == '__main__':
    main()