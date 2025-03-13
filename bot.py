import logging
import random
import os
import time
import requests
import asyncio
from functools import wraps

from requests.adapters import HTTPAdapter, Retry

# telegram-bot (v20.x)
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    CallbackContext,
)
from telegram.constants import ParseMode

# SQLAlchemy
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    DECIMAL,
    BigInteger,
    Text,
    TIMESTAMP,
    text
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session

# tronpy
from tronpy import Tron
from tronpy.providers import HTTPProvider

# ==============================
# 1. 환경 변수 (Fly.io secrets 등)
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")   # 봇 토큰
DATABASE_URL = os.getenv("DATABASE_URL")           # "postgres://user:pass@host:5432/db"
TRON_API = os.getenv("TRON_API", "https://api.trongrid.io")
TRON_API_KEY = os.getenv("TRON_API_KEY", "")
TRON_WALLET = os.getenv("TRON_WALLET", "TT8AZ3...")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
TRON_PASSWORD = os.getenv("TRON_PASSWORD", "")  # TronLink 지갑 비번 등 (예시는 단순히 로깅만)
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "999999999"))

USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# ==============================
# 2. requests 세션 (재시도/타임아웃)
http_session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
http_adapter = HTTPAdapter(max_retries=retries)
http_session.mount("https://", http_adapter)
http_session.mount("http://", http_adapter)

# ==============================
# 3. SQLAlchemy 설정
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
# 4. DB 모델
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
    transaction_id = Column(Text, unique=True)
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

# ==============================
# 5. Tron 설정
TRON_API_CLEAN = TRON_API.rstrip("/")
client = Tron(provider=HTTPProvider(TRON_API_CLEAN, api_key=TRON_API_KEY))

NORMAL_COMMISSION_RATE = 0.05
OVERSEND_COMMISSION_RATE = 0.075

# ==============================
# 6. Webhook 해제 함수
import requests
def remove_webhook(token: str):
    """deleteWebhook?drop_pending_updates=true를 호출해 이전 Webhook을 강제로 해제"""
    try:
        url = f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true"
        resp = requests.get(url, timeout=10)
        logging.info(f"deleteWebhook response: {resp.status_code}, {resp.text}")
    except Exception as e:
        logging.error(f"deleteWebhook error: {e}")

# ==============================
# 7. TronGrid 유틸
def fetch_transaction_detail(txid: str) -> dict:
    try:
        url = f"{TRON_API_CLEAN}/v1/transactions/{txid}"
        headers = {"Accept": "application/json"}
        if TRON_API_KEY:
            headers["TRON-PRO-API-KEY"] = TRON_API_KEY
        resp = http_session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        arr = resp.json().get("data", [])
        return arr[0] if arr else {}
    except Exception as e:
        logging.error(f"fetch_transaction_detail 오류: {e}")
        return {}

def parse_trc20_transfer_amount_and_memo(tx_detail: dict) -> (float, str):
    try:
        contracts = tx_detail.get("raw_data", {}).get("contract", [])
        if not contracts:
            return 0.0, ""
        first_contract = contracts[0].get("parameter", {}).get("value", {})
        transferred_amount = first_contract.get("amount", 0)
        data_hex = first_contract.get("data", "")
        memo = bytes.fromhex(data_hex).decode("utf-8") if data_hex else ""
        actual_amount = transferred_amount / 1e6
        return actual_amount, memo
    except Exception as e:
        logging.error(f"parse_trc20_transfer_amount_and_memo 오류: {e}")
        return 0.0, ""

def verify_deposit(expected_amount: float, txid: str, internal_txid: str) -> (bool, float):
    try:
        detail = fetch_transaction_detail(txid)
        actual_amount, memo = parse_trc20_transfer_amount_and_memo(detail)
        if abs(actual_amount - expected_amount) > 1e-6:
            return (False, actual_amount)
        if internal_txid.lower() not in memo.lower():
            return (False, actual_amount)
        return (True, actual_amount)
    except Exception as e:
        logging.error(f"verify_deposit 오류: {e}")
        return (False, 0.0)

def check_usdt_payment(expected_amount: float, txid: str = "", internal_txid: str = "") -> (bool, float):
    if txid and internal_txid:
        return verify_deposit(expected_amount, txid, internal_txid)
    try:
        contract = client.get_contract(USDT_CONTRACT)
        balance = contract.functions.balanceOf(TRON_WALLET)
        actual = balance / 1e6
        return (actual >= expected_amount, actual)
    except Exception as e:
        logging.error(f"check_usdt_payment 오류: {e}")
        return (False, 0.0)

