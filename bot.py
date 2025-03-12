import logging
import random
import os
import requests
from requests.adapters import HTTPAdapter, Retry
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    CallbackContext,
)
from telegram.constants import ParseMode

# SQLAlchemy 임포트
from sqlalchemy import create_engine, Column, Integer, String, DECIMAL, BigInteger, Text, TIMESTAMP, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session

from tronpy import Tron
from tronpy.providers import HTTPProvider

# -------------------------------------------------------------------
# 로깅 설정
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# -------------------------------------------------------------------
# 환경 변수 / 전역 변수
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")        # 봇 토큰 (필수)
DATABASE_URL = os.getenv("DATABASE_URL")                # "postgres://user:pass@host:5432/dbname"
TRON_API = os.getenv("TRON_API") or "https://api.trongrid.io"
TRON_API_KEY = os.getenv("TRON_API_KEY") or ""
TRON_WALLET = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"
PRIVATE_KEY = os.getenv("PRIVATE_KEY") or ""
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "999999999"))
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

USED_TXIDS = set()      # 이미 사용된 TXID 기록
NETWORK_FEE = 0.1       # 트론송금 시 네트워크 수수료 (USDT)
BANNED_USERS = set()    # 차단된 사용자
REGISTERED_USERS = set()# 봇과 대화한 사용자

NORMAL_COMMISSION_RATE = 0.05
OVERSEND_COMMISSION_RATE = 0.075

# -------------------------------------------------------------------
# requests 세션 설정
http_session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
http_adapter = HTTPAdapter(max_retries=retries)
http_session.mount("https://", http_adapter)
http_session.mount("http://", http_adapter)

# -------------------------------------------------------------------
# SQLAlchemy 설정
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

# -------------------------------------------------------------------
# 데이터베이스 모델
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
    session_id = Column(Text)
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

Base.metadata.create_all(bind=engine)

# -------------------------------------------------------------------
# Tronpy 설정
client = Tron(provider=HTTPProvider(TRON_API, api_key=TRON_API_KEY))

# -------------------------------------------------------------------
# 송금/검증 함수
def verify_deposit(expected_amount: float, txid: str, internal_txid: str) -> (bool, float):
    try:
        tx = client.get_transaction(txid)
        contract_info = tx["raw_data"]["contract"][0]["parameter"]["value"]
        transferred_amount = int(contract_info.get("amount", 0))
        data_hex = contract_info.get("data", "")
        memo = bytes.fromhex(data_hex).decode("utf-8") if data_hex else ""
        actual_amount = transferred_amount / 1e6
        if transferred_amount != int(expected_amount * 1e6):
            return (False, actual_amount)
        if internal_txid.lower() not in memo.lower():
            return (False, actual_amount)
        return (True, actual_amount)
    except Exception as e:
        logging.error(f"블록체인 거래 검증 오류: {e}")
        return (False, 0)

def verify_deposit_txid(expected_amount: float, txid: str) -> (bool, float):
    try:
        if txid in USED_TXIDS:
            logging.error(f"txid {txid}는 이미 사용되었습니다.")
            return (False, 0)
        tx = client.get_transaction(txid)
        contract_info = tx["raw_data"]["contract"][0]["parameter"]["value"]
        transferred_amount = int(contract_info.get("amount", 0))
        actual_amount = transferred_amount / 1e6
        if transferred_amount != int(expected_amount * 1e6):
            return (False, actual_amount)
        return (True, actual_amount)
    except Exception as e:
        logging.error(f"TXID 검증 오류: {e}")
        return (False, 0)

def check_usdt_payment(expected_amount: float, txid: str = "", internal_txid: str = "") -> (bool, float):
    if txid and internal_txid:
        return verify_deposit(expected_amount, txid, internal_txid)
    try:
        contract = client.get_contract(USDT_CONTRACT)
        balance = contract.functions.balanceOf(TRON_WALLET)
        return ((balance / 1e6) >= expected_amount, balance / 1e6)
    except Exception as e:
        logging.error(f"USDT 잔액 확인 오류: {e}")
        return (False, 0)

def send_usdt(to_address: str, amount: float, memo: str = "") -> dict:
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

