import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)
from telegram.constants import ParseMode

from sqlalchemy import create_engine, Column, Integer, String, DECIMAL, BigInteger, Text, TIMESTAMP, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from tronpy import Tron
from tronpy.providers import HTTPProvider

import os
import random

# 환경 변수 설정
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
TRON_API = os.getenv("TRON_API")
TRON_WALLET = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"

# SQLAlchemy 데이터베이스 설정
engine = create_engine(
    DATABASE_URL,
    echo=True,
    connect_args={"options": "-c timezone=utc"},
    future=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
session = SessionLocal()

Base = declarative_base()

# 데이터베이스 모델 정의
class Item(Base):
    __tablename__ = 'items'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, nullable=False)
    price = Column(DECIMAL, nullable=False)
    seller_id = Column(BigInteger, nullable=False)
    status = Column(String, default='available')
    type = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))

class Transaction(Base):
    __tablename__ = 'transactions'

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, nullable=False)
    buyer_id = Column(BigInteger, nullable=False)
    seller_id = Column(BigInteger, nullable=False)
    status = Column(String, default='pending')
    session_id = Column(Text)
    transaction_id = Column(Text)
    amount = Column(DECIMAL, nullable=False)
    created_at = Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))

class Rating(Base):
    __tablename__ = 'ratings'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, nullable=False)
    score = Column(Integer, nullable=False)
    review = Column(Text)
    created_at = Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))

Base.metadata.create_all(bind=engine)

# Tron 클라이언트 설정
client = Tron(provider=HTTPProvider(TRON_API))

# 로깅 설정
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# 봇 상태 정의
(
    WAITING_FOR_ITEM_NAME,
    WAITING_FOR_PRICE,
    WAITING_FOR_ITEM_TYPE,
    WAITING_FOR_ITEM_ID,
    WAITING_FOR_CANCEL_ID,
    WAITING_FOR_RATING,
    WAITING_FOR_CONFIRMATION,
) = range(7)

def command_guide() -> str:
    return (
        "\n\n사용 가능한 명령어:\n"
        "/sell - 물품 판매 등록\n"
        "/list - 구매 가능한 물품 목록\n"
        "/cancel - 판매 물품 취소\n"
        "/search - 물품 검색\n"
        "/exit - 초기 화면으로 돌아가기"
    )

async def start(update: Update, _) -> int:
    await update.message.reply_text(
        "에스크로 거래 봇에 오신 것을 환영합니다!" + command_guide()
    )
    return ConversationHandler.END

async def sell(update: Update, _) -> int:
    await update.message.reply_text("판매할 물품의 이름을 입력해주세요." + command_guide())
    return WAITING_FOR_ITEM_NAME

async def set_item_name(update: Update, context) -> int:
    context.user_data['item_name'] = update.message.text.strip()
    await update.message.reply_text("물품의 가격을 입력해주세요. (숫자만 입력)" + command_guide())
    return WAITING_FOR_PRICE

