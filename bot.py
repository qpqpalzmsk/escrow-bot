import logging
import random
import os
import time
import requests
from requests.adapters import HTTPAdapter, Retry

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, InputMediaPhoto
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
DATABASE_URL = os.getenv("DATABASE_URL")  # 실제 DSN으로 교체하세요.
TRON_API = os.getenv("TRON_API")            # 예: "https://api.trongrid.io"
TRON_API_KEY = os.getenv("TRON_API_KEY")      # 실제 API Key
TRON_WALLET = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"  # 봇의 Tron 지갑 주소 (TRC20 USDT 전용)
PRIVATE_KEY = os.getenv("PRIVATE_KEY")        # 봇 지갑의 개인키

# TRC20 USDT 컨트랙트 주소 (메인넷 – 본인이 확인한 주소)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# 관리자(운영자) 전용 텔레그램 ID (환경변수나 하드코딩)
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "999999999"))

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
    name = Column(Text, nullable=False)      # 한글 지원
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
    status = Column(String, default='pending')      # pending, accepted, completed, cancelled, rejected
    session_id = Column(Text)                       # 판매자 /accept 시 입력한 지갑 주소 또는 환불용 구매자 지갑
    transaction_id = Column(Text, unique=True)      # 12자리 랜덤 거래 id
    amount = Column(DECIMAL, nullable=False)
    created_at = Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))

class Rating(Base):
    __tablename__ = 'ratings'
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, nullable=False)    # 평가 대상 (봇만 보관)
    score = Column(Integer, nullable=False)
    review = Column(Text)
    created_at = Column(TIMESTAMP, server_default=text('CURRENT_TIMESTAMP'))

Base.metadata.create_all(bind=engine)

# ==============================
# Tron 클라이언트 설정 (HTTPProvider는 이제 api_key 인자를 사용)
client = Tron(provider=HTTPProvider(TRON_API, api_key=TRON_API_KEY))

# 중개 수수료 정의
NORMAL_COMMISSION_RATE = 0.05   # 기본 5%
OVERSEND_COMMISSION_RATE = 0.075  # 초과 송금 시 총 7.5%

# ==============================
# 송금(USDT 전송) 및 검증 로직
def verify_deposit(expected_amount: float, txid: str, internal_txid: str) -> (bool, float):
    """
    블록체인에서 거래(txid)를 조회하여 전송 금액과 메모(참조)에 internal_txid(거래ID)가 포함되어 있는지 검증.
    반환: (검증 성공 여부, 실제 입금된 USDT 금액)
    """
    try:
        tx = client.get_transaction(txid)
        contract_info = tx["raw_data"]["contract"][0]["parameter"]["value"]
        transferred_amount = int(contract_info.get("amount", 0))
        data_hex = contract_info.get("data", "")
        memo = bytes.fromhex(data_hex).decode("utf-8") if data_hex else ""
        actual_amount = transferred_amount / 1e6
        # 정확한 금액 검증
        if transferred_amount != int(expected_amount * 1e6):
            return (False, actual_amount)
        # 메모에 내부 거래ID가 반드시 포함되어야 함 (대소문자 구분 없이)
        if internal_txid.lower() not in memo.lower():
            return (False, actual_amount)
        return (True, actual_amount)
    except Exception as e:
        logging.error(f"블록체인 거래 검증 오류: {e}")
        return (False, 0)

def check_usdt_payment(expected_amount: float, txid: str = "", internal_txid: str = "") -> (bool, float):
    """
    txid와 internal_txid가 제공되면 verify_deposit()으로 검증하고,
    그렇지 않으면 현재 봇 지갑의 USDT 잔액을 확인합니다.
    """
    if txid and internal_txid:
        return verify_deposit(expected_amount, txid, internal_txid)
    try:
        contract = client.get_contract(USDT_CONTRACT)
        balance = contract.functions.balanceOf(TRON_WALLET)
        return (balance / 1e6) >= expected_amount, balance / 1e6
    except Exception as e:
        logging.error(f"USDT 잔액 확인 오류: {e}")
        return (False, 0)

