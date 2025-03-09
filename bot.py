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
            item_type TEXT,
            seller_id BIGINT,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''))

        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS offers (
            id SERIAL PRIMARY KEY,
            item_id INTEGER REFERENCES items(id),
            buyer_id BIGINT,
            status TEXT,
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
WAITING_FOR_ITEM_SELECTION = 4
WAITING_FOR_CANCEL_SELECTION = 5

# 페이지당 물품 수
ITEMS_PER_PAGE = 10

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
    try:
        price = Decimal(update.message.text.strip())
        context.user_data['item_price'] = price
        await update.message.reply_text("물품 유형을 입력해주세요. (디지털/현물)")
        return WAITING_FOR_ITEM_TYPE
    except InvalidOperation:
        await update.message.reply_text("유효한 가격을 입력해주세요. 숫자로만 입력해 주세요.")
        return WAITING_FOR_ITEM_PRICE

# 판매 물품 유형 입력
async def set_item_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item_type = update.message.text.strip().lower()
    if item_type not in ['디지털', '현물']:
        await update.message.reply_text("유효한 물품 유형을 입력해주세요. (디지털/현물)")
        return WAITING_FOR_ITEM_TYPE

    item_name = context.user_data.get('item_name')
    item_price = context.user_data.get('item_price')
    seller_id = update.message.from_user.id

    conn.execute(text('INSERT INTO items (name, price, item_type, seller_id, status) VALUES (:name, :price, :item_type, :seller_id, :status)'),
                 {'name': item_name, 'price': item_price, 'item_type': item_type, 'seller_id': seller_id, 'status': 'available'})
    conn.commit()

    await update.message.reply_text(f"'{item_name}'을(를) {item_price} USDT에 판매 등록하였습니다. 유형: {item_type}")
    return ConversationHandler.END

# /list 명령어 (물품 목록 표시)
async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    page = context.user_data.get('page', 1)
    offset = (page - 1) * ITEMS_PER_PAGE
    items = conn.execute(text('SELECT id, name, price FROM items WHERE status=:status LIMIT :limit OFFSET :offset'),
                         {'status': 'available', 'limit': ITEMS_PER_PAGE, 'offset': offset}).fetchall()

    if not items:
        await update.message.reply_text("판매 중인 물품이 없습니다.")
        return

    message = "판매 중인 물품 목록 (페이지 {page}):\n"
    for item in items:
        message += f"{item.id}. {item.name} - {item.price} USDT\n"

    navigation = "\n다음 페이지: /next | 이전 페이지: /prev"
    await update.message.reply_text(message + navigation)

# 페이지 이동 (다음/이전)
async def next_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data['page'] = context.user_data.get('page', 1) + 1
    await list_items(update, context)

async def prev_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data['page'] = max(context.user_data.get('page', 1) - 1, 1)
    await list_items(update, context)
    
    # 구매자가 물품을 선택했을 때 오퍼 전송
async def send_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    item_id = int(update.message.text.strip())
    buyer_id = update.message.from_user.id

    # 선택한 물품의 정보를 조회
    item = conn.execute(text('SELECT name, price, seller_id FROM items WHERE id=:id AND status=:status'),
                        {'id': item_id, 'status': 'available'}).fetchone()

    if not item:
        await update.message.reply_text("유효하지 않은 물품 ID입니다.")
        return

    # 오퍼 등록
    conn.execute(text('INSERT INTO offers (item_id, buyer_id, status) VALUES (:item_id, :buyer_id, :status)'),
                 {'item_id': item_id, 'buyer_id': buyer_id, 'status': 'pending'})
    conn.commit()

    await update.message.reply_text(f"{item.name}에 대한 구매 오퍼를 보냈습니다. 판매자가 수락할 때까지 기다려주세요.")

    # 판매자에게 오퍼 알림
    await context.bot.send_message(
        chat_id=item.seller_id,
        text=f"'{item.name}'에 대한 구매 오퍼가 도착했습니다. 가격: {item.price} USDT\n수락하려면 /accept_{item_id}, 거절하려면 /reject_{item_id}을 입력하세요."
    )

# 판매자가 오퍼를 수락했을 때
async def accept_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    item_id = int(update.message.text.split('_')[-1])
    offer = conn.execute(text('SELECT buyer_id, item_id FROM offers WHERE item_id=:item_id AND status=:status'),
                         {'item_id': item_id, 'status': 'pending'}).fetchone()

    if not offer:
        await update.message.reply_text("유효하지 않은 오퍼입니다.")
        return

    conn.execute(text('UPDATE offers SET status=:status WHERE item_id=:item_id'),
                 {'status': 'accepted', 'item_id': item_id})
    conn.execute(text('UPDATE items SET status=:status WHERE id=:id'),
                 {'status': 'sold', 'id': item_id})
    conn.commit()

    # 구매자에게 결제 요청
    await context.bot.send_message(
        chat_id=offer.buyer_id,
        text=f"판매자가 오퍼를 수락했습니다. {offer.item_id}번 물품의 결제를 진행해주세요.\n정확한 금액을 송금하면 자동으로 확인됩니다."
    )

# 판매자가 오퍼를 거절했을 때
async def reject_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    item_id = int(update.message.text.split('_')[-1])
    offer = conn.execute(text('SELECT buyer_id FROM offers WHERE item_id=:item_id AND status=:status'),
                         {'item_id': item_id, 'status': 'pending'}).fetchone()

    if not offer:
        await update.message.reply_text("유효하지 않은 오퍼입니다.")
        return

    conn.execute(text('UPDATE offers SET status=:status WHERE item_id=:item_id'),
                 {'status': 'rejected', 'item_id': item_id})
    conn.commit()

    await context.bot.send_message(
        chat_id=offer.buyer_id,
        text="판매자가 오퍼를 거절했습니다. 다른 물품을 확인해주세요."
    )

# 결제 확인 및 처리 (테더 입금 확인)
async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    item_id = int(update.message.text.split('_')[-1])
    offer = conn.execute(text('SELECT buyer_id, item_id FROM offers WHERE item_id=:item_id AND status=:status'),
                         {'item_id': item_id, 'status': 'accepted'}).fetchone()

    if not offer:
        await update.message.reply_text("유효하지 않은 거래입니다.")
        return

    # 결제 검증 로직 (예시: 실제 트론 블록체인 API를 이용해야 함)
    # 결제 확인 후 처리 로직
    is_payment_successful = True  # 실제 구현 필요

    if is_payment_successful:
        conn.execute(text('UPDATE offers SET status=:status WHERE item_id=:item_id'),
                     {'status': 'completed', 'item_id': item_id})
        conn.commit()

        await context.bot.send_message(
            chat_id=offer.buyer_id,
            text="테더 입금이 확인되었습니다. 거래가 완료되었습니다."
        )
        await context.bot.send_message(
            chat_id=offer.buyer_id,
            text="판매자에게 물품을 전달받을 준비를 해주세요."
        )

# /rate 명령어 (거래 완료 후 평점 등록)
async def rate_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = update.message.text.split()
        user_id = int(data[1])
        rating = int(data[2])

        if not (1 <= rating <= 5):
            raise ValueError("평점은 1에서 5 사이의 정수여야 합니다.")

        conn.execute(text('INSERT INTO ratings (user_id, rating) VALUES (:user_id, :rating)'),
                     {'user_id': user_id, 'rating': rating})
        conn.commit()

        await update.message.reply_text(f"{user_id}번 사용자에게 {rating}점을 부여하였습니다.")
    except Exception as e:
        await update.message.reply_text("평점 등록에 실패하였습니다. 올바른 형식으로 입력해주세요. (/rate [유저ID] [평점])")