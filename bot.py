import os
import logging
from decimal import Decimal, InvalidOperation
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
import random
import string
from tronpy import Tron
from tronpy.keys import PrivateKey
from tronpy.providers import HTTPProvider
from tronpy.exceptions import TransactionError

# 환경 변수 설정
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# 수수료 설정
ESCROW_FEE_PERCENTAGE = Decimal('0.05')  # 5% 중개 수수료
TRANSFER_FEE = Decimal('1.0')  # 송금 수수료 (TRON 기준)

# 트론(Tron) 네트워크 설정
tron = Tron()
SELLER_ADDRESS = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"
private_key = PrivateKey(bytes.fromhex(PRIVATE_KEY))
client = Tron(provider=HTTPProvider(TRON_API))

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
        conn.commit()
        logging.info("데이터베이스 테이블 초기화 완료")
    except SQLAlchemyError as e:
        logging.error(f"Database Initialization Error: {e}")

# 로깅 설정
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# 상태 정의
WAITING_FOR_ITEM_NAME = 1
WAITING_FOR_ITEM_PRICE = 2
WAITING_FOR_ITEM_TYPE = 3
WAITING_FOR_OFFER = 4
WAITING_FOR_PAYMENT = 5
WAITING_FOR_DELIVERY = 6
WAITING_FOR_REVIEW = 7

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
        if not price_input.replace('.', '', 1).isdigit():
            raise ValueError("입력 값이 숫자가 아님")
        
        price = Decimal(price_input)
        logging.info(f"Converted price: {price}")
        context.user_data['item_price'] = price

        await update.message.reply_text("판매할 물품의 종류를 선택해주세요. 디지털/현물 중 하나를 입력해주세요.")
        return WAITING_FOR_ITEM_TYPE
    except (InvalidOperation, ValueError) as e:
        logging.error(f"Error converting price: {e}")
        await update.message.reply_text("유효한 가격을 입력해주세요. 숫자로만 입력해 주세요.")
        return WAITING_FOR_ITEM_PRICE

# 판매 물품 종류 설정
async def set_item_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item_type = update.message.text.strip().lower()
    if item_type not in ['디지털', '현물']:
        await update.message.reply_text("유효한 물품 종류를 입력해주세요. (디지털 또는 현물)")
        return WAITING_FOR_ITEM_TYPE

    item_name = context.user_data.get('item_name')
    item_price = context.user_data.get('item_price')
    seller_id = update.message.from_user.id

    conn.execute(text('INSERT INTO items (name, price, seller_id, status, type) VALUES (:name, :price, :seller_id, :status, :type)'),
                 {'name': item_name, 'price': item_price, 'seller_id': seller_id, 'status': 'available', 'type': item_type})
    conn.commit()

    await update.message.reply_text(f"'{item_name}'을(를) {item_price} USDT에 {item_type} 물품으로 판매 등록하였습니다.")
    return ConversationHandler.END

# /list 명령어 (판매 물품 목록)
async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    page = context.user_data.get('page', 1)
    items_per_page = 10
    offset = (page - 1) * items_per_page

    items = conn.execute(
        text('SELECT id, name, price, type FROM items WHERE status=:status LIMIT :limit OFFSET :offset'),
        {'status': 'available', 'limit': items_per_page, 'offset': offset}
    ).fetchall()

    if not items:
        await update.message.reply_text("판매 중인 물품이 없습니다.")
        return

    message = f"판매 중인 물품 목록 (페이지 {page}):\n"
    for item in items:
        message += f"{item.id}. {item.name} - {item.price} USDT ({item.type})\n"

    keyboard = []
    if page > 1:
        keyboard.append([InlineKeyboardButton("이전 페이지", callback_data='previous_page')])
    if len(items) == items_per_page:
        keyboard.append([InlineKeyboardButton("다음 페이지", callback_data='next_page')])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text(message, reply_markup=reply_markup)

# 페이지 이동 처리
async def change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    page = context.user_data.get('page', 1)
    if query.data == 'next_page':
        context.user_data['page'] = page + 1
    elif query.data == 'previous_page':
        context.user_data['page'] = max(1, page - 1)

    await list_items(update, context)