def send_usdt(to_address: str, amount: float, memo: str = "") -> dict:
    """
    to_address로 amount(USDT)를 송금합니다.
    memo(거래ID 등)를 포함시키려면 해당 정보를 hex로 인코딩하여 data 필드에 포함할 수 있습니다.
    (현재 tronpy에서는 data 인자 지원이 제한적이므로, 별도 로직 필요할 수 있음)
    """
    try:
        contract = client.get_contract(USDT_CONTRACT)
        # 데이터(메모)는 hex 인코딩해서 전송 (메모가 비어있으면 생략)
        data = memo.encode("utf-8").hex() if memo else ""
        txn = (
            contract.functions.transfer(to_address, int(amount * 1e6))
            .with_owner(TRON_WALLET)
            .fee_limit(1_000_000_000)
            .with_data(data)  # 메모를 포함하여 전송 (지원 여부 확인 필요)
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

# ==============================
# 대화 상태 상수
(
    WAITING_FOR_ITEM_NAME,
    WAITING_FOR_PRICE,
    WAITING_FOR_ITEM_TYPE,
    WAITING_FOR_CANCEL_ID,
    WAITING_FOR_RATING,
    WAITING_FOR_CONFIRMATION,
    WAITING_FOR_REFUND_WALLET,
) = range(7)

ITEMS_PER_PAGE = 10
active_chats = {}  # {transaction_id: (buyer_id, seller_id)}

# ==============================
# 명령어 안내
def command_guide() -> str:
    return (
        "\n\n사용 가능한 명령어:\n"
        "/sell - 상품 판매 등록\n"
        "/list - 구매 가능한 상품 목록 조회\n"
        "/cancel - 본인이 등록한 상품 취소 (입금 전만 가능)\n"
        "/search - 상품 검색\n"
        "/offer - 거래 요청 (목록/검색 후 사용)\n"
        "/accept - 거래 요청 수락 (판매자 전용, 사용법: /accept 거래ID 판매자지갑주소 [TRC20 USDT])\n"
        "/refusal - 거래 요청 거절 (판매자 전용, 사용법: /refusal 거래ID)\n"
        "/confirm - 거래 완료 확인 (구매자 전용, 사용법: /confirm 거래ID 구매자지갑주소 txID)\n"
        "/refund - 거래 취소 시 환불 요청 (구매자 전용, 사용법: /refund 거래ID, 이후 지갑주소 입력)\n"
        "/rate - 거래 종료 후 평점 남기기 (사용법: /rate 거래ID)\n"
        "/chat - 거래 당사자 간 익명 채팅 (텍스트/파일 전송 가능)\n"
        "/off - 거래 중단 (구매자/판매자 모두, 사용법: /off 거래ID)\n"
        "/warexit - 강제 종료 (관리자 전용, 사용법: /warexit 거래ID)\n"
        "/exit - 대화 종료 및 초기화"
    )

# ==============================
# 1. /start
async def start(update: Update, _) -> int:
    await update.message.reply_text(
        "에스크로 거래 봇에 오신 것을 환영합니다!\n문제 발생 시 관리자에게 문의하세요.\n(관리자 ID는 봇 프로필에서 확인)"
        + command_guide()
    )
    return ConversationHandler.END

# ==============================
# 2. /sell
async def sell_command(update: Update, _) -> int:
    await update.message.reply_text("판매할 상품의 이름을 입력해주세요." + command_guide())
    return WAITING_FOR_ITEM_NAME

async def set_item_name(update: Update, context) -> int:
    context.user_data["item_name"] = update.message.text.strip()
    await update.message.reply_text("상품의 가격(USDT)을 숫자로 입력해주세요." + command_guide())
    return WAITING_FOR_PRICE

async def set_item_price(update: Update, context) -> int:
    try:
        price = float(update.message.text.strip())
        context.user_data["price"] = price
        await update.message.reply_text("상품 종류를 입력해주세요. (디지털/현물)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    except ValueError:
        await update.message.reply_text("유효한 가격을 숫자로 입력해주세요." + command_guide())
        return WAITING_FOR_PRICE

async def set_item_type(update: Update, context) -> int:
    item_type = update.message.text.strip().lower()
    if item_type not in ["디지털", "현물"]:
        await update.message.reply_text("유효한 종류를 입력해주세요. (디지털/현물)" + command_guide())
        return WAITING_FOR_ITEM_TYPE

    item_name = context.user_data["item_name"]
    price = context.user_data["price"]
    seller_id = update.message.from_user.id
    session = get_db_session()
    try:
        new_item = Item(name=item_name, price=price, seller_id=seller_id, type=item_type)
        session.add(new_item)
        session.commit()
        await update.message.reply_text(f"'{item_name}' 상품이 등록되었습니다." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"상품 등록 오류: {e}")
        await update.message.reply_text("상품 등록 중 오류가 발생했습니다. 다시 시도해주세요." + command_guide())
    finally:
        session.close()
    return ConversationHandler.END

# ==============================
# 3. /list
async def list_items_command(update: Update, context) -> None:
    session = get_db_session()
    try:
        page = context.user_data.get("list_page", 1)
        items = session.query(Item).filter(Item.status == "available").all()
        if not items:
            await update.message.reply_text("등록된 상품이 없습니다." + command_guide())
            return

        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        page = total_pages if page < 1 else (1 if page > total_pages else page)
        context.user_data["list_page"] = page

        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]

        context.user_data["list_mapping"] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}
        msg = f"구매 가능한 상품 목록 (페이지 {page}/{total_pages}):\n"
        for idx, item in enumerate(page_items, start=1):
            msg += f"{idx}. {item.name} - {item.price} USDT ({item.type})\n"
        msg += "\n/next, /prev 로 페이지 이동\n/offer [번호 또는 이름] 으로 거래 요청"
        await update.message.reply_text(msg + command_guide())
    except Exception as e:
        logging.error(f"/list 오류: {e}")
        await update.message.reply_text("상품 목록 조회 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

async def next_page(update: Update, context) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) + 1
    await list_items_command(update, context)