def fetch_recent_trc20_transactions(address: str, limit: int = 50) -> list:
    url = f"{TRON_API.rstrip('/')}/v1/accounts/{address}/transactions/trc20"
    headers = {"Accept": "application/json"}
    if TRON_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRON_API_KEY
    params = {
        "limit": limit,
        "contract_address": USDT_CONTRACT,
        "only_confirmed": "true"
    }
    try:
        resp = http_session.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])
    except Exception as e:
        logging.error(f"TronGrid TRC20 조회 오류: {e}")
        return []

def parse_trc20_transaction(tx_info: dict) -> (float, str):
    try:
        raw_amount = tx_info.get("value", "0")
        amount_float = float(raw_amount) / 1e6
    except Exception:
        amount_float = 0
    memo_hex = tx_info.get("data", "")
    try:
        memo_str = bytes.fromhex(memo_hex).decode("utf-8") if memo_hex else ""
    except Exception:
        memo_str = ""
    return amount_float, memo_str

# -------------------------------------------------------------------
# async 자동 입금 확인
async def process_deposit_confirmation(session, tx, deposited_amount: float, context: CallbackContext):
    original_amount = float(tx.amount)
    buyer_id = tx.buyer_id
    seller_id = tx.seller_id

    if deposited_amount < original_amount:
        # 부족분 환불
        refund_result = send_usdt(buyer_id, deposited_amount, memo=tx.transaction_id)
        await context.bot.send_message(
            chat_id=buyer_id,
            text=(
                f"입금액 {deposited_amount} USDT가 부족합니다 (필요: {original_amount} USDT).\n"
                f"전액 환불 처리되었습니다. 환불 결과: {refund_result}\n정확한 금액을 다시 송금해주세요."
            ),
        )
        tx.status = "cancelled"
        session.commit()

    elif deposited_amount > original_amount:
        # 오버송금
        tx.status = "deposit_confirmed_over"
        session.commit()
        await context.bot.send_message(
            chat_id=buyer_id,
            text=(
                f"입금액 {deposited_amount} USDT가 원래 금액 {original_amount} USDT보다 초과되었습니다.\n"
                "초과 송금의 경우, /refund 명령어를 사용하여 환불 요청해 주세요."
            ),
        )
        await context.bot.send_message(
            chat_id=seller_id,
            text=(
                f"입금액이 초과되었습니다 (입금액: {deposited_amount} USDT).\n"
                "구매자에게 초과 환불 절차를 안내해 주세요.\n해당 거래는 진행되지 않습니다."
            ),
        )
    else:
        # 정상 입금
        tx.status = "deposit_confirmed"
        session.commit()
        await context.bot.send_message(
            chat_id=buyer_id,
            text=(
                "입금이 확인되었습니다.\n판매자님, 구매자에게 물품을 발송해 주세요.\n"
                "물품 발송 후, 구매자께서는 /confirm 명령어를 입력하여 최종 거래를 완료해 주세요."
            ),
        )
        await context.bot.send_message(
            chat_id=seller_id,
            text=(
                "입금이 확인되었습니다.\n구매자에게 물품을 발송해 주시기 바랍니다.\n"
                "구매자가 /confirm 명령어를 입력하면 거래가 최종 완료됩니다."
            ),
        )

async def auto_verify_deposits(context: CallbackContext):
    session = get_db_session()
    try:
        accepted_txs = session.query(Transaction).filter_by(status="accepted").all()
        if not accepted_txs:
            return
        recent_txs = fetch_recent_trc20_transactions(TRON_WALLET, limit=50)
        parsed = {}
        for tx_info in recent_txs:
            tx_id = tx_info.get("transaction_id", "")
            if not tx_id:
                continue
            amt, memo = parse_trc20_transaction(tx_info)
            parsed[tx_id] = (amt, memo)

        for tx in accepted_txs:
            for tx_id, (amt, memo) in parsed.items():
                if tx.transaction_id.lower() in memo.lower():
                    if amt >= float(tx.amount):
                        await process_deposit_confirmation(session, tx, amt, context)
                        break
    except Exception as e:
        logging.error(f"자동 입금 확인 오류: {e}")
    finally:
        session.close()

# -------------------------------------------------------------------
# Conversation States
(WAITING_FOR_ITEM_NAME,
 WAITING_FOR_PRICE,
 WAITING_FOR_ITEM_TYPE,
 WAITING_FOR_CANCEL_ID,
 WAITING_FOR_RATING,
 WAITING_FOR_CONFIRMATION,
 WAITING_FOR_REFUND_WALLET) = range(7)