def send_usdt(to_address: str, amount: float, memo: str = "") -> dict:
    if not TRON_PASSWORD:
        logging.warning("TRON_PASSWORD가 설정되지 않았습니다. (예시 코드상 큰 문제는 없지만, 실제 서명 로직 유의)")
    try:
        contract = client.get_contract(USDT_CONTRACT)
        data = memo.encode("utf-8").hex() if memo else ""
        txn = (
            contract.functions.transfer(to_address, int(amount * 1e6))
            .with_owner(TRON_WALLET)
            .fee_limit(1_000_000_000)
            .with_data(data)
            .build()
            .sign(PRIVATE_KEY)
            .broadcast()
        )
        result = txn.wait()
        return result
    except Exception as e:
        logging.error(f"TRC20 송금 오류: {e}")
        raise

# ==============================
# 8. auto_verify_deposits (JobQueue)
async def auto_verify_deposits(context: CallbackContext):
    """예시: status='accepted' 상태를 주기적으로 확인. 이 예시에서는 실제 입금 체크 로직을 구현해도 되고, 필요하다면 skip해도 됨."""
    session = get_db_session()
    try:
        pending_txs = session.query(Transaction).filter_by(status="accepted").all()
        for tx in pending_txs:
            logging.info(f"[auto_verify] Checking deposit for transaction_id={tx.transaction_id}")
            # 실제로는 TronGrid를 뒤져서 메모=tx.transaction_id가 들어있는 전송을 찾고
            # 금액이 일치하면 tx.status='deposit_confirmed' 로 업데이트 후 메시지 전송
            # 여기서는 데모로 pass
    except Exception as e:
        logging.error(f"auto_verify_deposits 오류: {e}")
    finally:
        session.close()

# ==============================
# 9. 로깅
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# 대화 상태
(WAITING_FOR_ITEM_NAME,
 WAITING_FOR_PRICE,
 WAITING_FOR_ITEM_TYPE,
 WAITING_FOR_CANCEL_ID,
 WAITING_FOR_RATING,
 WAITING_FOR_CONFIRMATION,
 WAITING_FOR_REFUND_WALLET) = range(7)

ITEMS_PER_PAGE = 10
active_chats = {}  # 예: {거래ID: (buyer_id, seller_id)}

# ==============================
# 차단/등록 사용자
BANNED_USERS = set()
REGISTERED_USERS = set()