async def prev_page(update: Update, context) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) - 1
    await list_items_command(update, context)

# ==============================
# 4. /search
async def search_items_command(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("검색어를 입력해주세요. 예: /search 마우스" + command_guide())
        return
    query = args[1].strip().lower()
    context.user_data["search_query"] = query
    context.user_data["search_page"] = 1
    await list_search_results(update, context)

async def list_search_results(update: Update, context) -> None:
    session = get_db_session()
    try:
        query = context.user_data.get("search_query", "")
        page = context.user_data.get("search_page", 1)
        items = session.query(Item).filter(Item.name.ilike(f"%{query}%"), Item.status == "available").all()
        if not items:
            await update.message.reply_text(f"'{query}' 검색 결과가 없습니다." + command_guide())
            return

        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        page = total_pages if page < 1 else (1 if page > total_pages else page)
        context.user_data["search_page"] = page

        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]

        context.user_data["search_mapping"] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}
        msg = f"'{query}' 검색 결과 (페이지 {page}/{total_pages}):\n"
        for idx, item in enumerate(page_items, start=1):
            msg += f"{idx}. {item.name} - {item.price} USDT ({item.type})\n"
        msg += "\n/next, /prev 로 페이지 이동\n/offer [번호 또는 이름] 으로 거래 요청"
        await update.message.reply_text(msg + command_guide())
    except Exception as e:
        logging.error(f"/search 오류: {e}")
        await update.message.reply_text("상품 검색 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 5. /offer
def generate_transaction_id() -> str:
    return ''.join(str(random.randint(0, 9)) for _ in range(12))

async def offer_item(update: Update, context) -> None:
    session = get_db_session()
    try:
        args = update.message.text.split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text("사용법: /offer [번호 또는 상품이름]" + command_guide())
            return
        identifier = args[1].strip()
        mapping = context.user_data.get("list_mapping") or context.user_data.get("search_mapping") or {}
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
            await update.message.reply_text("유효한 상품 번호/이름을 입력해주세요." + command_guide())
            return

        buyer_id = update.message.from_user.id
        seller_id = item.seller_id

        t_id = generate_transaction_id()
        new_tx = Transaction(item_id=item.id, buyer_id=buyer_id, seller_id=seller_id, amount=item.price, transaction_id=t_id)
        session.add(new_tx)
        session.commit()

        await update.message.reply_text(
            f"'{item.name}' 거래 요청이 생성되었습니다.\n거래 ID: {t_id}\n반드시 송금 시 메모(참조)에 이 거래 ID를 입력해주세요!"
            + command_guide()
        )
        try:
            await context.bot.send_message(
                chat_id=seller_id,
                text=(
                    f"당신의 상품 '{item.name}'에 거래 요청이 도착했습니다.\n거래 ID: {t_id}\n"
                    "판매자께서는 /accept 거래ID 판매자지갑주소 로 수락하거나, /refusal 거래ID 로 거절하세요.\n"
                    "※ 네트워크: TRC20 USDT"
                )
            )
        except Exception as e:
            logging.error(f"판매자 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/offer 오류: {e}")
        await update.message.reply_text("거래 요청 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 6. /cancel (입금 전 상품만 취소)
async def cancel(update: Update, context) -> int:
    session = get_db_session()
    try:
        seller_id = update.message.from_user.id
        items = session.query(Item).filter(Item.seller_id == seller_id, Item.status == "available").all()
        if not items:
            await update.message.reply_text("취소할 수 있는 상품이 없습니다. (입금 전 상품만 가능)" + command_guide())
            return ConversationHandler.END

        page = context.user_data.get("cancel_page", 1)
        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        page = total_pages if page < 1 else (1 if page > total_pages else page)
        context.user_data["cancel_page"] = page

        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        context.user_data["cancel_mapping"] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}

        msg = f"취소 가능한 상품 목록 (페이지 {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"
        msg += "\n/next, /prev 로 페이지 이동\n취소할 상품 번호/이름을 입력해주세요."
        await update.message.reply_text(msg + command_guide())
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
        mapping = context.user_data.get("cancel_mapping") or {}
        if identifier in mapping:
            item_id = mapping[identifier]
            item = session.query(Item).filter_by(id=item_id, seller_id=seller_id, status="available").first()
        else:
            try:
                item = session.query(Item).filter_by(id=int(identifier), seller_id=seller_id, status="available").first()
            except ValueError:
                item = session.query(Item).filter(Item.name.ilike(f"%{identifier}%"), Item.seller_id == seller_id, Item.status == "available").first()
        if not item:
            await update.message.reply_text("유효한 상품 번호/이름이 없거나 취소 불가능한 상태입니다." + command_guide())
            return WAITING_FOR_CANCEL_ID

        session.delete(item)
        session.commit()
        await update.message.reply_text(f"'{item.name}' 상품이 취소되었습니다." + command_guide())
        return ConversationHandler.END
    except Exception as e:
        session.rollback()
        logging.error(f"/cancel 처리 오류: {e}")
        await update.message.reply_text("상품 취소 처리 중 오류가 발생했습니다." + command_guide())
        return WAITING_FOR_CANCEL_ID
    finally:
        session.close()

# ==============================
# 7. /accept (판매자 전용)
async def accept_transaction(update: Update, context) -> None:
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("사용법: /accept 거래ID 판매자지갑주소\n예: /accept 123456789012 TXXXXXX..." + command_guide())
        return
    t_id = args[1].strip()
    seller_wallet = args[2].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="pending").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        if update.message.from_user.id != tx.seller_id:
            await update.message.reply_text("판매자만 이 명령어를 사용할 수 있습니다." + command_guide())
            return

        tx.session_id = seller_wallet  # 판매자가 제공한 지갑 주소 저장
        tx.status = "accepted"
        session.commit()

        await update.message.reply_text(f"거래 ID {t_id}가 수락되었습니다. (네트워크: TRC20 USDT)\n구매자에게 송금 안내를 전송합니다." + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.buyer_id,
                text=(
                    f"거래 ID {t_id}가 수락되었습니다.\n"
                    f"해당 금액({tx.amount} USDT)를 {TRON_WALLET}로 송금해주세요.\n"
                    "반드시 메모(참조)에 거래 ID를 포함해 주세요!"
                )
            )
        except Exception as e:
            logging.error(f"구매자 알림 전송 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/accept 오류: {e}")
        await update.message.reply_text("거래 수락 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 7-2. /refusal (판매자 전용)
async def refusal_transaction(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refusal 거래ID" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="pending").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        if update.message.from_user.id != tx.seller_id:
            await update.message.reply_text("판매자만 사용 가능합니다." + command_guide())
            return

        session.delete(tx)
        session.commit()
        await update.message.reply_text(f"거래 ID {t_id}가 거절되었습니다." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/refusal 오류: {e}")
        await update.message.reply_text("거래 거절 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 8. /confirm (구매자 전용) – 송금 확인 및 오버/언더 센드 처리
# 사용법: /confirm 거래ID 구매자지갑주소 txID
async def confirm_payment(update: Update, context) -> None:
    args = update.message.text.split()
    if len(args) < 4:
        await update.message.reply_text("사용법: /confirm 거래ID 구매자지갑주소 txID\n예: /confirm 123456789012 TYYYYYYYYYYYY abcdef1234567890" + command_guide())
        return
    t_id = args[1].strip()
    buyer_wallet = args[2].strip()
    txid = args[3].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="accepted").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아니거나 아직 수락되지 않은 거래입니다." + command_guide())
            return
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 사용할 수 있습니다." + command_guide())
            return

        original_amount = float(tx.amount)
        valid, deposited_amount = check_usdt_payment(original_amount, txid, t_id)
        if not valid:
            if deposited_amount == 0:
                await update.message.reply_text("블록체인에서 거래를 확인할 수 없거나 메모(거래ID)가 누락되었습니다.\n송금 기록을 다시 확인해주세요." + command_guide())
                return
            if deposited_amount < original_amount:
                refund_result = send_usdt(buyer_wallet, deposited_amount)
                await update.message.reply_text(
                    f"입금액({deposited_amount} USDT)이 부족합니다(필요: {original_amount} USDT).\n전액 환불 처리되었습니다.\n환불 결과: {refund_result}\n정확한 금액을 다시 송금해주세요." + command_guide()
                )
                return
            else:
                # 초과 송금: 오버센드로 7.5% 수수료 적용
                net_amount = deposited_amount * (1 - OVERSEND_COMMISSION_RATE)
                tx.status = "completed"
                session.commit()
                await update.message.reply_text(
                    f"입금액({deposited_amount} USDT)이 원래보다 많습니다.\n초과 송금 시 7.5% 수수료 적용 후 판매자에게 {net_amount} USDT 송금 진행합니다." + command_guide()
                )
                try:
                    seller_wallet = tx.session_id
                    result = send_usdt(seller_wallet, net_amount, memo=t_id)
                    await context.bot.send_message(
                        chat_id=tx.seller_id,
                        text=f"거래 ID {t_id}가 완료되었습니다.\n초과 송금(7.5% 수수료 적용) 후 {net_amount} USDT가 판매자 지갑({seller_wallet})으로 송금되었습니다.\n송금 결과: {result}\n구매자에게는 해당 초과 금액 환불 안내 후 재송금 요청 바랍니다."
                    )
                except Exception as e:
                    logging.error(f"판매자 초과 송금 오류: {e}")
                return

        # 정상 입금: 정확한 금액
        tx.status = "completed"
        session.commit()
        net_amount = original_amount * (1 - NORMAL_COMMISSION_RATE)
        await update.message.reply_text(
            f"입금이 정확히 확인되었습니다 ({original_amount} USDT). 거래를 완료합니다.\n판매자에게 {net_amount} USDT 송금 진행 중..." + command_guide()
        )
        try:
            seller_wallet = tx.session_id
            result = send_usdt(seller_wallet, net_amount, memo=t_id)
            await context.bot.send_message(
                chat_id=tx.seller_id,
                text=f"거래 ID {t_id}가 완료되었습니다.\n{net_amount} USDT가 판매자 지갑({seller_wallet})으로 송금되었습니다.\n송금 결과: {result}\n구매자에게 물건을 발송해주세요!"
            )
        except Exception as e:
            logging.error(f"판매자 정상 송금 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/confirm 오류: {e}")
        await update.message.reply_text("거래 완료 처리 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 9. /rate
async def rate_user(update: Update, context) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /rate 거래ID" + command_guide())
        return WAITING_FOR_RATING
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("완료된 거래가 아니거나 유효하지 않은 거래ID입니다." + command_guide())
            return WAITING_FOR_RATING
        context.user_data["rating_txid"] = t_id
        await update.message.reply_text("평점 (1~5)을 입력해주세요." + command_guide())
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
        if not (1 <= score <= 5):
            await update.message.reply_text("평점은 1~5 사이여야 합니다." + command_guide())
            return WAITING_FOR_CONFIRMATION

        t_id = context.user_data.get("rating_txid")
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("유효한 거래가 아닙니다." + command_guide())
            return ConversationHandler.END

        # 평가 대상: 구매자 → 판매자, 판매자 → 구매자
        target_id = tx.seller_id if update.message.from_user.id == tx.buyer_id else tx.buyer_id
        new_rating = Rating(user_id=target_id, score=score, review="익명")
        session.add(new_rating)
        session.commit()
        await update.message.reply_text(f"평점 {score}점이 등록되었습니다!" + command_guide())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("숫자로 입력해주세요." + command_guide())
        return WAITING_FOR_CONFIRMATION
    except Exception as e:
        session.rollback()
        logging.error(f"/rate 처리 오류: {e}")
        return WAITING_FOR_CONFIRMATION
    finally:
        session.close()

# ==============================
# 10. /chat: 거래 당사자 간 익명 채팅 (텍스트 및 파일 전송 지원)
async def start_chat(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /chat 거래ID" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status__in=["accepted", "completed"]).first()
        if not tx:
            await update.message.reply_text("유효한 거래가 아니거나 아직 수락되지 않은 거래입니다." + command_guide())
            return
        # 당사자 확인
        user_id = update.message.from_user.id
        if user_id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("이 거래의 당사자가 아니므로 채팅을 시작할 수 없습니다." + command_guide())
            return
        active_chats[t_id] = (tx.buyer_id, tx.seller_id)
        context.user_data["current_chat_tx"] = t_id
        await update.message.reply_text(f"거래 ID {t_id}에 대한 익명 채팅을 시작합니다.\n텍스트나 파일(사진/문서 등)을 전송하면 상대방에게 전달됩니다." + command_guide())
    except Exception as e:
        logging.error(f"/chat 오류: {e}")
        await update.message.reply_text("채팅 시작 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

async def relay_message(update: Update, context) -> None:
    t_id = context.user_data.get("current_chat_tx")
    if not t_id or t_id not in active_chats:
        return
    buyer_id, seller_id = active_chats[t_id]
    sender = update.message.from_user.id
    partner = seller_id if sender == buyer_id else buyer_id if sender == seller_id else None
    if not partner:
        return
    try:
        # 파일 전송 (문서, 사진) 또는 텍스트 전송 모두 처리
        if update.message.document:
            file_id = update.message.document.file_id
            file_name = update.message.document.file_name
            await context.bot.send_document(chat_id=partner, document=file_id, caption=f"[파일] {file_name}")
        elif update.message.photo:
            photo = update.message.photo[-1]
            await context.bot.send_photo(chat_id=partner, photo=photo.file_id, caption="[사진]")
        else:
            await context.bot.send_message(chat_id=partner, text=f"[채팅] {update.message.text}")
    except Exception as e:
        logging.error(f"채팅 메시지 전송 오류: {e}")

# ==============================
# 11. /off: 거래 중단 (구매자/판매자 모두)
async def off_transaction(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /off 거래ID" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        # 거래가 이미 입금되어 완료/환불된 경우 취소 불가
        if tx.status not in ["pending", "accepted"]:
            await update.message.reply_text("이미 진행 중이거나 완료된 거래는 취소할 수 없습니다." + command_guide())
            return
        # 당사자 확인
        if update.message.from_user.id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("이 거래의 당사자가 아닙니다." + command_guide())
            return

        tx.status = "cancelled"
        session.commit()
        if t_id in active_chats:
            active_chats.pop(t_id)
        await update.message.reply_text(f"거래 ID {t_id}가 중단되었습니다." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/off 오류: {e}")
        await update.message.reply_text("거래 중단 처리 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 12. /refund: 구매자 환불 요청 (구매자 전용)
# 사용법: /refund 거래ID -> (후속: 구매자 지갑주소 입력)
async def refund_request(update: Update, context) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refund 거래ID\n예: /refund 123456789012" + command_guide())
        return ConversationHandler.END
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="accepted").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아니거나 환불 요청이 불가능합니다." + command_guide())
            return ConversationHandler.END
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 환불 요청이 가능합니다." + command_guide())
            return ConversationHandler.END

        expected_amount = float(tx.amount)
        valid, deposited_amount = check_usdt_payment(expected_amount, "", t_id)
        if not valid:
            await update.message.reply_text("입금 확인이 안 되었거나 거래 데이터에 이상이 있습니다." + command_guide())
            return ConversationHandler.END

        # 환불 시, 중개 수수료 2.5%만 차감 (즉, 총 7.5% 수수료 적용시 구매자에게 돌려줄 금액)
        refund_amount = expected_amount * (1 - (NORMAL_COMMISSION_RATE / 2))
        context.user_data["refund_txid"] = t_id
        context.user_data["refund_amount"] = refund_amount
        await update.message.reply_text(
            f"환불을 진행합니다. 구매자 지갑 주소를 입력해주세요.\n(환불 금액: {refund_amount} USDT, 수수료: 2.5% 적용)"
            + command_guide()
        )
        return WAITING_FOR_REFUND_WALLET
    except Exception as e:
        logging.error(f"/refund 오류: {e}")
        await update.message.reply_text("환불 요청 중 오류가 발생했습니다." + command_guide())
        return ConversationHandler.END
    finally:
        session.close()

async def process_refund(update: Update, context) -> int:
    buyer_wallet = update.message.text.strip()
    t_id = context.user_data.get("refund_txid")
    refund_amount = context.user_data.get("refund_amount")
    try:
        result = send_usdt(buyer_wallet, refund_amount, memo=t_id)
        await update.message.reply_text(
            f"환불 요청이 완료되었습니다. {refund_amount} USDT가 {buyer_wallet}로 송금되었습니다.\n거래 ID: {t_id}\n송금 결과: {result}"
            + command_guide()
        )
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"환불 송금 오류: {e}")
        await update.message.reply_text("환불 송금 중 오류가 발생했습니다. 다시 지갑 주소를 입력해주세요." + command_guide())
        return WAITING_FOR_REFUND_WALLET

# ==============================
# 13. /warexit: 관리자 전용 강제 종료
async def warexit_command(update: Update, context) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용할 수 있는 명령어입니다." + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /warexit 거래ID" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아닙니다." + command_guide())
            return
        tx.status = "cancelled"
        session.commit()
        if t_id in active_chats:
            active_chats.pop(t_id)
        await update.message.reply_text(f"[관리자] 거래 ID {t_id}가 강제 종료되었습니다." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/warexit 오류: {e}")
        await update.message.reply_text("강제 종료 처리 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# ==============================
# 14. /exit
async def exit_to_start(update: Update, context) -> int:
    if "current_chat_tx" in context.user_data:
        await update.message.reply_text("거래 채팅 중에는 /exit 명령어를 사용할 수 없습니다." + command_guide())
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("초기 화면으로 돌아갑니다. /start 명령어로 다시 시작해주세요." + command_guide())
    return ConversationHandler.END

# ==============================
# 에러 핸들러
async def error_handler(update: object, context) -> None:
    logging.error("오류 발생", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
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
    app.add_handler(CommandHandler("warexit", warexit_command))
    app.add_handler(CommandHandler("chat", start_chat))
    app.add_handler(CommandHandler("exit", exit_to_start))
    app.add_handler(refund_handler)

    # 대화형 핸들러 등록
    app.add_handler(sell_handler)
    app.add_handler(cancel_handler)
    app.add_handler(rate_handler)

    # 파일 및 텍스트 메시지 중계 (채팅)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay_message))

    app.add_error_handler(error_handler)
    app.run_polling()