ITEMS_PER_PAGE = 10
active_chats = {}  # {거래ID: (buyer_id, seller_id)}

# -------------------------------------------------------------------
# 관리자, 차단 사용자 체크용 데코레이터
def check_banned(func):
    @wraps(func)
    async def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user and update.effective_user.id in BANNED_USERS:
            await update.message.reply_text("차단된 사용자입니다.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# -------------------------------------------------------------------
# 사용자 등록
@check_banned
async def register_user(update: Update, context: CallbackContext) -> None:
    if update.effective_user:
        REGISTERED_USERS.add(update.effective_user.id)

# -------------------------------------------------------------------
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
        "/exit - 대화 종료 및 초기화"
    )

# -------------------------------------------------------------------
# 오류 핸들러
async def error_handler(update: object, context: CallbackContext) -> None:
    logging.error("Exception while handling an update:", exc_info=context.error)

# -------------------------------------------------------------------
# /start
@check_banned
async def start_command(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        "에스크로 거래 봇에 오신 것을 환영합니다!\n문제 발생 시 관리자에게 문의하세요."
        + command_guide()
    )
    return ConversationHandler.END

# -------------------------------------------------------------------
# /exit
@check_banned
async def exit_to_start(update: Update, context: CallbackContext) -> int:
    context.user_data.clear()
    await update.message.reply_text("대화가 취소되었습니다. 초기 화면으로 돌아갑니다.\n" + command_guide())
    return ConversationHandler.END

# -------------------------------------------------------------------
# /sell 대화 흐름
@check_banned
async def sell_command(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("판매할 상품의 이름을 입력해주세요.\n(취소: /exit)" + command_guide())
    return WAITING_FOR_ITEM_NAME

@check_banned
async def set_item_name(update: Update, context: CallbackContext) -> int:
    if update.message.text.strip().lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    context.user_data["item_name"] = update.message.text.strip()
    await update.message.reply_text("상품의 가격(USDT)을 숫자로 입력해주세요.\n(취소: /exit)" + command_guide())
    return WAITING_FOR_PRICE

@check_banned
async def set_item_price(update: Update, context: CallbackContext) -> int:
    if update.message.text.strip().lower() in ["/exit", "exit"]:
        return await exit_to_start(update, context)
    try:
        price = float(update.message.text.strip())
        context.user_data["price"] = price
        await update.message.reply_text("상품 종류를 입력해주세요. (디지털/현물)\n(취소: /exit)" + command_guide())
        return WAITING_FOR_ITEM_TYPE
    except ValueError:
        await update.message.reply_text("유효한 가격을 숫자로 입력해주세요.\n(취소: /exit)" + command_guide())
        return WAITING_FOR_PRICE

@check_banned
async def set_item_type(update: Update, context: CallbackContext) -> int:
    if update.message.text.strip().lower() in ["/exit", "exit"]:
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
        await update.message.reply_text(f"'{item_name}' 상품이 등록되었습니다." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"상품 등록 오류: {e}")
        await update.message.reply_text("상품 등록 중 오류가 발생했습니다. 다시 시도해주세요." + command_guide())
    finally:
        session.close()
    return ConversationHandler.END

# -------------------------------------------------------------------
# /list, /next, /prev
@check_banned
async def list_items_command(update: Update, context: CallbackContext) -> None:
    session = get_db_session()
    try:
        page = context.user_data.get("list_page", 1)
        items = session.query(Item).filter(Item.status == "available").all()
        if not items:
            await update.message.reply_text("등록된 상품이 없습니다." + command_guide())
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

@check_banned
async def next_page(update: Update, context: CallbackContext) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) + 1
    await list_items_command(update, context)

@check_banned
async def prev_page(update: Update, context: CallbackContext) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) - 1
    await list_items_command(update, context)