def check_banned(func):
    @wraps(func)
    async def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user and update.effective_user.id in BANNED_USERS:
            await update.message.reply_text("차단된 사용자입니다.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

@check_banned
async def register_user(update: Update, context: CallbackContext) -> None:
    if update.effective_user:
        REGISTERED_USERS.add(update.effective_user.id)

# ==============================
# 명령어 도움말
def command_guide() -> str:
    return (
        "\n\n사용 가능한 명령어:\n"
        "/sell - 상품 판매 등록\n"
        "/list - 구매 가능한 상품 목록 조회\n"
        "/cancel - 본인이 등록한 상품 취소 (입금 전만 가능)\n"
        "/search - 상품 검색\n"
        "/offer - 거래 요청 (목록/검색 후 사용)\n"
        "/accept - 거래 요청 수락 (판매자 전용)\n"
        "/refusal - 거래 요청 거절 (판매자 전용)\n"
        "/checkdeposit - 입금 확인 (구매자 전용)\n"
        "/confirm - 거래 완료 확인 (구매자 전용)\n"
        "/refund - 거래 취소 시 환불 요청 (구매자 전용)\n"
        "/rate - 거래 종료 후 평점 남기기\n"
        "/chat - 거래 당사자 간 채팅\n"
        "/off - 거래 중단\n"
        "/warexit - 거래 강제 종료 (관리자 전용)\n"
        "/adminsearch - 거래 검색 (관리자 전용)\n"
        "/ban - 사용자 차단 (관리자 전용)\n"
        "/unban - 사용자 차단 해제 (관리자 전용)\n"
        "/post - 전체공지 (관리자 전용)\n"
        "/exit - 대화 종료 및 초기화"
    )

# ==============================
# /start
@check_banned
async def start_command(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text("에스크로 거래 봇에 오신 것을 환영합니다!" + command_guide())

@check_banned
async def exit_to_start(update: Update, context: CallbackContext) -> int:
    context.user_data.clear()
    await update.message.reply_text("대화를 종료하고 초기 상태로 돌아갑니다.\n" + command_guide())
    return ConversationHandler.END

# ==============================
# 10. /sell (ConversationHandler)
@check_banned
async def sell_command(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("판매할 상품 이름을 입력하세요.\n(취소: /exit)" + command_guide())
    return WAITING_FOR_ITEM_NAME

@check_banned
async def set_item_name(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    context.user_data["item_name"] = update.message.text.strip()
    await update.message.reply_text("상품 가격(USDT)을 숫자로 입력해주세요.\n(취소: /exit)" + command_guide())
    return WAITING_FOR_PRICE

@check_banned
async def set_item_price(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    try:
        price = float(update.message.text.strip())
        context.user_data["price"] = price
        await update.message.reply_text("상품 종류를 입력해주세요. (디지털/현물)\n(취소: /exit)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    except ValueError:
        await update.message.reply_text("유효한 숫자를 입력해주세요.\n(취소: /exit)" + command_guide())
        return WAITING_FOR_PRICE

@check_banned
async def set_item_type(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    item_type = update.message.text.strip().lower()
    if item_type not in ["디지털", "현물"]:
        await update.message.reply_text("유효한 종류를 입력해주세요. (디지털/현물)\n(취소: /exit)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    item_name = context.user_data["item_name"]
    price = context.user_data["price"]
    seller_id = update.message.from_user.id

    session = get_db_session()
    try:
        new_item = Item(name=item_name, price=price, seller_id=seller_id, type=item_type)
        session.add(new_item)
        session.commit()
        await update.message.reply_text(f"'{item_name}' 상품이 등록되었습니다.\n" + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"상품 등록 오류: {e}")
        await update.message.reply_text("상품 등록 중 오류가 발생했습니다.\n" + command_guide())
    finally:
        session.close()
    return ConversationHandler.END

sell_handler = ConversationHandler(
    entry_points=[CommandHandler("sell", sell_command)],
    states={
        WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
        WAITING_FOR_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
        WAITING_FOR_ITEM_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_type)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start), MessageHandler(filters.COMMAND, exit_to_start)],
)

# ==============================
# /list, /next, /prev
@check_banned
async def list_items_command(update: Update, context: CallbackContext) -> None:
    session = get_db_session()
    try:
        page = context.user_data.get("list_page", 1)
        items = session.query(Item).filter(Item.status == "available").all()
        if not items:
            await update.message.reply_text("등록된 상품이 없습니다.\n" + command_guide())
            return

        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        # 페이지 범위 보정
        if page < 1:
            page = total_pages
        elif page > total_pages:
            page = 1
        context.user_data["list_page"] = page

        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]

        # 숫자→item.id 매핑
        context.user_data["list_mapping"] = {str(idx): it.id for idx, it in enumerate(page_items, start=1)}

        msg = f"구매 가능한 상품 (페이지 {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"
        msg += "\n/next, /prev 로 페이지 이동\n/offer [번호 또는 이름] 으로 거래 요청"
        await update.message.reply_text(msg + command_guide())
    except Exception as e:
        logging.error(f"/list 오류: {e}")
        await update.message.reply_text("상품 목록 조회 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

@check_banned
async def next_page(update: Update, context: CallbackContext) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) + 1
    await list_items_command(update, context)

@check_banned
async def prev_page(update: Update, context: CallbackContext) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) - 1
    await list_items_command(update, context)

# ==============================
# /search
@check_banned
async def search_items_command(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /search 검색어\n예: /search 마우스" + command_guide())
        return
    query = args[1].strip().lower()
    context.user_data["search_query"] = query
    context.user_data["search_page"] = 1
    await list_search_results(update, context)

@check_banned
async def list_search_results(update: Update, context: CallbackContext) -> None:
    session = get_db_session()
    try:
        query = context.user_data.get("search_query", "")
        page = context.user_data.get("search_page", 1)
        items = session.query(Item).filter(
            Item.name.ilike(f"%{query}%"),
            Item.status == "available"
        ).all()
        if not items:
            await update.message.reply_text(f"'{query}' 검색 결과가 없습니다.\n" + command_guide())
            return

        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        if page < 1:
            page = total_pages
        elif page > total_pages:
            page = 1
        context.user_data["search_page"] = page

        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]

        context.user_data["search_mapping"] = {str(idx): it.id for idx, it in enumerate(page_items, start=1)}

        msg = f"'{query}' 검색 결과 (페이지 {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"
        msg += "\n/next, /prev 로 페이지 이동\n/offer [번호 또는 이름] 으로 거래 요청"
        await update.message.reply_text(msg + command_guide())
    except Exception as e:
        logging.error(f"/search 오류: {e}")
        await update.message.reply_text("상품 검색 중 오류 발생." + command_guide())
    finally:
        session.close()

# ==============================
# /offer
def generate_transaction_id() -> str:
    return ''.join(str(random.randint(0, 9)) for _ in range(12))

@check_banned
async def offer_item(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /offer [번호 또는 상품이름]\n" + command_guide())
        return
    identifier = args[1].strip()
    session = get_db_session()
    try:
        mapping = context.user_data.get("list_mapping") or context.user_data.get("search_mapping") or {}
        if identifier in mapping:
            item_id = mapping[identifier]
            item = session.query(Item).filter_by(id=item_id, status="available").first()
        else:
            try:
                # 숫자로 시도
                item_id_int = int(identifier)
                item = session.query(Item).filter_by(id=item_id_int, status="available").first()
            except ValueError:
                # 이름 검색
                item = session.query(Item).filter(
                    Item.name.ilike(f"%{identifier}%"),
                    Item.status == "available"
                ).first()
        if not item:
            await update.message.reply_text("유효한 상품 번호/이름이 없습니다.\n" + command_guide())
            return

        buyer_id = update.message.from_user.id
        seller_id = item.seller_id
        t_id = generate_transaction_id()
        new_tx = Transaction(item_id=item.id, buyer_id=buyer_id, seller_id=seller_id, amount=item.price, transaction_id=t_id)
        session.add(new_tx)
        session.commit()

        await update.message.reply_text(
            f"'{item.name}' 거래 요청을 생성했습니다.\n거래 ID: {t_id}\n송금 시 메모(거래ID) 꼭 입력하세요!\n" + command_guide()
        )
        try:
            await context.bot.send_message(
                chat_id=seller_id,
                text=(
                    f"상품 '{item.name}'에 거래 요청이 도착했습니다.\n거래 ID: {t_id}\n"
                    "판매자님, /accept 거래ID 판매자지갑 으로 수락하거나, /refusal 거래ID 로 거절해주세요.\n"
                    "※ TRC20 USDT"
                )
            )
        except Exception as e:
            logging.error(f"판매자 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/offer 오류: {e}")
        await update.message.reply_text("거래 요청 중 오류 발생.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /cancel (ConversationHandler)
@check_banned
async def cancel(update: Update, context: CallbackContext) -> int:
    session = get_db_session()
    try:
        seller_id = update.message.from_user.id
        items = session.query(Item).filter(Item.seller_id == seller_id, Item.status == "available").all()
        if not items:
            await update.message.reply_text("취소 가능한 상품이 없습니다.\n" + command_guide())
            return ConversationHandler.END
        page = context.user_data.get("cancel_page", 1)
        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        if page < 1:
            page = total_pages
        elif page > total_pages:
            page = 1
        context.user_data["cancel_page"] = page

        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]

        context.user_data["cancel_mapping"] = {str(idx): it.id for idx, it in enumerate(page_items, start=1)}
        msg = f"취소 가능한 상품 목록 (페이지 {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"
        msg += "\n/next, /prev 로 페이지 이동\n취소할 상품 번호/이름을 입력해주세요.\n(취소: /exit)"
        await update.message.reply_text(msg + command_guide())
        return WAITING_FOR_CANCEL_ID
    except Exception as e:
        logging.error(f"/cancel 오류: {e}")
        await update.message.reply_text("상품 취소 목록 조회 오류.\n" + command_guide())
        return ConversationHandler.END
    finally:
        session.close()

@check_banned
async def cancel_item(update: Update, context: CallbackContext) -> int:
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
                item_id_int = int(identifier)
                item = session.query(Item).filter_by(id=item_id_int, seller_id=seller_id, status="available").first()
            except ValueError:
                item = session.query(Item).filter(
                    Item.name.ilike(f"%{identifier}%"),
                    Item.seller_id == seller_id,
                    Item.status == "available"
                ).first()
        if not item:
            await update.message.reply_text("유효한 상품 번호/이름이 없거나 취소가 불가능합니다.\n" + command_guide())
            return WAITING_FOR_CANCEL_ID

        session.delete(item)
        session.commit()
        await update.message.reply_text(f"'{item.name}' 상품이 취소되었습니다.\n" + command_guide())
        return ConversationHandler.END
    except Exception as e:
        session.rollback()
        logging.error(f"/cancel 처리 오류: {e}")
        await update.message.reply_text("상품 취소 처리 중 오류.\n" + command_guide())
        return WAITING_FOR_CANCEL_ID
    finally:
        session.close()

cancel_handler = ConversationHandler(
    entry_points=[CommandHandler("cancel", cancel)],
    states={
        WAITING_FOR_CANCEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_item)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start), MessageHandler(filters.COMMAND, exit_to_start)],
)

# ==============================
# /accept (판매자 전용)
@check_banned
async def accept_transaction(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("사용법: /accept 거래ID 판매자지갑\n" + command_guide())
        return
    t_id = args[1].strip()
    seller_wallet = args[2].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="pending").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아니거나 이미 처리된 거래입니다.\n" + command_guide())
            return
        if update.message.from_user.id != tx.seller_id:
            await update.message.reply_text("판매자만 사용할 수 있습니다.\n" + command_guide())
            return

        tx.session_id = seller_wallet
        tx.status = "accepted"
        session.commit()

        await update.message.reply_text(f"거래 ID {t_id}가 수락되었습니다.\n(구매자에게 송금 안내를 전송)\n" + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.buyer_id,
                text=(
                    f"거래 ID {t_id}가 수락되었습니다.\n"
                    f"{tx.amount} USDT를 {TRON_WALLET} 로 송금 시, 메모(거래ID:{t_id})를 꼭 입력해주세요."
                )
            )
        except Exception as e:
            logging.error(f"구매자 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/accept 오류: {e}")
        await update.message.reply_text("거래 수락 처리 중 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /refusal (판매자 전용)
@check_banned
async def refusal_transaction(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refusal 거래ID\n" + command_guide())
        return
    t_id = args[1].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="pending").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아니거나 이미 처리된 거래.\n" + command_guide())
            return
        if update.message.from_user.id != tx.seller_id:
            await update.message.reply_text("판매자만 사용 가능합니다.\n" + command_guide())
            return

        session.delete(tx)
        session.commit()
        await update.message.reply_text(f"거래 ID {t_id}가 거절되었습니다.\n" + command_guide())
        try:
            await context.bot.send_message(chat_id=tx.buyer_id, text=f"거래가 거절되었습니다. (ID: {t_id})")
        except Exception as e:
            logging.error(f"구매자 통지 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/refusal 오류: {e}")
        await update.message.reply_text("거래 거절 중 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /checkdeposit (구매자 전용)
@check_banned
async def check_deposit(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("사용법: /checkdeposit 거래ID txid\n" + command_guide())
        return
    t_id = args[1].strip()
    txid = args[2].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="accepted").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아니거나 아직 수락되지 않은 거래.\n" + command_guide())
            return
        valid, deposited_amount = verify_deposit(float(tx.amount), txid, t_id)
        if not valid:
            await update.message.reply_text("입금 확인 실패 (금액 불일치 또는 메모 누락).\n" + command_guide())
            return
        tx.status = "deposit_confirmed"
        session.commit()
        await update.message.reply_text("입금이 확인되었습니다. 판매자에게 안내를 전송합니다.\n" + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.seller_id,
                text=(
                    f"거래 ID {t_id} 입금 확인.\n구매자에게 물품 발송 후, /confirm 으로 최종 완료해주세요."
                )
            )
        except Exception as e:
            logging.error(f"판매자 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/checkdeposit 오류: {e}")
        await update.message.reply_text("입금 확인 중 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /confirm (구매자 전용)
@check_banned
async def confirm_payment(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split()
    if len(args) < 4:
        await update.message.reply_text("사용법: /confirm 거래ID 구매자지갑주소 txid\n" + command_guide())
        return
    t_id = args[1].strip()
    buyer_wallet = args[2].strip()
    txid = args[3].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="deposit_confirmed").first()
        if not tx:
            await update.message.reply_text("아직 입금이 확인되지 않았거나 거래 상태가 올바르지 않습니다.\n" + command_guide())
            return
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 사용할 수 있습니다.\n" + command_guide())
            return

        original_amount = float(tx.amount)
        valid, _ = verify_deposit(original_amount, txid, t_id)
        if not valid:
            await update.message.reply_text("TXID가 유효하지 않거나 메모가 일치하지 않습니다.\n" + command_guide())
            return
        net_amount = original_amount * (1 - NORMAL_COMMISSION_RATE)
        tx.status = "completed"
        session.commit()

        await update.message.reply_text(
            f"입금 최종 확인 ({original_amount} USDT). 판매자에게 {net_amount} USDT 송금.\n" + command_guide()
        )
        try:
            seller_wallet = tx.session_id
            result = send_usdt(seller_wallet, net_amount, memo=t_id)
            await context.bot.send_message(
                chat_id=tx.seller_id,
                text=(
                    f"거래 ID {t_id}가 최종 완료.\n"
                    f"{net_amount} USDT가 판매자 지갑({seller_wallet})으로 송금되었습니다.\n"
                    f"송금 결과: {result}"
                )
            )
        except Exception as e:
            logging.error(f"판매자 송금 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/confirm 오류: {e}")
        await update.message.reply_text("거래 완료 중 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /refund (구매자 전용, ConversationHandler)
@check_banned
async def refund_request(update: Update, context: CallbackContext) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refund 거래ID\n" + command_guide())
        return ConversationHandler.END
    t_id = args[1].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="deposit_confirmed").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아니거나 환불 요청 불가능.\n" + command_guide())
            return ConversationHandler.END
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 환불 요청 가능.\n" + command_guide())
            return ConversationHandler.END

        expected_amount = float(tx.amount)
        # 단순 예시: 원금 5%/2 = 2.5% 수수료 가정
        refund_amount = expected_amount * (1 - (NORMAL_COMMISSION_RATE / 2))

        context.user_data["refund_txid"] = t_id
        context.user_data["refund_amount"] = refund_amount
        await update.message.reply_text(
            f"환불 진행합니다. 구매자 지갑 주소를 입력해주세요.\n(환불금: {refund_amount} USDT)\n(취소: /exit)" + command_guide()
        )
        return WAITING_FOR_REFUND_WALLET
    except Exception as e:
        logging.error(f"/refund 오류: {e}")
        await update.message.reply_text("환불 요청 중 오류.\n" + command_guide())
        return ConversationHandler.END
    finally:
        session.close()

@check_banned
async def process_refund(update: Update, context: CallbackContext) -> int:
    buyer_wallet = update.message.text.strip()
    if buyer_wallet.lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    t_id = context.user_data.get("refund_txid")
    refund_amount = context.user_data.get("refund_amount")
    try:
        result = send_usdt(buyer_wallet, refund_amount, memo=t_id)
        await update.message.reply_text(
            f"환불 완료: {refund_amount} USDT → {buyer_wallet}\n거래ID: {t_id}\n결과: {result}\n" + command_guide()
        )
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"환불 송금 오류: {e}")
        await update.message.reply_text("환불 송금 중 오류 발생. 다시 시도.\n" + command_guide())
        return WAITING_FOR_REFUND_WALLET

refund_handler = ConversationHandler(
    entry_points=[CommandHandler("refund", refund_request)],
    states={
        WAITING_FOR_REFUND_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_refund)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start), MessageHandler(filters.COMMAND, exit_to_start)],
)

# ==============================
# /rate (거래 완료 후 평점, ConversationHandler)
@check_banned
async def rate_user(update: Update, context: CallbackContext) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /rate 거래ID\n" + command_guide())
        return WAITING_FOR_RATING
    t_id = args[1].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("완료된 거래가 아니거나 유효하지 않은 거래.\n" + command_guide())
            return WAITING_FOR_RATING
        context.user_data["rating_txid"] = t_id
        await update.message.reply_text("평점(1~5)을 입력해주세요.\n" + command_guide())
        return WAITING_FOR_CONFIRMATION
    except Exception as e:
        logging.error(f"/rate 오류: {e}")
        return WAITING_FOR_RATING
    finally:
        session.close()

@check_banned
async def save_rating(update: Update, context: CallbackContext) -> int:
    session = get_db_session()
    try:
        score = int(update.message.text.strip())
        if not (1 <= score <= 5):
            await update.message.reply_text("평점은 1~5만 가능합니다.\n" + command_guide())
            return WAITING_FOR_CONFIRMATION
        t_id = context.user_data.get("rating_txid")
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("유효한 거래가 아닙니다.\n" + command_guide())
            return ConversationHandler.END

        target_id = tx.seller_id if update.message.from_user.id == tx.buyer_id else tx.buyer_id
        new_rating = Rating(user_id=target_id, score=score, review="익명")
        session.add(new_rating)
        session.commit()

        await update.message.reply_text(f"{score}점 평점이 등록되었습니다.\n" + command_guide())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("숫자를 입력해주세요.\n" + command_guide())
        return WAITING_FOR_CONFIRMATION
    except Exception as e:
        session.rollback()
        logging.error(f"/rate 오류: {e}")
        return WAITING_FOR_CONFIRMATION
    finally:
        session.close()

rate_handler = ConversationHandler(
    entry_points=[CommandHandler("rate", rate_user)],
    states={
        WAITING_FOR_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rating)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start), MessageHandler(filters.COMMAND, exit_to_start)],
)

# ==============================
# /chat (거래 당사자 간 채팅)
@check_banned
async def start_chat(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /chat 거래ID\n" + command_guide())
        return
    t_id = args[1].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter(
            Transaction.transaction_id == t_id,
            Transaction.status.in_(["accepted", "deposit_confirmed", "deposit_confirmed_over", "completed"])
        ).first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아니거나 아직 입금이 확인되지 않았습니다.\n" + command_guide())
            return
        user_id = update.message.from_user.id
        if user_id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("이 거래의 당사자가 아닙니다.\n" + command_guide())
            return

        active_chats[t_id] = (tx.buyer_id, tx.seller_id)
        context.user_data["current_chat_tx"] = t_id
        await update.message.reply_text("채팅 시작. 텍스트/파일 전송 시 상대방에게 전달.\n" + command_guide())
    except Exception as e:
        logging.error(f"/chat 오류: {e}")
        await update.message.reply_text("채팅 시작 오류.\n" + command_guide())
    finally:
        session.close()

@check_banned
async def relay_message(update: Update, context: CallbackContext) -> None:
    t_id = context.user_data.get("current_chat_tx")
    if not t_id or t_id not in active_chats:
        return
    buyer_id, seller_id = active_chats[t_id]
    sender = update.message.from_user.id
    partner = None
    if sender == buyer_id:
        partner = seller_id
    elif sender == seller_id:
        partner = buyer_id

    if not partner:
        return

    try:
        if update.message.document:
            file_id = update.message.document.file_id
            file_name = update.message.document.file_name
            await context.bot.send_document(chat_id=partner, document=file_id, caption=f"[파일] {file_name}")
        elif update.message.photo:
            photo = update.message.photo[-1]
            await context.bot.send_photo(chat_id=partner, photo=photo.file_id, caption="[사진]")
        else:
            msg_text = update.message.text or "[빈 메시지]"
            await context.bot.send_message(chat_id=partner, text=f"[채팅] {msg_text}")
    except Exception as e:
        logging.error(f"채팅 중계 오류: {e}")

# ==============================
# /off (거래 중단)
@check_banned
async def off_transaction(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /off 거래ID\n" + command_guide())
        return
    t_id = args[1].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아닙니다.\n" + command_guide())
            return
        if tx.status not in ["pending", "accepted", "deposit_confirmed", "deposit_confirmed_over"]:
            await update.message.reply_text("이미 완료되었거나 해당 상태에서 중단 불가능.\n" + command_guide())
            return
        if update.message.from_user.id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("해당 거래의 당사자가 아닙니다.\n" + command_guide())
            return

        tx.status = "cancelled"
        session.commit()

        if t_id in active_chats:
            active_chats.pop(t_id)
        await update.message.reply_text(f"거래 ID {t_id}가 중단되었습니다.\n" + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/off 오류: {e}")
        await update.message.reply_text("거래 중단 처리 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# 관리자 전용 명령어: /warexit, /adminsearch, /ban, /unban, /post
@check_banned
async def warexit_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용 가능합니다.\n" + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /warexit 거래ID\n" + command_guide())
        return
    t_id = args[1].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아닙니다.\n" + command_guide())
            return
        tx.status = "cancelled"
        session.commit()
        if t_id in active_chats:
            active_chats.pop(t_id)
        await update.message.reply_text(f"[관리자] 거래 ID {t_id}를 강제 종료했습니다.\n" + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/warexit 오류: {e}")
        await update.message.reply_text("강제 종료 중 오류.\n" + command_guide())
    finally:
        session.close()

@check_banned
async def adminsearch_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 가능합니다.\n" + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /adminsearch 거래ID\n" + command_guide())
        return
    tid = args[1].strip()

    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=tid).first()
        if not tx:
            await update.message.reply_text("해당 거래를 찾을 수 없음.\n")
            return
        await update.message.reply_text(
            f"거래 ID {tid}\n구매자: {tx.buyer_id}\n판매자: {tx.seller_id}\n상태: {tx.status}"
        )
    except Exception as e:
        logging.error(f"/adminsearch 오류: {e}")
        await update.message.reply_text("관리자 검색 중 오류.\n")
    finally:
        session.close()

@check_banned
async def ban_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용 가능.\n" + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /ban 텔레그램ID\n" + command_guide())
        return
    try:
        ban_id = int(args[1].strip())
        BANNED_USERS.add(ban_id)
        await update.message.reply_text(f"텔레그램 ID {ban_id} 차단 완료.")
    except ValueError:
        await update.message.reply_text("유효한 정수 ID를 입력.\n")

@check_banned
async def unban_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용 가능.\n" + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /unban 텔레그램ID\n" + command_guide())
        return
    try:
        unban_id = int(args[1].strip())
        if unban_id in BANNED_USERS:
            BANNED_USERS.remove(unban_id)
            await update.message.reply_text(f"ID {unban_id} 차단 해제.")
        else:
            await update.message.reply_text(f"ID {unban_id}는 차단 목록에 없습니다.")
    except ValueError:
        await update.message.reply_text("정수 ID를 입력.\n")

@check_banned
async def post_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용 가능.\n" + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /post 공지내용\n" + command_guide())
        return
    notice = args[1].strip()
    sent_count = 0
    for user_id in REGISTERED_USERS:
        if user_id in BANNED_USERS:
            continue
        try:
            await context.bot.send_message(chat_id=user_id, text=f"[공지]\n{notice}")
            sent_count += 1
        except Exception as e:
            logging.error(f"공지 전송 오류(대상: {user_id}): {e}")

    await update.message.reply_text(f"공지 전송 완료 ({sent_count}명).")

