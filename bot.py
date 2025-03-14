import logging
import os
import random
import requests
import asyncio
from functools import wraps

from requests.adapters import HTTPAdapter, Retry

# telegram-bot
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
    CallbackContext
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
# SQLAlchemy 2.0 권장: sqlalchemy.orm.declarative_base 사용
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session

# Tronpy
from tronpy import Tron
from tronpy.providers import HTTPProvider

# ==============================
# 1) 환경변수 (Fly.io 시크릿 등)
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")  # 예: "postgresql://user:pass@host:5432/db"
TRON_API = os.getenv("TRON_API", "https://api.trongrid.io")
TRON_API_KEY = os.getenv("TRON_API_KEY", "")
TRON_WALLET = os.getenv("TRON_WALLET", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
TRON_PASSWORD = os.getenv("TRON_PASSWORD", "")  # 예시
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "999999999"))

# TRC20 USDT
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# ==============================
# 2) requests 세션 (재시도 설정)
http_session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
http_adapter = HTTPAdapter(max_retries=retries)
http_session.mount("https://", http_adapter)
http_session.mount("http://", http_adapter)

# ==============================
# 3) SQLAlchemy 설정
engine = create_engine(
    DATABASE_URL,
    echo=True,  # 콘솔 로그
    connect_args={"options": "-c timezone=utc"},
    future=True,
    pool_pre_ping=True,
)
SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()

def get_db_session():
    return SessionLocal()

# ==============================
# 4) DB 모델
class Item(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(Text, nullable=False)
    price = Column(DECIMAL, nullable=False)
    seller_id = Column(BigInteger, nullable=False)
    status = Column(String, default="available")
    type = Column(String, nullable=False)
    created_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, nullable=False)
    buyer_id = Column(BigInteger, nullable=False)
    seller_id = Column(BigInteger, nullable=False)
    status = Column(String, default="pending")
    session_id = Column(Text)         # 판매자 지갑 주소(accept 시 기록)
    transaction_id = Column(Text, unique=True)
    amount = Column(DECIMAL, nullable=False)
    created_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))

class Rating(Base):
    __tablename__ = "ratings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, nullable=False)
    score = Column(Integer, nullable=False)
    review = Column(Text)
    created_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))

# 테이블 생성
Base.metadata.create_all(bind=engine)

# ==============================
# 5) Tron 설정
TRON_API_CLEAN = TRON_API.rstrip("/")
client = Tron(provider=HTTPProvider(TRON_API_CLEAN, api_key=TRON_API_KEY))

NORMAL_COMMISSION_RATE = 0.05
OVERSEND_COMMISSION_RATE = 0.075

# ==============================
# 6) Webhook 해제 → Polling 사용 (getUpdates 방식)
def remove_webhook(token: str):
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true", timeout=10)
        logging.info(f"deleteWebhook response: {resp.status_code}, {resp.text}")
    except Exception as e:
        logging.error(f"deleteWebhook error: {e}")

# ==============================
# 7) Tron 유틸 (거래조회, 송금)
def fetch_transaction_detail(txid: str) -> dict:
    try:
        url = f"{TRON_API_CLEAN}/v1/transactions/{txid}"
        headers = {"Accept": "application/json"}
        if TRON_API_KEY:
            headers["TRON-PRO-API-KEY"] = TRON_API_KEY
        resp = http_session.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return data[0] if data else {}
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
        return (False, 0)

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
        return (False, 0)

def send_usdt(to_address: str, amount: float, memo: str = "") -> dict:
    if not TRON_PASSWORD:
        logging.warning("TRON_PASSWORD가 설정되지 않음(예시)")
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
# (자동 입금 확인 기능은 제거되었습니다. 수동으로 /checkdeposit 명령어를 사용하세요.)
# ==============================
# 8) 로깅 설정
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# 대화 상태 상수
(WAITING_FOR_ITEM_NAME,
 WAITING_FOR_PRICE,
 WAITING_FOR_ITEM_TYPE,
 WAITING_FOR_CANCEL_ID,
 WAITING_FOR_RATING,
 WAITING_FOR_CONFIRMATION,
 WAITING_FOR_REFUND_WALLET) = range(7)