# -------------------------------------------------------------------
# /search
@check_banned
async def search_items_command(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("검색어를 입력해주세요. 예: /search 마우스" + command_guide())
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
            await update.message.reply_text(f"'{query}' 검색 결과가 없습니다." + command_guide())
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

# -------------------------------------------------------------------
# /offer
@check_banned
async def offer_item(update: Update, context: CallbackContext) -> None:
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
            f"상품 '{item.name}'에 대한 거래 요청이 생성되었습니다!\n거래 ID: {t_id}\n※ 송금 시 메모(거래 ID) 꼭 입력해주세요."
            + command_guide()
        )
        try:
            await context.bot.send_message(
                chat_id=seller_id,
                text=(
                    f"당신의 상품 '{item.name}'에 거래 요청이 도착했습니다.\n거래 ID: {t_id}\n"
                    "판매자께서는 /accept 거래ID 판매자지갑주소 로 수락하거나, /refusal 거래ID 로 거절해주세요.\n"
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

# -------------------------------------------------------------------
# /cancel
@check_banned
async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("취소할 상품을 선택해주세요.\n(취소: /exit)" + command_guide())
    session = get_db_session()
    try:
        seller_id = update.message.from_user.id
        items = session.query(Item).filter(Item.seller_id == seller_id, Item.status == "available").all()
        if not items:
            await update.message.reply_text("취소할 수 있는 상품이 없습니다." + command_guide())
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
        msg += "\n/next 또는 /prev 로 페이지 이동\n취소할 상품 번호/이름을 입력해주세요.\n(취소: /exit)"
        await update.message.reply_text(msg + command_guide())
        return WAITING_FOR_CANCEL_ID
    except Exception as e:
        logging.error(f"/cancel 오류: {e}")
        await update.message.reply_text("상품 취소 목록 조회 중 오류가 발생했습니다." + command_guide())
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
                item = session.query(Item).filter_by(
                    id=int(identifier), seller_id=seller_id, status="available"
                ).first()
            except ValueError:
                item = session.query(Item).filter(
                    Item.name.ilike(f"%{identifier}%"),
                    Item.seller_id == seller_id,
                    Item.status == "available"
                ).first()
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

# -------------------------------------------------------------------
# /accept, /refusal
@check_banned
async def accept_transaction(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("사용법: /accept 거래ID 판매자지갑주소" + command_guide())
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
        tx.session_id = seller_wallet
        tx.status = "accepted"
        session.commit()
        await update.message.reply_text(f"거래 ID {t_id}가 수락되었습니다. 구매자에게 입금 안내를 전송합니다." + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.buyer_id,
                text=(
                    f"거래 ID {t_id}가 수락되었습니다.\n해당 금액({tx.amount} USDT)를 {TRON_WALLET} 로 송금하실 때, "
                    "메모(거래ID)를 반드시 기입해 주세요."
                )
            )
        except Exception as e:
            logging.error(f"구매자 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/accept 오류: {e}")
        await update.message.reply_text("거래 수락 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

@check_banned
async def refusal_transaction(update: Update, context: CallbackContext) -> None:
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
            await update.message.reply_text("판매자만 이 명령어를 사용할 수 있습니다." + command_guide())
            return
        session.delete(tx)
        session.commit()
        await update.message.reply_text(f"거래 ID {t_id}가 거절되었습니다." + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.buyer_id,
                text=f"거래 제안이 거절되었습니다. (거래 ID {t_id})"
            )
        except Exception as e:
            logging.error(f"구매자 거절 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/refusal 오류: {e}")
        await update.message.reply_text("거래 거절 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# -------------------------------------------------------------------
# /checkdeposit
@check_banned
async def check_deposit(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("사용법: /checkdeposit 거래ID txid" + command_guide())
        return
    t_id = args[1].strip()
    txid = args[2].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="accepted").first()
        if not tx:
            await update.message.reply_text("유효한 거래가 아니거나 아직 수락되지 않은 거래입니다." + command_guide())
            return
        valid, deposited_amount = verify_deposit_txid(float(tx.amount), txid)
        if not valid:
            await update.message.reply_text("입금 내역을 확인할 수 없거나 금액이 일치하지 않습니다." + command_guide())
            return
        USED_TXIDS.add(txid)
        tx.status = "deposit_confirmed"
        session.commit()
        await update.message.reply_text("입금이 확인되었습니다. 판매자와 구매자에게 안내 메시지를 보냅니다." + command_guide())
        await context.bot.send_message(
            chat_id=tx.buyer_id,
            text=(
                "입금이 확인되었습니다.\n판매자께서는 구매자에게 물품을 발송해 주세요.\n"
                "물품 발송 후, 구매자께서는 /confirm 명령어로 최종 거래를 완료해주세요."
            )
        )
        await context.bot.send_message(
            chat_id=tx.seller_id,
            text=(
                "입금이 확인되었습니다.\n구매자에게 물품을 발송해 주시기 바랍니다.\n"
                "구매자가 /confirm 명령어를 입력하면 거래가 최종 완료됩니다."
            )
        )
    except Exception as e:
        session.rollback()
        logging.error(f"/checkdeposit 오류: {e}")
        await update.message.reply_text("입금 확인 처리 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# -------------------------------------------------------------------
# /confirm
@check_banned
async def confirm_payment(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split()
    if len(args) < 4:
        await update.message.reply_text("사용법: /confirm 거래ID 구매자지갑주소 txid" + command_guide())
        return
    t_id = args[1].strip()
    buyer_wallet = args[2].strip()
    txid = args[3].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="deposit_confirmed").first()
        if not tx:
            await update.message.reply_text("해당 거래는 입금 확인되지 않았거나 아직 물품 발송 전입니다." + command_guide())
            return
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 이 명령어를 사용할 수 있습니다." + command_guide())
            return
        valid, _ = verify_deposit_txid(float(tx.amount), txid)
        if not valid:
            await update.message.reply_text("TXID가 일치하지 않습니다. 다시 확인해주세요." + command_guide())
            return
        original_amount = float(tx.amount)
        if original_amount <= NETWORK_FEE:
            await update.message.reply_text("송금 금액이 네트워크 수수료보다 적습니다. 관리자에게 문의하세요." + command_guide())
            return
        net_amount = (original_amount - NETWORK_FEE) * (1 - NORMAL_COMMISSION_RATE)
        tx.status = "completed"
        session.commit()
        await update.message.reply_text(
            f"입금이 최종 확인되었습니다 ({original_amount} USDT). 판매자에게 {net_amount} USDT 송금합니다..." + command_guide()
        )
        try:
            seller_wallet = tx.session_id
            result = send_usdt(seller_wallet, net_amount, memo=t_id)
            await context.bot.send_message(
                chat_id=tx.seller_id,
                text=(
                    f"거래 ID {t_id}가 최종 완료되었습니다.\n"
                    f"{net_amount} USDT가 판매자 지갑({seller_wallet})으로 송금되었습니다.\n"
                    f"송금 결과: {result}\n구매자님, 물품 수령 후 확인 부탁드립니다!"
                ),
            )
        except Exception as e:
            logging.error(f"판매자 송금 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/confirm 오류: {e}")
        await update.message.reply_text("거래 완료 처리 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# -------------------------------------------------------------------
# /rate
@check_banned
async def rate_user(update: Update, context: CallbackContext) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /rate 거래ID" + command_guide())
        return WAITING_FOR_RATING
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("완료된 거래가 아니거나 유효하지 않은 거래 ID입니다." + command_guide())
            return WAITING_FOR_RATING
        context.user_data["rating_txid"] = t_id
        await update.message.reply_text("평점(1~5)을 입력해주세요." + command_guide())
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
            await update.message.reply_text("평점은 1~5 사이여야 합니다." + command_guide())
            return WAITING_FOR_CONFIRMATION
        t_id = context.user_data.get("rating_txid")
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("유효한 거래가 아닙니다." + command_guide())
            return ConversationHandler.END
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

# -------------------------------------------------------------------
# /chat
@check_banned
async def start_chat(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /chat 거래ID" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).filter(
            Transaction.status.in_(["accepted", "deposit_confirmed", "deposit_confirmed_over", "completed"])
        ).first()
        if not tx:
            await update.message.reply_text("유효한 거래가 아니거나 아직 입금 확인되지 않은 거래입니다." + command_guide())
            return
        user_id = update.message.from_user.id
        if user_id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("이 거래의 당사자가 아니므로 채팅을 시작할 수 없습니다." + command_guide())
            return
        active_chats[t_id] = (tx.buyer_id, tx.seller_id)
        context.user_data["current_chat_tx"] = t_id
        await update.message.reply_text("채팅을 시작합니다. 텍스트/파일(사진, 문서) 전송 시 상대방에게 전달됩니다." + command_guide())
    except Exception as e:
        logging.error(f"/chat 오류: {e}")
        await update.message.reply_text("채팅 시작 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

@check_banned
async def relay_message(update: Update, context: CallbackContext) -> None:
    t_id = context.user_data.get("current_chat_tx")
    if not t_id or t_id not in active_chats:
        return
    buyer_id, seller_id = active_chats[t_id]
    sender = update.message.from_user.id
    partner = seller_id if sender == buyer_id else (buyer_id if sender == seller_id else None)
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
            await context.bot.send_message(chat_id=partner, text=f"[채팅] {update.message.text}")
    except Exception as e:
        logging.error(f"채팅 메시지 전송 오류: {e}")

# -------------------------------------------------------------------
# /off
@check_banned
async def off_transaction(update: Update, context: CallbackContext) -> None:
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
        if tx.status not in ["pending", "accepted", "deposit_confirmed", "deposit_confirmed_over"]:
            await update.message.reply_text("이미 진행 중이거나 완료된 거래는 중단할 수 없습니다." + command_guide())
            return
        if update.message.from_user.id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("해당 거래의 당사자가 아닙니다." + command_guide())
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

# -------------------------------------------------------------------
# /refund
@check_banned
async def refund_request(update: Update, context: CallbackContext) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refund 거래ID" + command_guide())
        return ConversationHandler.END
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="deposit_confirmed_over").first()
        if not tx:
            await update.message.reply_text("유효한 거래 ID가 아니거나 환불 요청 불가합니다." + command_guide())
            return ConversationHandler.END
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 환불 요청이 가능합니다." + command_guide())
            return ConversationHandler.END
        expected_amount = float(tx.amount)
        valid, _ = check_usdt_payment(expected_amount, "", t_id)
        if not valid:
            await update.message.reply_text("입금 확인이 안 되었거나 거래 데이터가 이상합니다." + command_guide())
            return ConversationHandler.END
        refund_amount = expected_amount * (1 - (NORMAL_COMMISSION_RATE / 2))  # 2.5% 수수료 가정
        context.user_data["refund_txid"] = t_id
        context.user_data["refund_amount"] = refund_amount
        await update.message.reply_text(
            f"환불을 진행합니다. 구매자 지갑 주소를 입력해주세요.\n(환불 금액: {refund_amount} USDT)\n(취소: /exit)" + command_guide()
        )
        return WAITING_FOR_REFUND_WALLET
    except Exception as e:
        logging.error(f"/refund 오류: {e}")
        await update.message.reply_text("환불 요청 중 오류가 발생했습니다." + command_guide())
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
            f"환불 요청이 완료되었습니다. {refund_amount} USDT가 {buyer_wallet}로 송금되었습니다.\n거래 ID: {t_id}\n송금 결과: {result}"
            + command_guide()
        )
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"환불 송금 오류: {e}")
        await update.message.reply_text("환불 송금 중 오류가 발생했습니다. 다시 시도해주세요." + command_guide())
        return WAITING_FOR_REFUND_WALLET

# -------------------------------------------------------------------
# 관리자 전용
@check_banned
async def warexit_command(update: Update, context: CallbackContext) -> None:
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
        await update.message.reply_text(f"[관리자] 거래 ID {t_id}가 강제 종료되었습니다.")
    except Exception as e:
        session.rollback()
        logging.error(f"/warexit 오류: {e}")
        await update.message.reply_text("강제 종료 처리 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

@check_banned
async def adminsearch_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용할 수 있는 명령어입니다.")
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /adminsearch 거래ID")
        return
    transaction_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=transaction_id).first()
        if not tx:
            await update.message.reply_text("해당 거래 ID가 존재하지 않습니다.")
            return
        await update.message.reply_text(
            f"거래 ID {transaction_id}\n구매자 Telegram ID: {tx.buyer_id}\n판매자 Telegram ID: {tx.seller_id}"
        )
    except Exception as e:
        logging.error(f"/adminsearch 오류: {e}")
        await update.message.reply_text("오류 발생. 다시 시도해주세요.")
    finally:
        session.close()

@check_banned
async def ban_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용할 수 있는 명령어입니다.")
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /ban 텔레그램ID")
        return
    try:
        ban_id = int(args[1].strip())
    except ValueError:
        await update.message.reply_text("유효한 텔레그램 ID를 입력해주세요.")
        return
    BANNED_USERS.add(ban_id)
    await update.message.reply_text(f"텔레그램 ID {ban_id}를 차단했습니다.")

@check_banned
async def unban_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용할 수 있는 명령어입니다.")
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /unban 텔레그램ID")
        return
    try:
        unban_id = int(args[1].strip())
    except ValueError:
        await update.message.reply_text("유효한 텔레그램 ID를 입력해주세요.")
        return
    if unban_id in BANNED_USERS:
        BANNED_USERS.remove(unban_id)
        await update.message.reply_text(f"텔레그램 ID {unban_id} 차단 해제 완료.")
    else:
        await update.message.reply_text(f"텔레그램 ID {unban_id} 은(는) 차단 목록에 없습니다.")

@check_banned
async def post_command(update: Update, context: CallbackContext) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("관리자만 사용할 수 있는 명령어입니다.")
        return
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /post 메시지내용")
        return
    post_message = args[1].strip()
    sent_count = 0
    for user_id in REGISTERED_USERS:
        if user_id in BANNED_USERS:
            continue
        try:
            await context.bot.send_message(chat_id=user_id, text=f"[공지] {post_message}")
            sent_count += 1
        except Exception as e:
            logging.error(f"공지 전송 오류 (user {user_id}): {e}")
    await update.message.reply_text(f"공지 전송 완료 ({sent_count}명에게 전송).")

# -------------------------------------------------------------------
# 대화형 핸들러
from telegram.ext import ConversationHandler

sell_handler = ConversationHandler(
    entry_points=[CommandHandler("sell", sell_command)],
    states={
        WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
        WAITING_FOR_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
        WAITING_FOR_ITEM_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_type)],
    },
    fallbacks=[
        CommandHandler("exit", exit_to_start),
        MessageHandler(filters.COMMAND, exit_to_start),  
    ],
)

cancel_handler = ConversationHandler(
    entry_points=[CommandHandler("cancel", cancel)],
    states={
        WAITING_FOR_CANCEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_item)],
    },
    fallbacks=[
        CommandHandler("exit", exit_to_start),
        MessageHandler(filters.COMMAND, exit_to_start),
    ],
)

rate_handler = ConversationHandler(
    entry_points=[CommandHandler("rate", rate_user)],
    states={
        WAITING_FOR_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rating)],
    },
    fallbacks=[
        CommandHandler("exit", exit_to_start),
        MessageHandler(filters.COMMAND, exit_to_start),
    ],
)

