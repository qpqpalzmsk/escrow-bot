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
from tronpy import Tron
from tronpy.providers import HTTPProvider
import requests

# 환경 변수 설정
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
TRON_API = os.getenv("TRON_API")
TRON_API_KEY = os.getenv("TRON_API_KEY")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# 수수료 설정
ESCROW_FEE_PERCENTAGE = Decimal('0.05')  # 5% 중개 수수료
TRANSFER_FEE = Decimal('1.0')  # 송금 수수료 (TRON 기준)
BOT_WALLET_ADDRESS = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"

# 데이터베이스 연결 설정 (PostgreSQL)
try:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    
    engine = create_engine(DATABASE_URL, echo=True)
    conn = engine.connect()
    logging.info("데이터베이스 연결 성공")
except SQLAlchemyError as e:
    logging.error(f"데이터베이스 연결 오류: {e}")
    conn = None

# 데이터베이스 초기화
if conn:
    try:
        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            name TEXT,
            price DECIMAL,
            seller_id BIGINT,
            status TEXT,
            type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''))

        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            item_id INTEGER REFERENCES items(id),
            buyer_id BIGINT,
            seller_id BIGINT,
            status TEXT,
            session_id TEXT,
            transaction_id TEXT,
            amount DECIMAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''))

        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS ratings (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            score INTEGER,
            review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''))
        conn.commit()
        logging.info("데이터베이스 테이블 초기화 완료")
    except SQLAlchemyError as e:
        logging.error(f"데이터베이스 초기화 오류: {e}")

# TronPy 클라이언트 초기화
session = requests.Session()
session.headers.update({"TRON-PRO-API-KEY": TRON_API_KEY})
client = Tron(provider=HTTPProvider(TRON_API))

# 로깅 설정
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 상태 정의
WAITING_FOR_ITEM_NAME = 1
WAITING_FOR_ITEM_PRICE = 2
WAITING_FOR_ITEM_TYPE = 3
WAITING_FOR_CANCEL_SELECTION = 4

# /start 명령어
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "안녕하세요! 에스크로 거래 봇입니다. \n"
        "판매할 물품은 /sell, \n"
        "구매할 물품은 /list를 입력해주세요."
    )

# /sell 명령어 (판매 물품 등록)
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("판매할 물품의 이름을 입력해주세요.")
    return WAITING_FOR_ITEM_NAME

# 판매 물품 이름 입력
async def set_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item_name = update.message.text.strip()
    
    if not item_name:
        await update.message.reply_text("물품 이름을 입력해주세요. (한글, 영어 모두 가능)")
        return WAITING_FOR_ITEM_NAME

    context.user_data['item_name'] = item_name
    await update.message.reply_text(f"'{item_name}'의 가격을 트론(USDT)으로 입력해주세요.")
    return WAITING_FOR_ITEM_PRICE

# 판매 물품 가격 입력
async def set_item_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    price_str = update.message.text.strip()
    try:
        # 가격은 숫자와 소수점만 허용
        if not price_str.replace('.', '', 1).isdigit():
            await update.message.reply_text("유효한 가격을 입력해주세요. 숫자 형식으로만 입력해 주세요.")
            return WAITING_FOR_ITEM_PRICE

        price = Decimal(price_str)
        context.user_data['price'] = price
        await update.message.reply_text("물품의 종류를 입력해주세요 (디지털/현물).")
        return WAITING_FOR_ITEM_TYPE
    except (InvalidOperation, ValueError) as e:
        logging.error(f"가격 변환 오류: {e}")
        await update.message.reply_text("유효한 가격을 입력해주세요. (숫자 형식만 가능)")
        return WAITING_FOR_ITEM_PRICE

# 물품 종류 입력
async def set_item_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item_type = update.message.text.strip().lower()

    if item_type not in ['디지털', '현물']:
        await update.message.reply_text("유효한 종류를 입력해주세요. (디지털/현물)")
        return WAITING_FOR_ITEM_TYPE

    item_name = context.user_data.get('item_name')
    price = context.user_data.get('price')
    seller_id = update.message.from_user.id

    conn.execute(text('INSERT INTO items (name, price, seller_id, status, type) VALUES (:name, :price, :seller_id, :status, :type)'),
                 {'name': item_name, 'price': price, 'seller_id': seller_id, 'status': 'available', 'type': item_type})
    conn.commit()

    await update.message.reply_text(f"'{item_name}'을(를) {price} USDT에 판매 등록하였습니다.")
    return ConversationHandler.END