# 구매자가 물품 선택 후 오퍼 전송
async def send_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        item_id = int(update.message.text)
        buyer_id = update.message.from_user.id

        item = conn.execute(
            text('SELECT id, name, price, seller_id, type FROM items WHERE id=:id AND status="available"'),
            {'id': item_id}
        ).fetchone()

        if not item:
            await update.message.reply_text("유효하지 않은 물품 ID입니다.")
            return ConversationHandler.END

        session_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
        context.user_data['session_id'] = session_id

        conn.execute(text('INSERT INTO transactions (item_id, buyer_id, seller_id, status, session_id, amount) VALUES (:item_id, :buyer_id, :seller_id, :status, :session_id, :amount)'),
                     {'item_id': item.id, 'buyer_id': buyer_id, 'seller_id': item.seller_id, 'status': 'pending', 'session_id': session_id, 'amount': item.price})
        conn.commit()

        await update.message.reply_text(f"'{item.name}'에 대한 구매 오퍼를 보냈습니다. 판매자가 수락할 때까지 기다려주세요.")

        # 판매자에게 오퍼 알림
        await context.bot.send_message(
            chat_id=item.seller_id,
            text=f"'{item.name}' 물품에 대해 구매자가 오퍼를 보냈습니다. 수락하시려면 /accept_{session_id}, 거절하시려면 /reject_{session_id}을 입력해주세요."
        )
        return WAITING_FOR_OFFER
    except Exception as e:
        await update.message.reply_text("유효한 물품 ID를 입력해주세요.")
        return ConversationHandler.END

# 판매자의 오퍼 수락
async def accept_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = update.message.text.split('_')[1]
    transaction = conn.execute(
        text('SELECT id, item_id, buyer_id, amount FROM transactions WHERE session_id=:session_id AND status="pending"'),
        {'session_id': session_id}
    ).fetchone()

    if not transaction:
        await update.message.reply_text("유효하지 않은 거래 세션입니다.")
        return

    conn.execute(
        text('UPDATE transactions SET status=:status WHERE id=:id'),
        {'status': 'accepted', 'id': transaction.id}
    )
    conn.commit()

    await update.message.reply_text("거래를 수락하였습니다. 구매자에게 결제 안내 메시지를 보냅니다.")

    # 구매자에게 결제 안내
    await context.bot.send_message(
        chat_id=transaction.buyer_id,
        text=(
            f"거래가 수락되었습니다. {transaction.amount} USDT를 다음 지갑으로 송금해주세요:\n"
            f"지갑 주소: {SELLER_ADDRESS}\n"
            f"네트워크: Tron (TRC20)\n"
            f"송금 후 /paid 명령어를 입력해주세요."
        )
    )

# 판매자의 오퍼 거절
async def reject_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = update.message.text.split('_')[1]
    transaction = conn.execute(
        text('SELECT id, buyer_id FROM transactions WHERE session_id=:session_id AND status="pending"'),
        {'session_id': session_id}
    ).fetchone()

    if not transaction:
        await update.message.reply_text("유효하지 않은 거래 세션입니다.")
        return

    conn.execute(
        text('UPDATE transactions SET status=:status WHERE id=:id'),
        {'status': 'rejected', 'id': transaction.id}
    )
    conn.commit()

    await update.message.reply_text("거래를 거절하였습니다. 구매자에게 알림을 보냅니다.")

    # 구매자에게 거절 알림
    await context.bot.send_message(
        chat_id=transaction.buyer_id,
        text="판매자가 거래를 거절하였습니다. 다른 물품을 선택해주세요."
    )

from tronpy import Tron
from tronpy.keys import PrivateKey

# TronLink 설정
TRON_API = "https://api.trongrid.io"  # Tron 메인넷
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
SELLER_ADDRESS = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"
USDT_CONTRACT = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"  # USDT (TRC20) 스마트 계약 주소

client = Tron(provider=TRON_API)
wallet = client.get_wallet(PRIVATE_KEY)

# /paid 명령어 (구매자가 결제 완료 알림)
async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    buyer_id = update.message.from_user.id
    transaction = conn.execute(
        text('SELECT id, item_id, seller_id, amount, session_id FROM transactions WHERE buyer_id=:buyer_id AND status="accepted"'),
        {'buyer_id': buyer_id}
    ).fetchone()

    if not transaction:
        await update.message.reply_text("진행 중인 거래가 없습니다.")
        return

    amount = transaction.amount
    session_id = transaction.session_id

    # Tron 네트워크에서 입금 확인
    balance = client.get_balance(SELLER_ADDRESS, USDT_CONTRACT) / 10**6  # USDT는 소수점 6자리
    logging.info(f"현재 지갑 잔액: {balance} USDT")

    if balance < amount:
        await update.message.reply_text(f"입금액이 부족합니다. {amount - balance} USDT를 추가로 송금해주세요.")
        return
    elif balance > amount:
        await update.message.reply_text(f"입금액이 초과되었습니다. 초과된 {balance - amount} USDT는 거래 완료 후 반환됩니다.")

    # 거래 완료 처리
    fee = amount * ESCROW_FEE_PERCENTAGE
    seller_amount = amount - fee

    # 송금 처리 (중개 수수료를 제외하고 판매자에게 송금)
    txn = wallet.trx.transfer(SELLER_ADDRESS, transaction.seller_id, int(seller_amount * 10**6))
    txn.sign(PRIVATE_KEY).broadcast()

    conn.execute(
        text('UPDATE transactions SET status=:status WHERE id=:id'),
        {'status': 'completed', 'id': transaction.id}
    )
    conn.commit()

    await update.message.reply_text("테더 입금이 확인되었습니다. 거래를 완료합니다.")

    # 판매자와 구매자에게 완료 메시지 전송
    await context.bot.send_message(
        chat_id=transaction.seller_id,
        text="구매자가 결제를 완료하였습니다. 물품을 발송해주세요."
    )
    await context.bot.send_message(
        chat_id=buyer_id,
        text="거래가 완료되었습니다. 판매자를 평가해주세요. /rate 명령어를 사용하세요."
    )