refund_handler = ConversationHandler(
    entry_points=[CommandHandler("refund", refund_request)],
    states={
        WAITING_FOR_REFUND_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_refund)],
    },
    fallbacks=[
        CommandHandler("exit", exit_to_start),
        MessageHandler(filters.COMMAND, exit_to_start),
    ],
)

# -------------------------------------------------------------------
# 메인 실행
def main():
    """
    Fly.io 환경에서 제대로 작동하도록:
    1) Webhook 제거 후 (deleteWebhook) 
    2) run_polling() 으로 getUpdates 폴링
    """
    if not TELEGRAM_API_KEY:
        logging.error("TELEGRAM_API_KEY is empty! Check fly.io secrets.")
        return

    # ** 이전 Webhook 제거 (중요) **
    try:
        resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_API_KEY}/deleteWebhook?drop_pending_updates=true", timeout=10)
        logging.info(f"deleteWebhook response: {resp.status_code}, {resp.text}")
    except Exception as e:
        logging.error(f"deleteWebhook error: {e}")

    app = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    # group=0: 사용자 등록
    app.add_handler(MessageHandler(filters.ALL, register_user), group=0)

    # 주요 명령어
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

    # ConversationHandlers
    app.add_handler(sell_handler)
    app.add_handler(cancel_handler)
    app.add_handler(rate_handler)
    app.add_handler(refund_handler)

    # 채팅 메시지 중계
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay_message))

    app.add_error_handler(error_handler)

    # 자동 입금 확인 (async 함수)
    app.job_queue.run_repeating(auto_verify_deposits, interval=60, first=10)

    # ** 폴링 실행 ** (동기)
    app.run_polling()

if __name__ == "__main__":
    main()