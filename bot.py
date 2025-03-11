import logging
import random
import os
import time
import requests
from requests.adapters import HTTPAdapter, Retry

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)
from telegram.constants import ParseMode

from sqlalchemy import create_engine, Column, Integer, String, DECIMAL, BigInteger, Text, TIMESTAMP, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session

from tronpy import Tron
from tronpy.providers import HTTPProvider

# ==============================
# 환경 변수 설정 (Fly.io 시크릿 등에서 주입)
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
# DATABASE_URL 예: "postgresql://postgres:비밀번호@escrow-bot-db.flycast:5432/escrow_bot?sslmode=disable"
DATABASE_URL = os.getenv("DATABASE_URL")
TRON_API = os.getenv("TRON_API")            # 예: "https://api.trongrid.io"
TRON_API_KEY = os.getenv("TRON_API_KEY")      # TronGrid API Key
TRON_WALLET = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"  # 봇의 Tron 지갑 주소
PRIVATE_KEY = os.getenv("PRIVATE_KEY")        # 봇 지갑의 개인키

# TRC20 USDT 컨트랙트 주소 (메인넷 예시 – 실제 사용하는 주소로 교체)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# ==============================
# requests 세션 설정 (재시도/타임아웃)
http_session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
http_adapter = HTTPAdapter(max_retries=retries)
http_session.mount("https://", http_adapter)
http_session.mount("http://", http_adapter)

# ==============================
# SQLAlchemy 설정 (pool_pre_ping 옵션 추가)
engine = create_engine(
    DATABASE_URL,
    echo=True,
    connect_args={"options": "-c timezone=utc"},
    future=True,
    pool_pre_ping=True,
)
SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()

def get_db_session():
    return SessionLocal()

# ==============================
# 데이터베이스 모델
class Item(Base):
    __tablename__ = 'items'
    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, nullable=False)  # 한글 지원
    price = Column(DECIMAL, nullable=False)
    seller_id = Column(BigInteger, nullable=False)
    status = Column(String, default='available')  # available, sold 등
    type = Column(String, nullable=False)         # 디지털 / 현물
    created_at = Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))

class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, nullable=False)
    buyer_id = Column(BigInteger, nullable=False)
    seller_id = Column(BigInteger, nullable=False)
    status = Column(String, default='pending')  # pending, accepted, completed, cancelled, rejected
    session_id = Column(Text)  # 판매자가 /accept 시 입력한 출금(송금) 지갑 주소 또는 환불용 구매자 지갑
    transaction_id = Column(Text, unique=True)  # 12자리 랜덤 거래 id
    amount = Column(DECIMAL, nullable=False)
    created_at = Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))

class Rating(Base):
    __tablename__ = 'ratings'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, nullable=False)  # 평가 대상 (봇만 보관)
    score = Column(Integer, nullable=False)
    review = Column(Text)
    created_at = Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))

Base.metadata.create_all(bind=engine)

# ==============================
# Tron 클라이언트 설정 (HTTPProvider는 이제 api_key 인자를 사용)
client = Tron(provider=HTTPProvider(TRON_API, api_key=TRON_API_KEY))

# 송금(USDT 전송) 로직
def check_usdt_payment(expected_amount: float, txid: str, internal_txid: str) -> (bool, float):
    """
    주어진 블록체인 txid에서 전송 금액과 메모(참조)를 검증합니다.
    반환: (검증성공 여부, 실제 전송된 USDT 금액)
    """
    try:
        tx = client.get_transaction(txid)
        # TRC20 전송은 TriggerSmartContract를 통해 이루어집니다.
        contract_info = tx["raw_data"]["contract"][0]["parameter"]["value"]
        transferred_amount = int(contract_info.get("amount", 0))
        # data 필드에 거래 id가 hex로 인코딩되어 있을 수 있음 (없으면 빈 문자열)
        data_hex = contract_info.get("data", "")
        memo = bytes.fromhex(data_hex).decode("utf-8") if data_hex else ""
        # 전송 금액 검증 (정확히 expected_amount, 1e6 단위)
        if transferred_amount != int(expected_amount * 1e6):
            return (False, transferred_amount / 1e6)
        # 메모에 내부 거래 ID가 반드시 포함되어 있어야 함
        if internal_txid not in memo:
            return (False, transferred_amount / 1e6)
        return (True, transferred_amount / 1e6)
    except Exception as e:
        logging.error(f"블록체인 거래 검증 오류: {e}")
        return (False, 0)