ITEMS_PER_PAGE = 10
active_chats = {}

BANNED_USERS = set()  # 차단된 사용자 ID 모음
REGISTERED_USERS = set()

# ==============================
# ban 데코레이터
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
# 명령어 안내
def command_guide() -> str:
    return (
        "\n\n사용 가능한 명령어:\n"
        "/sell - 상품 판매 등록\n"
        "/list - 구매 가능한 상품 목록\n"
        "/cancel - 본인이 등록한 상품 취소 (입금 전)\n"
        "/search - 상품 검색\n"
        "/offer - 거래 요청 (목록/검색 후)\n"
        "/accept - 거래 요청 수락 (판매자)\n"
        "/refusal - 거래 요청 거절 (판매자)\n"
        "/checkdeposit - 입금 확인 (구매자)\n"
        "/confirm - 거래 완료 확인 (구매자)\n"
        "/refund - 환불 요청 (구매자)\n"
        "/rate - 거래 종료 후 평점\n"
        "/chat - 거래 당사자 간 채팅\n"
        "/off - 거래 중단\n"
        "/warexit - 거래 강제 종료 (관리자)\n"
        "/adminsearch - 거래 검색 (관리자)\n"
        "/post - 전체공지 (관리자)\n"
        "/ban - 사용자 차단 (관리자)\n"
        "/unban - 사용자 차단 해제 (관리자)\n"
        "/exit - 대화 종료"
    )