# /list 명령어 (판매 물품 목록)
async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
    items_per_page = 10
    offset = (page - 1) * items_per_page

    items = conn.execute(text('SELECT id, name, price FROM items WHERE status=:status LIMIT :limit OFFSET :offset'),
                         {'status': 'available', 'limit': items_per_page, 'offset': offset}).fetchall()

    if not items:
        await update.message.reply_text("판매 중인 물품이 없습니다.")
        return

    message = "판매 중인 물품 목록:\n"
    for item in items:
        message += f"ID: {item.id}, 이름: {item.name}, 가격: {item.price} USDT\n"

    next_page = f"/list {page + 1}"
    prev_page = f"/list {page - 1}" if page > 1 else None

    pagination_buttons = [InlineKeyboardButton("다음 페이지", callback_data=next_page)]
    if prev_page:
        pagination_buttons.insert(0, InlineKeyboardButton("이전 페이지", callback_data=prev_page))

    reply_markup = InlineKeyboardMarkup([pagination_buttons])
    await update.message.reply_text(message, reply_markup=reply_markup)

# 페이지 이동 처리
async def paginate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await list_items(update, context)

# 구매자가 물품을 선택했을 때 오퍼 전송
async def send_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        item_id = int(update.message.text)
        buyer_id = update.message.from_user.id

        item = conn.execute(text('SELECT name, seller_id, price FROM items WHERE id=:id AND status=:status'),
                            {'id': item_id, 'status': 'available'}).fetchone()

        if not item:
            await update.message.reply_text("유효하지 않은 물품 ID입니다.")
            return

        # 거래 생성
        conn.execute(text('INSERT INTO transactions (item_id, buyer_id, seller_id, status) VALUES (:item_id, :buyer_id, :seller_id, :status)'),
                     {'item_id': item_id, 'buyer_id': buyer_id, 'seller_id': item.seller_id, 'status': 'pending'})
        conn.commit()

        await update.message.reply_text(f"'{item.name}'에 대한 구매 오퍼를 보냈습니다. 판매자가 수락할 때까지 기다려주세요.")
        await context.bot.send_message(item.seller_id, f"'{item.name}'을(를) {item.price} USDT에 구매하려는 오퍼가 들어왔습니다. 수락하시겠습니까? (ID: {item_id})")
    except ValueError:
        await update.message.reply_text("유효한 물품 ID를 입력해주세요.")
    
# 판매자가 오퍼를 수락하거나 거절
async def handle_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data.split(':')

    if data[0] == 'accept':
        transaction_id = data[1]
        transaction = conn.execute(text('SELECT * FROM transactions WHERE id=:id AND status=:status'),
                                   {'id': transaction_id, 'status': 'pending'}).fetchone()

        if not transaction:
            await query.edit_message_text("유효하지 않은 거래입니다.")
            return

        conn.execute(text('UPDATE transactions SET status=:status WHERE id=:id'),
                     {'status': 'accepted', 'id': transaction_id})
        conn.commit()

        await query.edit_message_text("거래를 수락하였습니다. 구매자에게 입금 안내를 보냅니다.")
        await context.bot.send_message(transaction.buyer_id, 
                                       f"거래가 수락되었습니다. 다음 주소로 {transaction.amount} USDT를 보내주세요: {BOT_WALLET_ADDRESS}")
    elif data[0] == 'reject':
        transaction_id = data[1]
        conn.execute(text('UPDATE transactions SET status=:status WHERE id=:id'),
                     {'status': 'rejected', 'id': transaction_id})
        conn.commit()
        await query.edit_message_text("거래를 거절하였습니다.")

# 테더(USDT) 입금 확인
def check_usdt_payment(expected_amount: Decimal, buyer_address: str) -> bool:
    try:
        response = client.get_account(BOT_WALLET_ADDRESS)
        balance = Decimal(response.get("balance", "0")) / Decimal(1e6)

        if balance >= expected_amount:
            return True
        return False
    except Exception as e:
        logging.error(f"트론 입금 확인 오류: {e}")
        return False

# 입금 확인 및 거래 완료
async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    transaction_id = context.args[0] if context.args else None
    if not transaction_id:
        await update.message.reply_text("유효하지 않은 거래 ID입니다.")
        return

    transaction = conn.execute(text('SELECT * FROM transactions WHERE id=:id AND status=:status'),
                               {'id': transaction_id, 'status': 'accepted'}).fetchone()

    if not transaction:
        await update.message.reply_text("거래가 유효하지 않습니다.")
        return

    item = conn.execute(text('SELECT * FROM items WHERE id=:id'), {'id': transaction.item_id}).fetchone()

    if check_usdt_payment(item.price, transaction.buyer_id):
        conn.execute(text('UPDATE transactions SET status=:status WHERE id=:id'),
                     {'status': 'completed', 'id': transaction_id})
        conn.execute(text('UPDATE items SET status=:status WHERE id=:id'),
                     {'status': 'sold', 'id': item.id})
        conn.commit()

        await update.message.reply_text("거래 완료! 판매자에게 물품을 보내주세요.")
        await context.bot.send_message(transaction.seller_id, "입금이 확인되었습니다. 구매자에게 물품을 보내주세요.")
    else:
        await update.message.reply_text("입금이 확인되지 않았습니다. 다시 확인해주세요.")