def send_usdt(to_address: str, amount: float) -> dict:
    try:
        contract = client.get_contract(USDT_CONTRACT)
        txn = (
            contract.functions.transfer(to_address, int(amount * 1e6))
            .with_owner(TRON_WALLET)
            .fee_limit(1_000_000_000)
            .build()
            .sign(PRIVATE_KEY)
            .broadcast()
        )
        result = txn.wait()  # 송금 결과 대기
        return result
    except Exception as e:
        logging.error(f"TRC20 송금 오류: {e}")
        raise

# ==============================
# 로깅 설정
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# 대화 상태 상수
(
    WAITING_FOR_ITEM_NAME,
    WAITING_FOR_PRICE,
    WAITING_FOR_ITEM_TYPE,
    WAITING_FOR_ITEM_ID,       # /offer 입력 대기
    WAITING_FOR_CANCEL_ID,     # /cancel 입력 대기
    WAITING_FOR_RATING,        # /rate 거래ID 입력 후 평점 대기
    WAITING_FOR_CONFIRMATION,  # /rate 평점 최종 입력 상태
    WAITING_FOR_REFUND_WALLET, # /refund 환불 지갑 입력 대기
) = range(8)

ITEMS_PER_PAGE = 10  # 한 페이지에 보여줄 상품 수

# 거래 및 채팅 관련 전역 변수 (메모리 내 관리)
active_chats = {}  # {transaction_id: (buyer_id, seller_id)}

def command_guide() -> str:
    return (
        "\n\n사용 가능한 명령어:\n"
        "/sell - 상품 판매 등록\n"
        "/list - 구매 가능한 상품 목록 조회\n"
        "/cancel - 본인이 등록한 상품 취소\n"
        "/search - 상품 검색\n"
        "/offer - 거래 요청 (목록/검색 후 사용)\n"
        "/accept - 거래 요청 수락 (판매자 전용, 사용법: /accept 거래ID 판매자지갑주소 [TRC20 USDT])\n"
        "/refusal - 거래 요청 거절 (판매자 전용, 사용법: /refusal 거래ID)\n"
        "/confirm - 거래 완료 확인 (구매자 전용, 사용법: /confirm 거래ID 구매자지갑주소 블록체인txID)\n"
        "/refund - 거래 취소 시 환불 요청 (구매자 전용, 사용법: /refund 거래ID)\n"
        "/rate - 거래 종료 후 평점 남기기 (사용법: /rate 거래ID)\n"
        "/chat - 거래 당사자 간 익명 채팅 (사용법: /chat 거래ID)\n"
        "/off - 거래 중단 (구매자/판매자 모두, 사용법: /off 거래ID)\n"
        "/exit - 대화 종료 및 초기화 (거래/채팅/평가 중 제외)"
    )

# ==============================
# 1. /start: 초기 안내
async def start(update: Update, _) -> int:
    await update.message.reply_text("에스크로 거래 봇에 오신 것을 환영합니다!" + command_guide())
    return ConversationHandler.END

# ==============================
# 2. /sell: 상품 판매 등록 대화
async def sell_command(update: Update, _) -> int:
    await update.message.reply_text("판매할 상품의 이름을 입력해주세요." + command_guide())
    return WAITING_FOR_ITEM_NAME

async def set_item_name(update: Update, context) -> int:
    context.user_data['item_name'] = update.message.text.strip()
    await update.message.reply_text("상품의 가격(테더)을 숫자로 입력해주세요." + command_guide())
    return WAITING_FOR_PRICE