# /rate 명령어 (거래 평가)
async def rate_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        rating = int(update.message.text.split()[1])
        if rating < 1 or rating > 5:
            raise ValueError("평점은 1에서 5 사이의 숫자여야 합니다.")
        
        buyer_id = update.message.from_user.id
        transaction = conn.execute(
            text('SELECT id, seller_id FROM transactions WHERE buyer_id=:buyer_id AND status="completed"'),
            {'buyer_id': buyer_id}
        ).fetchone()

        if not transaction:
            await update.message.reply_text("평가할 거래가 없습니다.")
            return

        conn.execute(
            text('INSERT INTO ratings (transaction_id, seller_id, rating) VALUES (:transaction_id, :seller_id, :rating)'),
            {'transaction_id': transaction.id, 'seller_id': transaction.seller_id, 'rating': rating}
        )
        conn.commit()

        await update.message.reply_text(f"거래에 대해 {rating}점을 부여하였습니다.")
    except (ValueError, IndexError) as e:
        await update.message.reply_text("올바른 형식으로 평점을 입력해주세요. 예: /rate 4")

# 구매자와 판매자 간 안전한 채팅 및 파일 전송 기능
async def start_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = update.message.text.split()[1]
    transaction = conn.execute(
        text('SELECT buyer_id, seller_id FROM transactions WHERE session_id=:session_id AND status="accepted"'),
        {'session_id': session_id}
    ).fetchone()

    if not transaction:
        await update.message.reply_text("유효하지 않은 채팅 세션입니다.")
        return

    context.user_data['chat_session'] = session_id
    await update.message.reply_text(
        "안전한 채팅을 시작합니다. 메시지를 입력하면 상대방에게 전달됩니다. 파일도 전송할 수 있습니다.\n"
        "채팅을 종료하려면 /exit 명령어를 입력하세요."
    )

async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    session_id = context.user_data.get('chat_session')
    if not session_id:
        return

    transaction = conn.execute(
        text('SELECT buyer_id, seller_id FROM transactions WHERE session_id=:session_id AND status="accepted"'),
        {'session_id': session_id}
    ).fetchone()

    if not transaction:
        return

    sender_id = update.message.from_user.id
    receiver_id = transaction.seller_id if sender_id == transaction.buyer_id else transaction.buyer_id

    if update.message.text:
        await context.bot.send_message(chat_id=receiver_id, text=update.message.text)
    elif update.message.document:
        await update.message.document.forward(receiver_id)
    elif update.message.photo:
        await update.message.photo[-1].forward(receiver_id)

# /exit 명령어 (초기 화면으로 돌아가기)
async def exit_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("초기 화면으로 돌아갑니다. /start 명령어를 입력하여 다시 시작할 수 있습니다.")

# 메인 함수
def main():
    application = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    # 판매 물품 등록 대화 흐름
    sell_handler = ConversationHandler(
        entry_points=[CommandHandler('sell', sell)],
        states={
            WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
            WAITING_FOR_ITEM_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
        },
        fallbacks=[CommandHandler('exit', exit_chat)]
    )

    # 물품 취소 대화 흐름
    cancel_handler = ConversationHandler(
        entry_points=[CommandHandler('cancel', cancel)],
        states={
            WAITING_FOR_CANCEL_SELECTION: [CallbackQueryHandler(confirm_cancel)],
        },
        fallbacks=[CommandHandler('exit', exit_chat)]
    )

    # 채팅 대화 흐름
    chat_handler = ConversationHandler(
        entry_points=[CommandHandler('chat', start_chat)],
        states={
            WAITING_FOR_CANCEL_SELECTION: [MessageHandler(filters.ALL, forward_message)],
        },
        fallbacks=[CommandHandler('exit', exit_chat)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_items))
    application.add_handler(CommandHandler("next", list_items_next_page))
    application.add_handler(CommandHandler("previous", list_items_previous_page))
    application.add_handler(CommandHandler("offer", send_offer))
    application.add_handler(CommandHandler("accept", accept_offer))
    application.add_handler(CommandHandler("reject", reject_offer))
    application.add_handler(CommandHandler("paid", check_payment))
    application.add_handler(CommandHandler("rate", rate_transaction))
    application.add_handler(CommandHandler("ok", confirm_purchase))
    application.add_handler(sell_handler)
    application.add_handler(cancel_handler)
    application.add_handler(chat_handler)
    application.add_handler(CommandHandler("exit", exit_chat))

    application.run_polling()

if __name__ == '__main__':
    main()