# ==============================
# 11. 에러 핸들러
async def error_handler(update: object, context: CallbackContext) -> None:
    logging.error("오류 발생", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("오류가 발생했습니다. 다시 시도해주세요.\n" + command_guide())

# ==============================
# 12. 메인 실행부
def main():
    # 1) Webhook 해제
    if TELEGRAM_API_KEY:
        remove_webhook(TELEGRAM_API_KEY)
    else:
        logging.error("TELEGRAM_API_KEY가 설정되지 않았습니다. 봇이 동작하지 않을 수 있음.")

    # 2) Application
    app = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    # 3) 에러 핸들러
    app.add_error_handler(error_handler)

    # 4) 그룹 0에서 모든 메시지 -> register_user
    app.add_handler(MessageHandler(filters.ALL, register_user), group=0)

    # 5) 명령어 핸들러
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("list", list_items_command))
    app.add_handler(CommandHandler("next", next_page))
    app.add_handler(CommandHandler("prev", prev_page))
    app.add_handler(CommandHandler("search", search_items_command))
    app.add_handler(CommandHandler("offer", offer_item))
    app.add_handler(CommandHandler("accept", accept_transaction))
    app.add_handler(CommandHandler("refusal", refusal_transaction))
    app.add_handler(CommandHandler("checkdeposit", check_deposit))
    app.add_handler(CommandHandler("confirm", confirm_payment))
    app.add_handler(CommandHandler("off", off_transaction))

    # 관리자
    app.add_handler(CommandHandler("warexit", warexit_command))
    app.add_handler(CommandHandler("adminsearch", adminsearch_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("post", post_command))

    # 공통
    app.add_handler(CommandHandler("chat", start_chat))
    app.add_handler(CommandHandler("exit", exit_to_start))

    # 6) ConversationHandlers
    app.add_handler(sell_handler)
    app.add_handler(cancel_handler)
    app.add_handler(rate_handler)
    app.add_handler(refund_handler)

    # 7) 채팅 중계 (command 제외)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay_message))

    # 8) JobQueue로 자동 입금 확인
    app.job_queue.run_repeating(auto_verify_deposits, interval=60, first=10)

    # 9) run_polling
    app.run_polling()

if __name__ == "__main__":
    main()