async def set_item_price(update: Update, context) -> int:
    try:
        price = float(update.message.text.strip())
        context.user_data['price'] = price
        await update.message.reply_text("상품 종류를 입력해주세요. (디지털/현물)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    except ValueError:
        await update.message.reply_text("유효한 가격을 입력해주세요. 숫자만 입력해주세요." + command_guide())
        return WAITING_FOR_PRICE

async def set_item_type(update: Update, context) -> int:
    item_type = update.message.text.strip().lower()
    if item_type not in ["디지털", "현물"]:
        await update.message.reply_text("유효한 종류를 입력해주세요. (디지털/현물)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    item_name = context.user_data['item_name']
    price = context.user_data['price']
    seller_id = update.message.from_user.id
    session = get_db_session()
    try:
        new_item = Item(name=item_name, price=price, seller_id=seller_id, type=item_type)
        session.add(new_item)
        session.commit()
        await update.message.reply_text(f"'{item_name}'이(가) 등록되었습니다!" + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"상품 등록 오류: {e}")
        await update.message.reply_text("상품 등록 중 오류가 발생했습니다. 다시 시도해주세요." + command_guide())
    finally:
        session.close()
    return ConversationHandler.END

# ==============================
# 3. /list: 상품 목록 조회 및 페이지네이션
async def list_items_command(update: Update, context) -> None:
    session = get_db_session()
    try:
        page = context.user_data.get('list_page', 1)
        items = session.query(Item).filter(Item.status == "available").all()
        if not items:
            await update.message.reply_text("등록된 상품이 없습니다." + command_guide())
            return
        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        if page < 1:
            page = total_pages
        elif page > total_pages:
            page = 1
        context.user_data['list_page'] = page
        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        context.user_data['list_mapping'] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}
        message = f"구매 가능한 상품 목록 (페이지 {page}/{total_pages}):\n"
        for idx, item in enumerate(page_items, start=1):
            message += f"{idx}. {item.name} - {item.price} 테더 ({item.type})\n"
        message += "\n페이지 이동: /next 또는 /prev"
        message += "\n거래 요청은 /offer [상품번호 또는 상품이름]을 입력해주세요."
        await update.message.reply_text(message + command_guide())
    except Exception as e:
        logging.error(f"/list 오류: {e}")
        await update.message.reply_text("상품 목록 조회 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

async def next_page(update: Update, context) -> None:
    current = context.user_data.get('list_page', 1)
    context.user_data['list_page'] = current + 1
    await list_items_command(update, context)

async def prev_page(update: Update, context) -> None:
    current = context.user_data.get('list_page', 1)
    context.user_data['list_page'] = current - 1
    await list_items_command(update, context)

# ==============================
# 4. /search: 상품 검색 (페이지네이션 포함)
async def search_items_command(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("검색어를 입력해주세요. 예: /search 금쪽이" + command_guide())
        return
    query = args[1].strip().lower()
    context.user_data['search_query'] = query
    context.user_data['search_page'] = 1
    await list_search_results(update, context)

async def list_search_results(update: Update, context) -> None:
    session = get_db_session()
    try:
        query = context.user_data.get('search_query', '')
        page = context.user_data.get('search_page', 1)
        items = session.query(Item).filter(Item.name.ilike(f"%{query}%"), Item.status == "available").all()
        if not items:
            await update.message.reply_text(f"'{query}' 검색 결과가 없습니다." + command_guide())
            return
        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        if page < 1:
            page = total_pages
        elif page > total_pages:
            page = 1
        context.user_data['search_page'] = page
        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        context.user_data['search_mapping'] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}
        message = f"'{query}' 검색 결과 (페이지 {page}/{total_pages}):\n"
        for idx, item in enumerate(page_items, start=1):
            message += f"{idx}. {item.name} - {item.price} 테더 ({item.type})\n"
        message += "\n페이지 이동: /next 또는 /prev"
        message += "\n거래 요청은 /offer [상품번호 또는 상품이름]을 입력해주세요."
        await update.message.reply_text(message + command_guide())
    except Exception as e:
        logging.error(f"/search 오류: {e}")
        await update.message.reply_text("상품 검색 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 5. /offer: 거래 요청 (목록/검색 후)
def generate_transaction_id() -> str:
    return ''.join([str(random.randint(0, 9)) for _ in range(12)])

async def offer_item(update: Update, context) -> None:
    session = get_db_session()
    try:
        args = update.message.text.split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text("상품 번호나 이름을 입력해주세요. 예: /offer 금쪽이" + command_guide())
            return
        identifier = args[1].strip()
        mapping = context.user_data.get('list_mapping') or context.user_data.get('search_mapping') or {}
        if identifier in mapping:
            item_id = mapping[identifier]
            item = session.query(Item).filter_by(id=item_id, status="available").first()
        else:
            try:
                item = session.query(Item).filter(
                    (Item.id == int(identifier)) | (Item.name.ilike(f"%{identifier}%")),
                    Item.status == "available"
                ).first()
            except ValueError:
                item = session.query(Item).filter(
                    Item.name.ilike(f"%{identifier}%"),
                    Item.status == "available"
                ).first()
        if not item:
            await update.message.reply_text("유효한 상품 번호나 이름을 입력해주세요." + command_guide())
            return
        buyer_id = update.message.from_user.id
        seller_id = item.seller_id
        transaction_id = generate_transaction_id()
        new_transaction = Transaction(
            item_id=item.id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            amount=item.price,
            transaction_id=transaction_id,
        )
        session.add(new_transaction)
        session.commit()
        await update.message.reply_text(
            f"상품 '{item.name}'에 대한 거래 요청이 전송되었습니다! 거래 ID: {transaction_id}\n※ 송금 시 반드시 거래 ID를 메모(참조)에 입력해주세요." + command_guide()
        )
        try:
            await context.bot.send_message(
                chat_id=seller_id,
                text=f"당신의 상품 '{item.name}'에 거래 요청이 도착했습니다. 거래 ID: {transaction_id}\n판매자께서는 /accept 거래ID 판매자지갑주소 TRC20 USDT로 응답해주세요.\n또는 /refusal 거래ID를 입력해주세요."
            )
        except Exception as e:
            logging.error(f"판매자 알림 전송 오류: {e}")
    except Exception as e:
        logging.error(f"/offer 오류: {e}")
        await update.message.reply_text("거래 요청 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 6. /cancel: 본인이 등록한 상품 취소 (페이지네이션 포함)
async def cancel(update: Update, context) -> int:
    session = get_db_session()
    try:
        seller_id = update.message.from_user.id
        items = session.query(Item).filter(Item.seller_id == seller_id).all()
        if not items:
            await update.message.reply_text("등록된 상품이 없습니다." + command_guide())
            return ConversationHandler.END
        page = context.user_data.get('cancel_page', 1)
        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        if page < 1:
            page = total_pages
        elif page > total_pages:
            page = 1
        context.user_data['cancel_page'] = page
        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        context.user_data['cancel_mapping'] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}
        message = f"취소 가능한 상품 목록 (페이지 {page}/{total_pages}):\n"
        for idx, item in enumerate(page_items, start=1):
            message += f"{idx}. {item.name} - {item.price} 테더 ({item.type})\n"
        message += "\n페이지 이동: /next 또는 /prev"
        message += "\n취소할 상품의 번호 또는 상품이름을 입력해주세요."
        await update.message.reply_text(message + command_guide())
        return WAITING_FOR_CANCEL_ID
    except Exception as e:
        logging.error(f"/cancel 오류: {e}")
        await update.message.reply_text("상품 취소 목록 조회 중 오류가 발생했습니다." + command_guide())
        return ConversationHandler.END
    finally:
        session.close()

async def cancel_item(update: Update, context) -> int:
    session = get_db_session()
    try:
        identifier = update.message.text.strip()
        seller_id = update.message.from_user.id
        mapping = context.user_data.get('cancel_mapping') or {}
        if identifier in mapping:
            item_id = mapping[identifier]
            item = session.query(Item).filter_by(id=item_id, seller_id=seller_id).first()
        else:
            try:
                item = session.query(Item).filter(Item.id == int(identifier), Item.seller_id == seller_id).first()
            except ValueError:
                item = session.query(Item).filter(Item.name.ilike(f"%{identifier}%"), Item.seller_id == seller_id).first()
        if not item:
            await update.message.reply_text("유효한 상품 번호나 이름을 입력해주세요." + command_guide())
            return WAITING_FOR_CANCEL_ID
        session.delete(item)
        session.commit()
        await update.message.reply_text(f"상품 '{item.name}'이(가) 삭제되었습니다." + command_guide())
        return ConversationHandler.END
    except Exception as e:
        session.rollback()
        logging.error(f"/cancel 처리 오류: {e}")
        await update.message.reply_text("상품 취소 처리 중 오류가 발생했습니다." + command_guide())
        return WAITING_FOR_CANCEL_ID
    finally:
        session.close()

# ==============================
# 7. /accept 및 /refusal: 거래 요청 수락/거절 (판매자 전용)
async def accept_transaction(update: Update, context) -> None:
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("사용법: /accept 거래ID 판매자지갑주소\n예: /accept 123456789012 TXXXXXXXXXXXXXXXXXXXX" + command_guide())
        return
    transaction_id = args[1].strip()
    seller_wallet = args[2].strip()
    # 네트워크는 무조건 TRC20 USDT입니다.
    session = get_db_session()
    try:
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="pending").first()
        if not transaction:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        if update.message.from_user.id != transaction.seller_id:
            await update.message.reply_text("판매자만 이 명령어를 사용할 수 있습니다." + command_guide())
            return
        transaction.session_id = seller_wallet  # 판매자가 제공한 지갑 주소 저장
        transaction.status = "accepted"
        session.commit()
        await update.message.reply_text(f"거래 ID {transaction_id}가 수락되었습니다. (네트워크: TRC20 USDT)\n구매자에게 송금 안내를 보냅니다." + command_guide())
        try:
            await context.bot.send_message(
                chat_id=transaction.buyer_id,
                text=f"거래 ID {transaction_id}가 수락되었습니다.\n해당 금액을 {TRON_WALLET}로 송금해주세요.\n※ 송금 시 반드시 거래 ID를 메모(참조)에 포함해 주세요!"
            )
        except Exception as e:
            logging.error(f"구매자 알림 전송 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/accept 오류: {e}")
    finally:
        session.close()

async def refusal_transaction(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refusal 거래ID\n예: /refusal 123456789012" + command_guide())
        return
    transaction_id = args[1].strip()
    session = get_db_session()
    try:
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="pending").first()
        if not transaction:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        session.delete(transaction)
        session.commit()
        await update.message.reply_text(f"거래 ID {transaction_id}가 거절되었습니다." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/refusal 오류: {e}")
    finally:
        session.close()

# ==============================
# 8. /confirm: 거래 완료 확인 (구매자 전용, 송금 실행 및 검증)
# 사용법: /confirm 거래ID 구매자지갑주소 블록체인txID
# - 블록체인 txID로 실제 입금된 금액과 메모(거래ID 포함)를 검증합니다.
# - 만약 입금 금액이 정확하지 않으면 구매자에게 전액 환불 후 재송금 안내를 합니다.
async def confirm_payment(update: Update, context) -> None:
    args = update.message.text.split()
    if len(args) < 4:
        await update.message.reply_text("사용법: /confirm 거래ID 구매자지갑주소 블록체인txID\n예: /confirm 123456789012 TYYYYYYYYYYYYYYYYYYY abcdef1234567890" + command_guide())
        return
    transaction_id = args[1].strip()
    buyer_wallet = args[2].strip()
    blockchain_txid = args[3].strip()
    session = get_db_session()
    try:
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="accepted").first()
        if not transaction:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        if update.message.from_user.id != transaction.buyer_id:
            await update.message.reply_text("구매자만 이 명령어를 사용할 수 있습니다." + command_guide())
            return
        expected_amount = float(transaction.amount)
        # 검증: 블록체인 거래의 전송 금액과 메모(거래ID 포함) 확인
        valid, deposited_amount = verify_deposit(expected_amount, blockchain_txid, transaction_id)
        if not valid:
            # 만약 금액이 다르다면 환불 처리 (송금 수수료는 차감됨)
            refund_result = send_usdt(buyer_wallet, deposited_amount)
            await update.message.reply_text(
                f"입금된 금액({deposited_amount} 테더)이 예상({expected_amount} 테더)과 일치하지 않습니다.\n전액 환불 처리되었습니다. 송금 결과: {refund_result}\n정확한 금액을 다시 송금해주세요."
                + command_guide()
            )
            return
        # 입금이 정확한 경우
        # 구매자에게 입금 확인 메시지 전송
        await update.message.reply_text(
            f"입금이 확인되었습니다. 거래 ID {transaction_id}가 완료 처리됩니다." + command_guide()
        )
        # 거래 완료 처리: 중개 수수료 차감 후 판매자에게 송금
        net_amount = expected_amount * (1 - COMMISSION_RATE)
        transaction.status = "completed"
        session.commit()
        try:
            seller_wallet = transaction.session_id  # 판매자가 /accept 시 입력한 지갑 주소
            result = send_usdt(seller_wallet, net_amount)
            await context.bot.send_message(
                chat_id=transaction.seller_id,
                text=f"거래 ID {transaction_id}가 완료되었습니다. {net_amount} 테더가 판매자 지갑({seller_wallet})으로 송금되었습니다.\n거래 결과: {result}"
            )
        except Exception as e:
            logging.error(f"판매자 송금 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/confirm 오류: {e}")
    finally:
        session.close()

def verify_deposit(expected_amount: float, txid: str, internal_txid: str) -> (bool, float):
    """
    블록체인에서 거래(txid)를 조회하여 전송 금액과 메모(참조)에 내부 거래 ID(internal_txid)가 포함되어 있는지 검증합니다.
    반환: (검증성공 여부, 실제 전송된 테더 금액)
    """
    try:
        tx = client.get_transaction(txid)
        contract_info = tx["raw_data"]["contract"][0]["parameter"]["value"]
        transferred_amount = int(contract_info.get("amount", 0))
        data_hex = contract_info.get("data", "")
        memo = bytes.fromhex(data_hex).decode("utf-8") if data_hex else ""
        if transferred_amount != int(expected_amount * 1e6):
            return (False, transferred_amount / 1e6)
        if internal_txid not in memo:
            return (False, transferred_amount / 1e6)
        return (True, transferred_amount / 1e6)
    except Exception as e:
        logging.error(f"블록체인 거래 검증 오류: {e}")
        return (False, 0)

# ==============================
# 9. /rate: 거래 종료 후 평점 남기기 (익명)
async def rate_user(update: Update, context) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /rate 거래ID\n예: /rate 123456789012" + command_guide())
        return WAITING_FOR_RATING
    transaction_id = args[1].strip()
    session = get_db_session()
    try:
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="completed").first()
        if not transaction:
            await update.message.reply_text("유효한 거래 ID를 입력해주세요." + command_guide())
            return WAITING_FOR_RATING
        context.user_data['transaction_id'] = transaction_id
        await update.message.reply_text("평점 (1-5)을 입력해주세요." + command_guide())
        return WAITING_FOR_CONFIRMATION
    except Exception as e:
        logging.error(f"/rate 오류: {e}")
        return WAITING_FOR_RATING
    finally:
        session.close()

async def save_rating(update: Update, context) -> int:
    session = get_db_session()
    try:
        score = int(update.message.text.strip())
        if score < 1 or score > 5:
            await update.message.reply_text("평점은 1에서 5 사이의 숫자여야 합니다." + command_guide())
            return WAITING_FOR_CONFIRMATION
        transaction_id = context.user_data.get('transaction_id')
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id).first()
        if update.message.from_user.id == transaction.buyer_id:
            target_id = transaction.seller_id
        else:
            target_id = transaction.buyer_id
        new_rating = Rating(user_id=target_id, score=score, review="익명")
        session.add(new_rating)
        session.commit()
        await update.message.reply_text(f"평점 {score}점이 등록되었습니다!" + command_guide())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("유효한 숫자를 입력해주세요." + command_guide())
        return WAITING_FOR_CONFIRMATION
    except Exception as e:
        session.rollback()
        logging.error(f"/rate 처리 오류: {e}")
        return WAITING_FOR_CONFIRMATION
    finally:
        session.close()