async def set_item_price(update: Update, context) -> int:
    try:
        price = float(update.message.text.strip())
        context.user_data['price'] = price
        await update.message.reply_text("물품 종류를 입력해주세요. (디지털/현물)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    except ValueError:
        await update.message.reply_text("유효한 가격을 입력해주세요." + command_guide())
        return WAITING_FOR_PRICE

async def set_item_type(update: Update, context) -> int:
    item_type = update.message.text.strip().lower()
    if item_type not in ["디지털", "현물"]:
        await update.message.reply_text("유효한 종류를 입력해주세요. (디지털/현물)" + command_guide())
        return WAITING_FOR_ITEM_TYPE

    item_name = context.user_data['item_name']
    price = context.user_data['price']
    seller_id = update.message.from_user.id

    new_item = Item(name=item_name, price=price, seller_id=seller_id, type=item_type)
    session.add(new_item)
    session.commit()

    await update.message.reply_text(f"'{item_name}'을(를) 등록하였습니다!" + command_guide())
    return ConversationHandler.END

ITEMS_PER_PAGE = 10  # 한 페이지에 보여줄 물품 수

async def list_items(update: Update, context) -> None:
    page = context.user_data.get('page', 1)
    items = session.query(Item).filter(Item.status == "available").all()
    
    if not items:
        await update.message.reply_text("구매 가능한 물품이 없습니다." + command_guide())
        return

    total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
    page = max(1, min(page, total_pages))

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    items_on_page = items[start:end]

    message = f"구매 가능한 물품 목록 (페이지 {page}/{total_pages}):\n"
    for index, item in enumerate(items_on_page, start=1):
        message += f"{index}. {item.name} - {item.price} 테더 ({item.type})\n"

    message += "\n다음 페이지: /next\n이전 페이지: /prev"
    message += "\n거래 요청: /offer 물품 이름 또는 /offer 물품 번호"

    context.user_data['page'] = page
    await update.message.reply_text(message + command_guide())

async def next_page(update: Update, context) -> None:
    current_page = context.user_data.get('page', 1)
    context.user_data['page'] = current_page + 1
    await list_items(update, context)

async def prev_page(update: Update, context) -> None:
    current_page = context.user_data.get('page', 1)
    context.user_data['page'] = current_page - 1
    await list_items(update, context)

async def cancel(update: Update, context) -> int:
    page = context.user_data.get('page', 1)
    seller_id = update.message.from_user.id
    items = session.query(Item).filter(Item.seller_id == seller_id).all()

    if not items:
        await update.message.reply_text("등록된 물품이 없습니다." + command_guide())
        return ConversationHandler.END

    total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
    page = max(1, min(page, total_pages))

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    items_on_page = items[start:end]

    message = f"취소 가능한 물품 목록 (페이지 {page}/{total_pages}):\n"
    for index, item in enumerate(items_on_page, start=1):
        message += f"{index}. {item.name} - {item.price} 테더 ({item.type})\n"

    message += "\n다음 페이지: /next\n이전 페이지: /prev"
    message += "\n취소할 물품 ID나 이름을 입력해주세요."

    context.user_data['page'] = page
    return WAITING_FOR_CANCEL_ID

async def cancel_item(update: Update, context) -> int:
    try:
        item_identifier = update.message.text.strip()
        seller_id = update.message.from_user.id

        item = session.query(Item).filter(
            (Item.id == int(item_identifier)) | (Item.name == item_identifier),
            Item.seller_id == seller_id
        ).first()

        if not item:
            await update.message.reply_text("유효한 물품 ID나 이름을 입력해주세요." + command_guide())
            return WAITING_FOR_CANCEL_ID

        session.delete(item)
        session.commit()

        await update.message.reply_text(f"물품 '{item.name}'가 삭제되었습니다." + command_guide())
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("유효한 ID나 물품 이름을 입력해주세요." + command_guide())
        return WAITING_FOR_CANCEL_ID
    
def generate_transaction_id() -> str:
    return ''.join([str(random.randint(0, 9)) for _ in range(12)])

async def offer_item(update: Update, context) -> int:
    try:
        item_identifier = update.message.text.strip()
        item = session.query(Item).filter(
            (Item.id == int(item_identifier)) | (Item.name == item_identifier),
            Item.status == "available"
        ).first()

        if not item:
            await update.message.reply_text("유효한 물품 ID나 이름을 입력해주세요." + command_guide())
            return WAITING_FOR_ITEM_ID

        transaction_id = generate_transaction_id()
        seller_id = item.seller_id
        buyer_id = update.message.from_user.id

        transaction = Transaction(
            item_id=item.id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            amount=item.price,
            transaction_id=transaction_id,
        )
        session.add(transaction)
        session.commit()

        await update.message.reply_text(
            f"'{item.name}'에 대한 거래 요청을 보냈습니다! 거래 ID: {transaction_id}" + command_guide()
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("유효한 물품 ID나 이름을 입력해주세요." + command_guide())
        return WAITING_FOR_ITEM_ID
    
async def accept_transaction(update: Update, context) -> None:
    transaction_id = update.message.text.strip()
    transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="pending").first()

    if not transaction:
        await update.message.reply_text("유효한 거래 ID를 입력해주세요." + command_guide())
        return

    transaction.status = "accepted"
    session.commit()

    await update.message.reply_text(f"거래 ID {transaction_id}가 수락되었습니다!" + command_guide())

async def refuse_transaction(update: Update, context) -> None:
    transaction_id = update.message.text.strip()
    transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="pending").first()

    if not transaction:
        await update.message.reply_text("유효한 거래 ID를 입력해주세요." + command_guide())
        return

    session.delete(transaction)
    session.commit()

    await update.message.reply_text(f"거래 ID {transaction_id}가 거절되었습니다!" + command_guide())

COMMISSION_RATE = 0.05  # 중개 수수료 5%

async def confirm_payment(update: Update, context) -> None:
    transaction_id = update.message.text.strip()
    transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="accepted").first()

    if not transaction:
        await update.message.reply_text("유효한 거래 ID를 입력해주세요." + command_guide())
        return

    net_amount = transaction.amount * (1 - COMMISSION_RATE)
    
    # Tron Wallet 송금 로직 추가 필요
    # 예시: client.trx.transfer(sender_wallet, receiver_wallet, net_amount)

    transaction.status = "completed"
    session.commit()

    await update.message.reply_text(f"거래 ID {transaction_id}가 완료되었습니다!" + command_guide())

async def rate_user(update: Update, context) -> int:
    transaction_id = update.message.text.strip()
    transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="completed").first()

    if not transaction:
        await update.message.reply_text("유효한 거래 ID를 입력해주세요." + command_guide())
        return WAITING_FOR_RATING

    context.user_data['transaction_id'] = transaction_id
    await update.message.reply_text("평가할 사용자에 대해 1~5점의 평점을 입력해주세요." + command_guide())
    return WAITING_FOR_CONFIRMATION