# 거래 완료 후 평가 시스템
async def rate_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    transaction_id = context.args[0] if context.args else None
    if not transaction_id:
        await update.message.reply_text("유효하지 않은 거래 ID입니다.")
        return

    await update.message.reply_text("거래 평가를 위해 1점에서 5점 사이의 평점을 입력해주세요.")
    context.user_data['transaction_id'] = transaction_id
    return WAITING_FOR_RATING

# 평점 저장
async def save_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        rating = int(update.message.text)
        if rating < 1 or rating > 5:
            raise ValueError("평점은 1에서 5 사이의 숫자여야 합니다.")

        transaction_id = context.user_data.get('transaction_id')
        user_id = update.message.from_user.id

        conn.execute(text('INSERT INTO ratings (user_id, score, review) VALUES (:user_id, :score, :review)'),
                     {'user_id': user_id, 'score': rating, 'review': '거래 완료'})
        conn.commit()

        await update.message.reply_text(f"거래에 {rating}점을 주셨습니다. 감사합니다!")
        return ConversationHandler.END
    except ValueError as e:
        await update.message.reply_text("유효한 평점을 입력해주세요. (1~5점)")
        return WAITING_FOR_RATING
    
# 안전한 채팅 및 파일 전송 지원
async def start_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    transaction_id = context.args[0] if context.args else None
    if not transaction_id:
        await update.message.reply_text("유효하지 않은 거래 ID입니다.")
        return

    transaction = conn.execute(text('SELECT * FROM transactions WHERE id=:id'), {'id': transaction_id}).fetchone()

    if not transaction:
        await update.message.reply_text("거래가 유효하지 않습니다.")
        return

    context.user_data['transaction_id'] = transaction_id
    context.user_data['chat_partner'] = transaction.buyer_id if update.message.from_user.id == transaction.seller_id else transaction.seller_id

    await update.message.reply_text("채팅을 시작합니다. 상대방에게 메시지를 보내세요.")

# 채팅 메시지 전달
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_partner = context.user_data.get('chat_partner')
    if chat_partner:
        await context.bot.send_message(chat_partner, f"메시지: {update.message.text}")

# 파일 전송
async def forward_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_partner = context.user_data.get('chat_partner')
    if chat_partner:
        if update.message.document:
            file = await update.message.document.get_file()
            await context.bot.send_document(chat_partner, file.file_id, caption="파일을 받았습니다.")
        elif update.message.photo:
            photo = update.message.photo[-1]
            await context.bot.send_photo(chat_partner, photo.file_id, caption="사진을 받았습니다.")

# /exit 명령어 (초기 화면으로 돌아가기)
async def exit_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("초기 화면으로 돌아갑니다. 판매할 물품은 /sell, 구매할 물품은 /list를 입력해주세요.")
    return ConversationHandler.END

# /cancel 명령어 (물품 삭제)
async def cancel_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("유효한 물품 ID를 입력해주세요. 예: /cancel 123")
        return

    item_id = int(context.args[0])
    seller_id = update.message.from_user.id

    # 판매자가 본인의 물품만 삭제할 수 있도록 검증
    result = conn.execute(text('SELECT id FROM items WHERE id=:id AND seller_id=:seller_id AND status="available"'),
                          {'id': item_id, 'seller_id': seller_id}).fetchone()

    if not result:
        await update.message.reply_text("유효하지 않은 물품 ID이거나 삭제 권한이 없습니다.")
        return

    conn.execute(text('UPDATE items SET status="cancelled" WHERE id=:id'), {'id': item_id})
    conn.commit()

    await update.message.reply_text("작업이 취소되었습니다. 초기 화면으로 돌아갑니다.")

def main():
    application = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    sell_handler = ConversationHandler(
        entry_points=[CommandHandler('sell', sell)],
        states={
            WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
            WAITING_FOR_ITEM_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
            WAITING_FOR_ITEM_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_type)],
        },
        fallbacks=[CommandHandler('exit', exit_to_start)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_items))
    application.add_handler(CommandHandler("cancel", cancel_item))
    application.add_handler(CallbackQueryHandler(paginate, pattern="^/list"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, send_offer))
    application.add_handler(CallbackQueryHandler(handle_offer, pattern="^(accept|reject):"))
    application.add_handler(CommandHandler("ok", confirm_payment))
    application.add_handler(CommandHandler("rate", rate_transaction))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, save_rating))
    application.add_handler(CommandHandler("chat", start_chat))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_message))
    application.add_handler(MessageHandler(filters.Document.ALL, forward_file))
    application.add_handler(MessageHandler(filters.PHOTO, forward_file))
    application.add_handler(CommandHandler("exit", exit_to_start))
    application.add_handler(sell_handler)

    application.run_polling()

if __name__ == '__main__':
    main()