# ==============================
# 10. /chat: 거래 당사자 간 익명 채팅
active_chats = {}  # {transaction_id: (buyer_id, seller_id)}
async def start_chat(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /chat 거래ID\n예: /chat 123456789012" + command_guide())
        return
    transaction_id = args[1].strip()
    session = get_db_session()
    try:
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="accepted").first()
        if not transaction:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        active_chats[transaction_id] = (transaction.buyer_id, transaction.seller_id)
        context.user_data['current_transaction'] = transaction_id
        await update.message.reply_text("익명 채팅을 시작합니다. 메시지를 입력하면 상대방에게 전달됩니다." + command_guide())
    except Exception as e:
        logging.error(f"/chat 오류: {e}")
    finally:
        session.close()

async def relay_message(update: Update, context) -> None:
    transaction_id = context.user_data.get('current_transaction')
    if not transaction_id or transaction_id not in active_chats:
        return
    buyer_id, seller_id = active_chats[transaction_id]
    sender = update.message.from_user.id
    partner = seller_id if sender == buyer_id else buyer_id if sender == seller_id else None
    if not partner:
        return
    try:
        await context.bot.send_message(chat_id=partner, text=f"[채팅] {update.message.text}")
    except Exception as e:
        logging.error(f"채팅 메시지 전송 오류: {e}")

