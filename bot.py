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
    name = Column(Text, nullable=False)  # Text 타입으로 한글 지원
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

# 명령어 안내 메시지
def command_guide() -> str:
    return (
        "\n\n사용 가능한 명령어:\n"
        "/sell - 물품 판매 등록\n"
        "/list - 구매 가능한 물품 목록\n"
        "/cancel - 판매 물품 취소\n"
        "/exit - 초기 화면으로 돌아가기"
    )

# 초기 시작 명령어
async def start(update: Update, _) -> int:
    await update.message.reply_text(
        "에스크로 거래 봇에 오신 것을 환영합니다!" + command_guide()
    )
    return ConversationHandler.END

# 판매 물품 등록 시작
async def sell(update: Update, _) -> int:
    await update.message.reply_text("판매할 물품의 이름을 입력해주세요." + command_guide())
    return WAITING_FOR_ITEM_NAME

# 물품 이름 설정
async def set_item_name(update: Update, context) -> int:
    context.user_data['item_name'] = update.message.text.strip()
    await update.message.reply_text("물품의 가격을 입력해주세요." + command_guide())
    return WAITING_FOR_PRICE

# 물품 가격 설정
async def set_item_price(update: Update, context) -> int:
    try:
        price = float(update.message.text.strip())
        context.user_data['price'] = price
        await update.message.reply_text("물품 종류를 입력해주세요. (디지털/현물)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    except ValueError:
        await update.message.reply_text("유효한 가격을 입력해주세요." + command_guide())
        return WAITING_FOR_PRICE

# 물품 유형 설정 및 데이터베이스 저장
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

# 초기 화면으로 돌아가기
async def exit_to_start(update: Update, _) -> int:
    await update.message.reply_text("초기 화면으로 돌아갑니다." + command_guide())
    return ConversationHandler.END

# 대화 종료 및 오류 처리
async def error_handler(update: Update, context) -> None:
    logging.error(msg="오류 발생", exc_info=context.error)
    await update.message.reply_text("오류가 발생했습니다. 다시 시도해주세요." + command_guide())

# 봇 애플리케이션 실행
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    sell_handler = ConversationHandler(
        entry_points=[CommandHandler("sell", sell)],
        states={
            WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
            WAITING_FOR_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
            WAITING_FOR_ITEM_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_type)],
        },
        fallbacks=[CommandHandler("exit", exit_to_start)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(sell_handler)
    app.add_handler(CommandHandler("exit", exit_to_start))
    app.add_error_handler(error_handler)

    app.run_polling()

# 구매 가능한 물품 목록 확인
async def list_items(update: Update, _) -> None:
    items = session.query(Item).filter(Item.status == "available").all()
    if not items:
        await update.message.reply_text("구매 가능한 물품이 없습니다." + command_guide())
        return

    message = "구매 가능한 물품 목록:\n"
    for item in items:
        message += f"- ID: {item.id}, 이름: {item.name}, 가격: {item.price}, 유형: {item.type}\n"
    await update.message.reply_text(message + command_guide())

# 판매 물품 취소 시작
async def cancel(update: Update, _) -> int:
    await update.message.reply_text("취소할 물품 ID를 입력해주세요." + command_guide())
    return WAITING_FOR_CANCEL_ID

# 물품 ID를 입력받아 판매 물품 취소
async def cancel_item(update: Update, context) -> int:
    try:
        item_id = int(update.message.text.strip())
        seller_id = update.message.from_user.id

        item = session.query(Item).filter_by(id=item_id, seller_id=seller_id).first()
        if not item:
            await update.message.reply_text("유효한 물품 ID를 입력해주세요." + command_guide())
            return WAITING_FOR_CANCEL_ID

        session.delete(item)
        session.commit()

        await update.message.reply_text(f"물품 ID {item_id}가 삭제되었습니다." + command_guide())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("유효한 ID를 입력해주세요." + command_guide())
        return WAITING_FOR_CANCEL_ID
    
# 구매자가 물품 선택 시 판매자에게 오퍼 전송
async def offer_item(update: Update, context) -> int:
    try:
        item_id = int(update.message.text.strip())
        item = session.query(Item).filter_by(id=item_id, status="available").first()

        if not item:
            await update.message.reply_text("유효한 물품 ID를 입력해주세요." + command_guide())
            return WAITING_FOR_ITEM_ID

        seller_id = item.seller_id
        buyer_id = update.message.from_user.id

        transaction = Transaction(
            item_id=item_id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            amount=item.price,
        )
        session.add(transaction)
        session.commit()

        await update.message.reply_text(
            f"물품 '{item.name}'에 대한 거래 요청을 보냈습니다. 판매자의 응답을 기다리세요." + command_guide()
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("유효한 물품 ID를 입력해주세요." + command_guide())
        return WAITING_FOR_ITEM_ID
    
# 입금 확인 후 구매자/판매자에게 알림
async def confirm_payment(update: Update, context) -> None:
    transaction_id = update.message.text.strip()
    transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="pending").first()

    if not transaction:
        await update.message.reply_text("유효한 거래 ID를 입력해주세요." + command_guide())
        return

    transaction.status = "completed"
    session.commit()

    await update.message.reply_text(f"거래 ID {transaction_id}가 완료되었습니다!" + command_guide())

# 평가 시스템 시작
async def rate_user(update: Update, _) -> int:
    await update.message.reply_text("평가할 사용자의 ID를 입력해주세요." + command_guide())
    return WAITING_FOR_RATING

# 사용자 평가 저장
async def save_rating(update: Update, context) -> int:
    try:
        user_id = int(update.message.text.strip())
        await update.message.reply_text("평점 (1-5)을 입력해주세요." + command_guide())
        context.user_data['rating_user_id'] = user_id
        return WAITING_FOR_CONFIRMATION
    except ValueError:
        await update.message.reply_text("유효한 사용자 ID를 입력해주세요." + command_guide())
        return WAITING_FOR_RATING
    
# 구매자와 판매자 간 안전한 채팅 및 파일 전송
async def safe_chat(update: Update, _) -> None:
    await update.message.reply_text("상대방과 안전하게 메시지를 전송할 수 있습니다." + command_guide())

# 전체 대화 흐름 설정
conversation_handler = ConversationHandler(
    entry_points=[CommandHandler("sell", sell), CommandHandler("cancel", cancel)],
    states={
        WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
        WAITING_FOR_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
        WAITING_FOR_ITEM_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_type)],
        WAITING_FOR_ITEM_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, offer_item)],
        WAITING_FOR_CANCEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_item)],
        WAITING_FOR_RATING: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rating)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start)],
)

# 봇 초기화
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_items))
    app.add_handler(conversation_handler)
    app.add_handler(CommandHandler("confirm", confirm_payment))
    app.add_handler(CommandHandler("rate", rate_user))
    app.add_handler(CommandHandler("chat", safe_chat))
    app.add_handler(CommandHandler("exit", exit_to_start))
    app.add_error_handler(error_handler)

    app.run_polling()