# ==============================
# /start
@check_banned
async def start_command(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text("에스크로 거래 봇에 오신 것을 환영합니다!" + command_guide())

@check_banned
async def exit_to_start(update: Update, context: CallbackContext) -> int:
    context.user_data.clear()
    await update.message.reply_text("대화를 취소합니다. /start 로 다시 시작.\n" + command_guide())
    return ConversationHandler.END

# ==============================
# /sell (ConversationHandler)
@check_banned
async def sell_command(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("판매할 상품 이름?\n(취소: /exit)" + command_guide())
    return WAITING_FOR_ITEM_NAME

@check_banned
async def set_item_name(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    context.user_data["item_name"] = update.message.text.strip()
    await update.message.reply_text("상품 가격(USDT)을 숫자로 입력.\n(취소: /exit)" + command_guide())
    return WAITING_FOR_PRICE

@check_banned
async def set_item_price(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    try:
        price = float(update.message.text.strip())
        context.user_data["price"] = price
        await update.message.reply_text("상품 종류? (디지털/현물)\n(취소: /exit)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    except ValueError:
        await update.message.reply_text("숫자로 입력해주세요.\n(취소: /exit)" + command_guide())
        return WAITING_FOR_PRICE

@check_banned
async def set_item_type(update: Update, context: CallbackContext) -> int:
    if update.message.text.lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    itype = update.message.text.strip().lower()
    if itype not in ["디지털", "현물"]:
        await update.message.reply_text("디지털/현물 중에 입력.\n(취소: /exit)" + command_guide())
        return WAITING_FOR_ITEM_TYPE

    name = context.user_data["item_name"]
    price = context.user_data["price"]
    seller_id = update.message.from_user.id

    session = get_db_session()
    try:
        new_item = Item(name=name, price=price, seller_id=seller_id, type=itype)
        session.add(new_item)
        session.commit()
        await update.message.reply_text(f"'{name}' 상품이 등록되었습니다.\n" + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"상품 등록 오류: {e}")
        await update.message.reply_text("상품 등록 중 오류 발생.\n" + command_guide())
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
            await update.message.reply_text("등록된 상품 없음.\n" + command_guide())
            return

        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        if page < 1:
            page = total_pages
        elif page > total_pages:
            page = 1
        context.user_data["list_page"] = page

        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]

        context.user_data["list_mapping"] = {
            str(idx): it.id for idx, it in enumerate(page_items, start=1)
        }

        msg = f"구매 가능한 상품 목록 (페이지 {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"

        msg += "\n/next, /prev 로 페이지 이동\n/offer [번호/이름] 으로 거래 요청"
        await update.message.reply_text(msg + command_guide())
    except Exception as e:
        logging.error(f"/list 오류: {e}")
        await update.message.reply_text("상품 목록 조회 중 오류.\n" + command_guide())
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
        await update.message.reply_text("사용법: /search [검색어]\n" + command_guide())
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
        items = session.query(Item).filter(Item.name.ilike(f"%{query}%"), Item.status == "available").all()
        if not items:
            await update.message.reply_text(f"'{query}' 검색 결과 없음.\n" + command_guide())
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

        context.user_data["search_mapping"] = {
            str(idx): it.id for idx, it in enumerate(page_items, start=1)
        }

        msg = f"'{query}' 검색 결과 (페이지 {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"

        msg += "\n/next, /prev 로 페이지 이동\n/offer [번호/이름] 으로 거래 요청"
        await update.message.reply_text(msg + command_guide())
    except Exception as e:
        logging.error(f"/search 오류: {e}")
        await update.message.reply_text("상품 검색 중 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /offer
@check_banned
async def offer_item(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /offer [번호/상품이름]\n" + command_guide())
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
                item_id_int = int(identifier)
                item = session.query(Item).filter_by(id=item_id_int, status="available").first()
            except ValueError:
                item = session.query(Item).filter(
                    Item.name.ilike(f"%{identifier}%"),
                    Item.status == "available"
                ).first()
        if not item:
            await update.message.reply_text("유효한 상품 번호/이름을 입력.\n" + command_guide())
            return

        buyer_id = update.message.from_user.id
        seller_id = item.seller_id
        t_id = ''.join(str(random.randint(0, 9)) for _ in range(12))

        new_tx = Transaction(
            item_id=item.id,
            buyer_id=buyer_id,
            seller_id=seller_id,
            amount=item.price,
            transaction_id=t_id,
        )
        session.add(new_tx)
        session.commit()

        await update.message.reply_text(
            f"'{item.name}' 거래 요청 생성!\n거래 ID: {t_id}\n(송금 시 메모 필수)\n" + command_guide()
        )
        try:
            await context.bot.send_message(
                chat_id=seller_id,
                text=(
                    f"상품 '{item.name}'에 거래 요청이 도착했습니다.\n거래 ID: {t_id}\n"
                    "판매자: /accept 거래ID 판매자지갑 /refusal 거래ID"
                )
            )
        except Exception as e:
            logging.error(f"판매자 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/offer 오류: {e}")
        await update.message.reply_text("거래 요청 중 오류.\n" + command_guide())
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
            await update.message.reply_text("취소할 상품이 없습니다.\n" + command_guide())
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

        context.user_data["cancel_mapping"] = {
            str(idx): it.id for idx, it in enumerate(page_items, start=1)
        }
        msg = f"취소 가능한 상품 목록 (페이지 {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"

        msg += "\n/next, /prev 로 페이지 이동\n취소할 상품 번호/이름 입력.\n(취소: /exit)"
        await update.message.reply_text(msg + command_guide())
        return WAITING_FOR_CANCEL_ID
    except Exception as e:
        logging.error(f"/cancel 오류: {e}")
        await update.message.reply_text("상품 취소 목록 조회 중 오류.\n" + command_guide())
        return ConversationHandler.END
    finally:
        session.close()

@check_banned
async def cancel_item(update: Update, context: CallbackContext) -> int:
    if update.message.text.strip().lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
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
            await update.message.reply_text("유효한 상품 번호/이름 없음.\n" + command_guide())
            return WAITING_FOR_CANCEL_ID

        session.delete(item)
        session.commit()
        await update.message.reply_text(f"'{item.name}' 상품 취소됨.\n" + command_guide())
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
# /accept (판매자)
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
            await update.message.reply_text("유효한 거래 ID가 아니거나 이미 처리됨.\n" + command_guide())
            return
        if update.message.from_user.id != tx.seller_id:
            await update.message.reply_text("판매자만 가능.\n" + command_guide())
            return
        tx.session_id = seller_wallet
        tx.status = "accepted"
        session.commit()
        await update.message.reply_text(f"거래 ID {t_id} 수락됨. 구매자에게 송금 안내.\n" + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.buyer_id,
                text=(
                    f"거래 ID {t_id} 수락됨.\n"
                    f"해당 금액({tx.amount} USDT)를 {TRON_WALLET} 로 송금할 때, 메모(거래ID:{t_id}) 기입 필수."
                )
            )
        except Exception as e:
            logging.error(f"구매자 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/accept 오류: {e}")
        await update.message.reply_text("거래 수락 중 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /refusal (판매자)
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
            await update.message.reply_text("유효한 거래 ID가 아니거나 이미 처리됨.\n" + command_guide())
            return
        if update.message.from_user.id != tx.seller_id:
            await update.message.reply_text("판매자만 사용 가능.\n" + command_guide())
            return
        session.delete(tx)
        session.commit()
        await update.message.reply_text(f"거래 ID {t_id} 거절됨.\n" + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.buyer_id,
                text=f"거래 제안(거래ID {t_id})이 거절되었습니다."
            )
        except Exception as e:
            logging.error(f"거절 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/refusal 오류: {e}")
        await update.message.reply_text("거래 거절 중 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /checkdeposit (구매자)
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
            await update.message.reply_text("유효한 거래가 아니거나 아직 수락되지 않음.\n" + command_guide())
            return
        valid, deposited_amount = verify_deposit(float(tx.amount), txid, t_id)
        if not valid:
            await update.message.reply_text("입금 내역 확인 실패.\n" + command_guide())
            return
        tx.status = "deposit_confirmed"
        session.commit()
        await update.message.reply_text("입금 확인됨. 안내 메시지 전송.\n" + command_guide())
        await context.bot.send_message(
            chat_id=tx.seller_id,
            text=(f"거래 ID {t_id} 입금 확인됨.\n판매자: 물품 발송 후 /confirm 명령어로 거래 완료 처리하세요.")
        )
    except Exception as e:
        session.rollback()
        logging.error(f"/checkdeposit 오류: {e}")
        await update.message.reply_text("입금 확인 중 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /confirm (구매자)
@check_banned
async def confirm_payment(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split()
    if len(args) < 4:
        await update.message.reply_text("사용법: /confirm 거래ID 구매자지갑 txid\n" + command_guide())
        return
    t_id = args[1].strip()
    buyer_wallet = args[2].strip()
    txid = args[3].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="deposit_confirmed").first()
        if not tx:
            await update.message.reply_text("아직 입금확인 안 됐거나 상태 불일치.\n" + command_guide())
            return
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 사용 가능.\n" + command_guide())
            return
        original_amount = float(tx.amount)
        valid, _ = verify_deposit(original_amount, txid, t_id)
        if not valid:
            await update.message.reply_text("TXID/메모가 일치하지 않음.\n" + command_guide())
            return

        net_amount = original_amount * (1 - NORMAL_COMMISSION_RATE)
        tx.status = "completed"
        session.commit()
        await update.message.reply_text(
            f"입금 최종 확인({original_amount} USDT). 판매자에게 {net_amount} USDT 송금!\n" + command_guide()
        )
        try:
            seller_wallet = tx.session_id
            result = send_usdt(seller_wallet, net_amount, memo=t_id)
            await context.bot.send_message(
                chat_id=tx.seller_id,
                text=(
                    f"거래 ID {t_id} 완료.\n"
                    f"{net_amount} USDT가 판매자 지갑({seller_wallet})으로 송금됨.\n"
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
# /refund (구매자, ConversationHandler)
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
            await update.message.reply_text("유효한 거래 ID 아니거나 환불 불가.\n" + command_guide())
            return ConversationHandler.END
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 환불 요청.\n" + command_guide())
            return ConversationHandler.END

        original_amount = float(tx.amount)
        refund_amount = original_amount * 0.975  # 예: 수수료 2.5%
        context.user_data["refund_txid"] = t_id
        context.user_data["refund_amount"] = refund_amount
        await update.message.reply_text(
            f"환불 진행. 구매자 지갑 주소?\n(환불 금액: {refund_amount} USDT)\n(취소: /exit)" + command_guide()
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
    if update.message.text.strip().lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    buyer_wallet = update.message.text.strip()
    t_id = context.user_data.get("refund_txid")
    refund_amount = context.user_data.get("refund_amount")
    try:
        result = send_usdt(buyer_wallet, refund_amount, memo=t_id)
        await update.message.reply_text(
            f"환불 완료: {refund_amount} USDT → {buyer_wallet}\n거래ID {t_id}\n결과: {result}\n" + command_guide()
        )
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"환불 송금 오류: {e}")
        await update.message.reply_text("환불 송금 중 오류.\n" + command_guide())
        return WAITING_FOR_REFUND_WALLET

refund_handler = ConversationHandler(
    entry_points=[CommandHandler("refund", refund_request)],
    states={
        WAITING_FOR_REFUND_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_refund)],
    },
    fallbacks=[CommandHandler("exit", exit_to_start), MessageHandler(filters.COMMAND, exit_to_start)],
)

# ==============================
# /rate (거래 종료 후 평점)
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
            await update.message.reply_text("완료된 거래 아님.\n" + command_guide())
            return WAITING_FOR_RATING
        context.user_data["rating_txid"] = t_id
        await update.message.reply_text("평점(1~5) 입력.\n" + command_guide())
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
        if score < 1 or score > 5:
            await update.message.reply_text("평점은 1~5.\n" + command_guide())
            return WAITING_FOR_CONFIRMATION
        t_id = context.user_data.get("rating_txid")
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("유효한 거래 아님.\n" + command_guide())
            return ConversationHandler.END

        target_id = tx.seller_id if update.message.from_user.id == tx.buyer_id else tx.buyer_id
        new_rating = Rating(user_id=target_id, score=score, review="익명")
        session.add(new_rating)
        session.commit()
        await update.message.reply_text(f"평점 {score}점 등록!\n" + command_guide())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("숫자로 입력.\n" + command_guide())
        return WAITING_FOR_CONFIRMATION
    except Exception as e:
        session.rollback()
        logging.error(f"/rate 처리 오류: {e}")
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
# /chat + relay_message
@check_banned
async def start_chat(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /chat 거래ID\n" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).filter(
            Transaction.status.in_(["accepted", "deposit_confirmed", "deposit_confirmed_over", "completed"])
        ).first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID 아니거나 상태 불일치.\n" + command_guide())
            return
        user_id = update.message.from_user.id
        if user_id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("해당 거래 당사자 아님.\n" + command_guide())
            return
        active_chats[t_id] = (tx.buyer_id, tx.seller_id)
        context.user_data["current_chat_tx"] = t_id
        await update.message.reply_text("채팅 시작. 메시지/파일 전송 시 상대방에게 전달.\n" + command_guide())
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
    partner = seller_id if sender == buyer_id else buyer_id if sender == seller_id else None
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
        logging.error(f"채팅 메시지 전송 오류: {e}")

# ==============================
# /off
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
            await update.message.reply_text("유효한 거래 ID 아님.\n" + command_guide())
            return
        if tx.status not in ["pending", "accepted", "deposit_confirmed", "deposit_confirmed_over"]:
            await update.message.reply_text("이미 완료되었거나 중단 불가능.\n" + command_guide())
            return
        if update.message.from_user.id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("해당 거래 당사자 아님.\n" + command_guide())
            return
        tx.status = "cancelled"
        session.commit()
        if t_id in active_chats:
            active_chats.pop(t_id)
        await update.message.reply_text(f"거래 ID {t_id}가 중단됨.\n" + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/off 오류: {e}")
        await update.message.reply_text("거래 중단 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /warexit (관리자)
@check_banned
async def warexit_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 가능.\n" + command_guide())
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
            await update.message.reply_text("유효한 거래ID 아님.\n" + command_guide())
            return
        tx.status = "cancelled"
        session.commit()
        if t_id in active_chats:
            active_chats.pop(t_id)
        await update.message.reply_text(f"거래 ID {t_id} 강제 종료됨.\n" + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/warexit 오류: {e}")
        await update.message.reply_text("강제 종료 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /adminsearch (관리자)
@check_banned
async def adminsearch_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 가능.\n" + command_guide())
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
            await update.message.reply_text("거래ID 찾을 수 없음.\n" + command_guide())
            return
        await update.message.reply_text(
            f"거래 ID {tid}\n구매자={tx.buyer_id}\n판매자={tx.seller_id}\n상태={tx.status}"
        )
    except Exception as e:
        logging.error(f"/adminsearch 오류: {e}")
        await update.message.reply_text("관리자 검색 오류.\n" + command_guide())
    finally:
        session.close()

# ==============================
# /post (관리자)
@check_banned
async def post_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 가능.\n" + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /post 내용\n" + command_guide())
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
            logging.error(f"공지 전송 오류(유저 {user_id}): {e}")
    await update.message.reply_text(f"공지 전송 완료 ({sent_count}명).\n")

# ==============================
# /ban, /unban (관리자)
@check_banned
async def ban_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 가능.\n" + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /ban 텔레그램ID\n" + command_guide())
        return
    try:
        ban_id = int(args[1].strip())
        BANNED_USERS.add(ban_id)
        await update.message.reply_text(f"텔레그램 ID {ban_id} 차단.")
    except ValueError:
        await update.message.reply_text("유효한 텔레그램 ID.\n" + command_guide())

@check_banned
async def unban_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 가능.\n" + command_guide())
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /unban 텔레그램ID\n" + command_guide())
        return
    try:
        unban_id = int(args[1].strip())
        if unban_id in BANNED_USERS:
            BANNED_USERS.remove(unban_id)
            await update.message.reply_text(f"텔레그램 ID {unban_id} 차단 해제.")
        else:
            await update.message.reply_text(f"ID {unban_id}는 차단 목록에 없음.")
    except ValueError:
        await update.message.reply_text("유효한 텔레그램 ID.\n" + command_guide())

# ==============================
# 에러 핸들러
async def error_handler(update: object, context: CallbackContext) -> None:
    logging.error("오류 발생", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("오류가 발생했습니다.\n" + command_guide())

# ==============================
# 메인 실행부
def main():
    if not TELEGRAM_API_KEY:
        logging.error("TELEGRAM_API_KEY가 설정되지 않았습니다!")
        return

    # DB 연결 확인
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        logging.error("데이터베이스 연결 오류(Dialect/psycopg2 등): %s", e)
        return

    # Webhook 해제 (Polling 사용)
    remove_webhook(TELEGRAM_API_KEY)

    # Telegram Application 준비 (JobQueue 관련 코드는 제거됨)
    app = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    # 에러 핸들러
    app.add_error_handler(error_handler)

    # 모든 메시지 핸들러 (등록)
    app.add_handler(MessageHandler(filters.ALL, register_user), group=0)

    # 주요 명령어 핸들러 등록
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

    # 관리자 명령어
    app.add_handler(CommandHandler("warexit", warexit_command))
    app.add_handler(CommandHandler("adminsearch", adminsearch_command))
    app.add_handler(CommandHandler("post", post_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))

    # 공통 명령어
    app.add_handler(CommandHandler("chat", start_chat))
    app.add_handler(CommandHandler("exit", exit_to_start))

    # ConversationHandlers
    app.add_handler(sell_handler)
    app.add_handler(cancel_handler)
    app.add_handler(rate_handler)
    app.add_handler(refund_handler)

    # 파일/메시지 중계 핸들러
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay_message))

    # 봇 실행 (Polling 방식)
    app.run_polling()

if __name__ == "__main__":
    main()