# ==============================
# 11. /off: 거래 중단 (구매자/판매자 모두)
async def off_transaction(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /off 거래ID\n예: /off 123456789012" + command_guide())
        return
    transaction_id = args[1].strip()
    session = get_db_session()
    try:
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id).first()
        if not transaction:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        transaction.status = "cancelled"
        session.commit()
        if transaction_id in active_chats:
            active_chats.pop(transaction_id)
        await update.message.reply_text(f"거래 ID {transaction_id}가 중단되었습니다." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/off 오류: {e}")
    finally:
        session.close()

# ==============================
# 12. /refund: 구매자 환불 요청 (구매자, 송금 후 거래 취소 시)
async def refund_request(update: Update, context) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refund 거래ID\n예: /refund 123456789012" + command_guide())
        return ConversationHandler.END
    transaction_id = args[1].strip()
    session = get_db_session()
    try:
        transaction = session.query(Transaction).filter_by(transaction_id=transaction_id, status="accepted").first()
        if not transaction:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return ConversationHandler.END
        if update.message.from_user.id != transaction.buyer_id:
            await update.message.reply_text("구매자만 이 명령어를 사용할 수 있습니다." + command_guide())
            return ConversationHandler.END
        expected_amount = float(transaction.amount)
        if not check_usdt_payment(expected_amount, "", transaction_id):
            await update.message.reply_text("아직 송금이 확인되지 않았습니다." + command_guide())
            return ConversationHandler.END
        # 환불 시, 중개 수수료의 절반만 차감 (예: 5%의 절반 = 2.5%)
        refund_amount = expected_amount * (1 - (COMMISSION_RATE / 2))
        context.user_data['refund_transaction_id'] = transaction_id
        context.user_data['refund_amount'] = refund_amount
        await update.message.reply_text(
            f"구매자가 송금한 경우, 중개 수수료의 절반만 차감하고 {refund_amount} 테더를 환불해드리겠습니다.\n환불 받을 지갑 주소를 입력해주세요." + command_guide()
        )
        return WAITING_FOR_REFUND_WALLET
    except Exception as e:
        logging.error(f"/refund 오류: {e}")
        await update.message.reply_text("환불 요청 중 오류가 발생했습니다." + command_guide())
        return ConversationHandler.END
    finally:
        session.close()

async def process_refund(update: Update, context) -> int:
    wallet_address = update.message.text.strip()
    transaction_id = context.user_data.get('refund_transaction_id')
    refund_amount = context.user_data.get('refund_amount')
    try:
        result = send_usdt(wallet_address, refund_amount)
        await update.message.reply_text(
            f"환불 요청이 완료되었습니다. {refund_amount} 테더가 {wallet_address}로 송금되었습니다.\n거래 ID: {transaction_id}\n송금 결과: {result}" + command_guide()
        )
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"환불 송금 오류: {e}")
        await update.message.reply_text("환불 송금 중 오류가 발생했습니다. 다시 지갑 주소를 입력해주세요." + command_guide())
        return WAITING_FOR_REFUND_WALLET