async def save_rating(update: Update, context) -> int:
    try:
        score = int(update.message.text.strip())
        if score < 1 or score > 5:
            await update.message.reply_text("평점은 1에서 5 사이의 숫자여야 합니다." + command_guide())
            return WAITING_FOR_CONFIRMATION

        transaction_id = context.user_data.get('transaction_id')
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id).first()

        user_id = transaction.buyer_id if update.message.from_user.id == transaction.seller_id else transaction.seller_id
        
        new_rating = Rating(user_id=user_id, score=score, review="익명 평가")
        session.add(new_rating)
        session.commit()

        await update.message.reply_text(f"평점 {score}점을 등록했습니다!" + command_guide())
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("유효한 숫자를 입력해주세요." + command_guide())
        return WAITING_FOR_CONFIRMATION
    
user_chat_sessions = {}

async def start_chat(update: Update, context) -> None:
    transaction_id = update.message.text.strip()
    transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="accepted").first()

    if not transaction:
        await update.message.reply_text("유효한 거래 ID를 입력해주세요." + command_guide())
        return

    buyer_id = transaction.buyer_id
    seller_id = transaction.seller_id

    user_chat_sessions[buyer_id] = seller_id
    user_chat_sessions[seller_id] = buyer_id

    await update.message.reply_text("익명 채팅을 시작합니다. 메시지를 입력해주세요." + command_guide())

async def relay_message(update: Update, context) -> None:
    sender_id = update.message.from_user.id
    recipient_id = user_chat_sessions.get(sender_id)

    if not recipient_id:
        await update.message.reply_text("채팅을 시작할 수 없습니다. 유효한 거래 ID가 필요합니다." + command_guide())
        return

    await context.bot.send_message(chat_id=recipient_id, text=update.message.text)

async def end_chat(transaction_id: str) -> None:
    transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="completed").first()

    if transaction:
        buyer_id = transaction.buyer_id
        seller_id = transaction.seller_id

        user_chat_sessions.pop(buyer_id, None)
        user_chat_sessions.pop(seller_id, None)

async def search_items(update: Update, context) -> None:
    search_query = update.message.text.strip().lower().replace("/search ", "")
    items = session.query(Item).filter(Item.name.ilike(f"%{search_query}%"), Item.status == "available").all()
    
    if not items:
        await update.message.reply_text("검색된 물품이 없습니다." + command_guide())
        return

    total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
    page = 1

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    items_on_page = items[start:end]

    message = f"'{search_query}' 검색 결과 (페이지 {page}/{total_pages}):\n"
    for index, item in enumerate(items_on_page, start=1):
        message += f"{index}. {item.name} - {item.price} 테더 ({item.type})\n"

    message += "\n다음 페이지: /next\n이전 페이지: /prev"
    message += "\n거래 요청: /offer 물품 이름 또는 /offer 물품 번호"

    context.user_data['page'] = page
    context.user_data['search_query'] = search_query
    await update.message.reply_text(message + command_guide())

async def next_search_page(update: Update, context) -> None:
    search_query = context.user_data.get('search_query', '')
    page = context.user_data.get('page', 1) + 1

    items = session.query(Item).filter(Item.name.ilike(f"%{search_query}%"), Item.status == "available").all()

    total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
    page = max(1, min(page, total_pages))

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    items_on_page = items[start:end]

    message = f"'{search_query}' 검색 결과 (페이지 {page}/{total_pages}):\n"
    for index, item in enumerate(items_on_page, start=1):
        message += f"{index}. {item.name} - {item.price} 테더 ({item.type})\n"

    message += "\n다음 페이지: /next\n이전 페이지: /prev"
    message += "\n거래 요청: /offer 물품 이름 또는 /offer 물품 번호"

    context.user_data['page'] = page
    await update.message.reply_text(message + command_guide())

async def off_transaction(update: Update, context) -> None:
    transaction_id = update.message.text.strip()
    transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="accepted").first()

    if not transaction:
        await update.message.reply_text("유효한 거래 ID를 입력해주세요." + command_guide())
        return

    transaction.status = "cancelled"
    session.commit()

    await update.message.reply_text(f"거래 ID {transaction_id}가 중단되었습니다!" + command_guide())
    await end_chat(transaction_id)

async def exit_to_start(update: Update, context) -> int:
    if update.message.from_user.id in user_chat_sessions:
        await update.message.reply_text("거래 중 또는 채팅 중에는 /exit 명령어를 사용할 수 없습니다." + command_guide())
        return ConversationHandler.END

    await update.message.reply_text("초기 화면으로 돌아갑니다." + command_guide())
    context.user_data.clear()
    return ConversationHandler.END

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("list", list_items))
app.add_handler(CommandHandler("next", next_page))
app.add_handler(CommandHandler("prev", prev_page))
app.add_handler(CommandHandler("search", search_items))
app.add_handler(CommandHandler("accept", accept_transaction))
app.add_handler(CommandHandler("refusal", refuse_transaction))
app.add_handler(CommandHandler("confirm", confirm_payment))
app.add_handler(CommandHandler("rate", rate_user))
app.add_handler(CommandHandler("off", off_transaction))
app.add_handler(CommandHandler("chat", start_chat))
app.add_handler(CommandHandler("exit", exit_to_start))

# 메시지 리레이 기능 (익명 채팅)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, relay_message))