# ==============================
# 13. /exit: 대화 종료 및 초기화 (거래/채팅/평가 중 제외)
async def exit_to_start(update: Update, context) -> int:
    if 'current_transaction' in context.user_data:
        await update.message.reply_text("거래 중이거나 채팅 중에는 /exit 명령어를 사용할 수 없습니다." + command_guide())
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("초기 화면으로 돌아갑니다. /start 명령어로 다시 시작해주세요." + command_guide())
    return ConversationHandler.END

# ==============================
# 14. 에러 핸들러
async def error_handler(update: object, context) -> None:
    logging.error(msg="오류 발생", exc_info=context.error)
    if update and hasattr(update, 'message') and update.message:
        await update.message.reply_text("오류가 발생했습니다. 다시 시도해주세요." + command_guide())

# ==============================
# 대화 흐름 핸들러 설정
sell_handler = ConversationHandler(
    entry_points=[CommandHandler("sell", sell_command)],
    states={
        WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
        WAITING_FOR_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
        WAITING_FOR_ITEM_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_type)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start)],
)

cancel_handler = ConversationHandler(
    entry_points=[CommandHandler("cancel", cancel)],
    states={
        WAITING_FOR_CANCEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_item)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start)],
)

rate_handler = ConversationHandler(
    entry_points=[CommandHandler("rate", rate_user)],
    states={
        WAITING_FOR_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rating)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start)],
)

refund_handler = ConversationHandler(
    entry_points=[CommandHandler("refund", refund_request)],
    states={
        WAITING_FOR_REFUND_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_refund)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start)],
)

# ==============================
# 앱 초기화 및 핸들러 등록
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    # 개별 명령어 핸들러 등록
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_items_command))
    app.add_handler(CommandHandler("next", next_page))
    app.add_handler(CommandHandler("prev", prev_page))
    app.add_handler(CommandHandler("search", search_items_command))
    app.add_handler(CommandHandler("offer", offer_item))
    app.add_handler(CommandHandler("accept", accept_transaction))
    app.add_handler(CommandHandler("refusal", refusal_transaction))
    app.add_handler(CommandHandler("confirm", confirm_payment))
    app.add_handler(CommandHandler("off", off_transaction))
    app.add_handler(CommandHandler("chat", start_chat))
    app.add_handler(CommandHandler("exit", exit_to_start))
    app.add_handler(refund_handler)

    # 대화 흐름 핸들러 등록
    app.add_handler(sell_handler)
    app.add_handler(cancel_handler)
    app.add_handler(rate_handler)

    # 채팅 메시지 리레이 핸들러 (거래 채팅 중)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, relay_message))

    app.add_error_handler(error_handler)
    app.run_polling()