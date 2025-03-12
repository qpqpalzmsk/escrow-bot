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
    CallbackContext,
)
from telegram.constants import ParseMode

# SQLAlchemy 임포트 (String 포함)
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

# 전역: 이미 사용된 txid 기록 (중복 사용 방지)
USED_TXIDS = set()

# 송금 시 네트워크 수수료 (예: 0.1 USDT; 실제 수수료에 맞게 조정)
NETWORK_FEE = 0.1

# -------------------------------------------------------------------
# 환경 변수 설정 (Fly.io 등)
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # 예: "postgres://user:pass@host:5432/dbname"
TRON_API = os.getenv("TRON_API")            # 예: "https://api.trongrid.io"
TRON_API_KEY = os.getenv("TRON_API_KEY")      # 실제 API Key
TRON_WALLET = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"  # 봇의 Tron 지갑 주소 (TRC20 USDT 전용)
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "999999999"))

# TRC20 USDT 컨트랙트 주소 (메인넷 – 실제 사용하는 주소)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# -------------------------------------------------------------------
# requests 세션 설정 (재시도/타임아웃)
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
    status = Column(String, default="available")  # available, sold 등
    type = Column(String, nullable=False)           # 디지털 / 현물
    created_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))

class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, nullable=False)
    buyer_id = Column(BigInteger, nullable=False)
    seller_id = Column(BigInteger, nullable=False)
    # 거래 상태: pending -> accepted -> deposit_confirmed -> completed
    status = Column(String, default="pending")
    session_id = Column(Text)  # 판매자가 /accept 시 입력한 지갑 주소 (출금 주소)
    transaction_id = Column(Text, unique=True)  # 12자리 랜덤 거래 ID
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

# 중개 수수료
NORMAL_COMMISSION_RATE = 0.05      # 5%
OVERSEND_COMMISSION_RATE = 0.075    # 7.5% (초과 송금 시)

# -------------------------------------------------------------------
# 송금 및 검증 로직
def verify_deposit(expected_amount: float, txid: str, internal_txid: str) -> (bool, float):
    """
    기존 방식: 블록체인에서 txid를 조회해, 송금 금액과 메모(거래 ID)가 정확한지 검증합니다.
    (메모 기반 – 거래소 지갑은 적용이 어려울 수 있음)
    """
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
    """
    새 방식: 메모 없이 txid만으로 입금 확인.
    이미 사용된 txid는 중복 사용을 방지합니다.
    """
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
    """
    txid와 internal_txid가 주어지면 기존 방식으로 검증하고,
    그렇지 않으면 봇 지갑 잔액을 확인합니다.
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
    봇 지갑에서 to_address로 amount(USDT)를 송금합니다.
    memo(거래 ID 등)는 hex로 인코딩하여 data 필드에 포함합니다.
    """
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

# ─────────────────────────────────────────────
# 자동 입금 확인 기능 (기존 메모 기반 + /checkdeposit용 txid 방식)
def fetch_recent_trc20_transactions(address: str, limit: int = 50) -> list:
    """
    TronGrid API를 사용해 봇 지갑의 최근 TRC20 USDT 트랜잭션을 조회합니다.
    """
    url = f"{TRON_API.rstrip('/')}/v1/accounts/{address}/transactions/trc20"
    headers = {"Accept": "application/json"}
    if TRON_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRON_API_KEY
    params = {"limit": limit, "contract_address": USDT_CONTRACT, "only_confirmed": "true"}
    try:
        resp = http_session.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])
    except Exception as e:
        logging.error(f"TronGrid TRC20 조회 오류: {e}")
        return []

def parse_trc20_transaction(tx_info: dict) -> (float, str):
    """
    TronGrid로 조회한 트랜잭션 정보에서, 송금 금액과 메모(거래 ID)를 파싱합니다.
    """
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

def process_deposit_confirmation(session, tx: Transaction, deposited_amount: float, context: CallbackContext):
    """
    자동 입금 확인 후 처리:
      - 입금액이 부족하면 전액 환불 후 거래 취소
      - 입금액이 초과되면 상태를 "deposit_confirmed_over"로 업데이트하고 /refund 안내
      - 정확한 입금이면 상태를 "deposit_confirmed"로 업데이트하고 양측에 안내 메시지 전송
    """
    original_amount = float(tx.amount)
    if deposited_amount < original_amount:
        refund_result = send_usdt(tx.buyer_id, deposited_amount, memo=tx.transaction_id)
        context.bot.send_message(
            chat_id=tx.buyer_id,
            text=(f"입금액 {deposited_amount} USDT가 부족합니다 (필요: {original_amount} USDT).\n"
                  f"전액 환불 처리되었습니다. 환불 결과: {refund_result}\n정확한 금액을 다시 송금해주세요.")
        )
        tx.status = "cancelled"
        session.commit()
    elif deposited_amount > original_amount:
        tx.status = "deposit_confirmed_over"
        session.commit()
        context.bot.send_message(
            chat_id=tx.buyer_id,
            text=(f"입금액 {deposited_amount} USDT가 원래 금액 {original_amount} USDT보다 초과되었습니다.\n"
                  "초과 송금의 경우, /refund 명령어를 사용하여 환불 요청해 주세요.")
        )
        context.bot.send_message(
            chat_id=tx.seller_id,
            text=(f"입금액이 초과되었습니다 (입금액: {deposited_amount} USDT).\n구매자에게 초과 환불 절차를 안내해 주세요.\n"
                  "해당 거래는 진행되지 않습니다.")
        )
    else:
        tx.status = "deposit_confirmed"
        session.commit()
        context.bot.send_message(
            chat_id=tx.buyer_id,
            text=("입금이 확인되었습니다.\n판매자님, 구매자에게 물품을 발송해 주세요.\n"
                  "물품 발송 후 구매자께서는 /confirm 명령어를 입력하여 최종 거래를 완료해 주세요.")
        )
        context.bot.send_message(
            chat_id=tx.seller_id,
            text=("입금이 확인되었습니다.\n구매자에게 물품을 발송해 주시기 바랍니다.\n"
                  "구매자가 /confirm 명령어를 입력하면 거래가 최종 완료됩니다.")
        )

def auto_verify_deposits(context: CallbackContext):
    """
    주기적으로 'accepted' 상태의 거래에 대해 봇 지갑의 최근 TRC20 입금을 확인하여,
    입금이 확인되면 process_deposit_confirmation()을 호출합니다.
    (메모 기반 확인)
    """
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
                        process_deposit_confirmation(session, tx, amt, context)
                        break
    except Exception as e:
        logging.error(f"자동 입금 확인 오류: {e}")
    finally:
        session.close()

# -------------------------------------------------------------------
# 텔레그램 봇 대화 상태 상수
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
# 명령어 안내 (한글)
def command_guide() -> str:
    return (
        "\n\n사용 가능한 명령어:\n"
        "/sell - 상품 판매 등록\n"
        "/list - 구매 가능한 상품 목록 조회\n"
        "/cancel - 본인이 등록한 상품 취소 (입금 전만 가능)\n"
        "/search - 상품 검색\n"
        "/offer - 거래 요청 (목록/검색 후 사용)\n"
        "/accept - 거래 요청 수락 (판매자 전용, 예: /accept 거래ID 판매자지갑주소)\n"
        "/refusal - 거래 요청 거절 (판매자 전용, 예: /refusal 거래ID)\n"
        "/checkdeposit - 입금 확인 (구매자 전용, 예: /checkdeposit 거래ID txid)\n"
        "/confirm - 거래 완료 확인 (구매자 전용, 예: /confirm 거래ID 구매자지갑주소 txid)\n"
        "/refund - 거래 취소 시 환불 요청 (구매자 전용)\n"
        "/rate - 거래 종료 후 평점 남기기 (예: /rate 거래ID)\n"
        "/chat - 거래 당사자 간 익명 채팅\n"
        "/off - 거래 중단 (예: /off 거래ID)\n"
        "/warexit - 강제 종료 (관리자 전용, 예: /warexit 거래ID)\n"
        "/exit - 대화 종료 및 초기화"
    )

# -------------------------------------------------------------------
# /start
async def start(update: Update, _) -> int:
    await update.message.reply_text(
        "에스크로 거래 봇에 오신 것을 환영합니다!\n문제 발생 시 관리자에게 문의하세요.\n(관리자 ID는 봇 프로필에서 확인)"
        + command_guide()
    )
    return ConversationHandler.END

# -------------------------------------------------------------------
# /sell
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

# -------------------------------------------------------------------
# /list, /next, /prev
async def list_items_command(update: Update, context) -> None:
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

async def next_page(update: Update, context) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) + 1
    await list_items_command(update, context)

async def prev_page(update: Update, context) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) - 1
    await list_items_command(update, context)

# -------------------------------------------------------------------
# /search
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
            f"상품 '{item.name}'에 대한 거래 요청이 생성되었습니다!\n거래 ID: {t_id}\n※ 송금 시 반드시 거래 ID를 메모(참조)에 입력해주세요."
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
# /cancel (입금 전 상품만 취소)
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
        if page < 1:
            page = total_pages
        elif page > total_pages:
            page = 1
        context.user_data["cancel_page"] = page

        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        context.user_data["cancel_mapping"] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}

        msg = f"취소 가능한 상품 목록 (페이지 {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"
        msg += "\n/next 또는 /prev 로 페이지 이동\n취소할 상품 번호/이름을 입력해주세요."
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
# /accept (판매자 전용)
async def accept_transaction(update: Update, context) -> None:
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("사용법: /accept 거래ID 판매자지갑주소\n예: /accept 123456789012 TXXXXXXXXXXXX" + command_guide())
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

        await update.message.reply_text(f"거래 ID {t_id}가 수락되었습니다. (TRC20 USDT)\n구매자에게 입금 안내 메시지를 전송합니다." + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.buyer_id,
                text=(f"거래 ID {t_id}가 수락되었습니다.\n"
                      f"해당 금액({tx.amount} USDT)를 {TRON_WALLET}로 송금하시고, "
                      "송금 시 반드시 거래 ID를 확인용으로 입력해 주세요.\n(메모는 사용하지 않습니다.)")
            )
        except Exception as e:
            logging.error(f"구매자 알림 오류: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/accept 오류: {e}")
        await update.message.reply_text("거래 수락 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# -------------------------------------------------------------------
# /refusal (판매자 전용)
async def refusal_transaction(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refusal 거래ID\n예: /refusal 123456789012" + command_guide())
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
        await update.message.reply_text(f"거래 ID {t_id}가 거절되었습니다. (해당 거래는 파기되었습니다.)" + command_guide())
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
# /checkdeposit (구매자 전용)
# 메모 없이 TXID만으로 입금 확인 (TXID는 한 번만 사용 가능)
async def check_deposit(update: Update, context: CallbackContext) -> None:
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("사용법: /checkdeposit 거래ID txid\n예: /checkdeposit 123456789012 abcdef1234567890" + command_guide())
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
            await update.message.reply_text("입금 내역을 확인할 수 없거나, 금액이 일치하지 않습니다. TXID를 다시 확인해주세요." + command_guide())
            return
        USED_TXIDS.add(txid)
        tx.status = "deposit_confirmed"
        session.commit()
        await update.message.reply_text("입금이 확인되었습니다. 판매자와 구매자에게 안내 메시지를 전송합니다." + command_guide())
        context.bot.send_message(
            chat_id=tx.buyer_id,
            text=("입금이 확인되었습니다.\n판매자께서는 구매자에게 물품을 발송해 주세요.\n"
                  "물품 발송 후, 구매자께서는 /confirm 명령어를 입력하여 최종 거래를 완료해 주세요.")
        )
        context.bot.send_message(
            chat_id=tx.seller_id,
            text=("입금이 확인되었습니다.\n구매자에게 물품을 발송해 주시기 바랍니다.\n"
                  "구매자가 /confirm 명령어를 입력하면 최종 거래가 완료됩니다.")
        )
    except Exception as e:
        session.rollback()
        logging.error(f"/checkdeposit 오류: {e}")
        await update.message.reply_text("입금 확인 처리 중 오류가 발생했습니다." + command_guide())
    finally:
        session.close()

# -------------------------------------------------------------------
# /confirm (구매자 전용)
# 구매자가 물품 수령 후 /confirm 명령어를 입력하면 최종 송금(네트워크 수수료 및 중개 수수료 차감)이 진행됩니다.
async def confirm_payment(update: Update, context) -> None:
    args = update.message.text.split()
    if len(args) < 4:
        await update.message.reply_text("사용법: /confirm 거래ID 구매자지갑주소 txid\n예: /confirm 123456789012 TXXXX abc123" + command_guide())
        return
    t_id = args[1].strip()
    buyer_wallet = args[2].strip()
    txid = args[3].strip()

    session = get_db_session()
    try:
        # /confirm은 상태가 "deposit_confirmed"인 거래에서만 실행됩니다.
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="deposit_confirmed").first()
        if not tx:
            await update.message.reply_text("해당 거래는 입금 확인이 되지 않았거나 아직 물품 발송 전입니다." + command_guide())
            return
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("구매자만 이 명령어를 사용할 수 있습니다." + command_guide())
            return

        # 최종 확인 시 TXID 검증(이미 사용된 TXID는 방지)
        valid, _ = verify_deposit_txid(float(tx.amount), txid)
        if not valid:
            await update.message.reply_text("TXID를 다시 확인해주세요. (입금 내역 미확인)" + command_guide())
            return

        # 네트워크 수수료 NETWORK_FEE를 차감한 후 중개 수수료 적용
        original_amount = float(tx.amount)
        if original_amount <= NETWORK_FEE:
            await update.message.reply_text("송금할 금액이 네트워크 수수료보다 적습니다. 관리자에게 문의하세요." + command_guide())
            return
        net_amount = (original_amount - NETWORK_FEE) * (1 - NORMAL_COMMISSION_RATE)
        tx.status = "completed"
        session.commit()
        await update.message.reply_text(
            f"입금이 최종 확인되었습니다 ({original_amount} USDT). 거래가 완료됩니다.\n판매자에게 {net_amount} USDT 송금 진행 중..."
            + command_guide()
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
                )
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
async def rate_user(update: Update, context) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /rate 거래ID\n예: /rate 123456789012" + command_guide())
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

async def save_rating(update: Update, context) -> int:
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
active_chats = {}  # {거래ID: (buyer_id, seller_id)}

async def start_chat(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /chat 거래ID\n예: /chat 123456789012" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).filter(Transaction.status.in_(["accepted", "deposit_confirmed", "deposit_confirmed_over", "completed"])).first()
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
async def off_transaction(update: Update, context) -> None:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /off 거래ID\n예: /off 123456789012" + command_guide())
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
# /refund (구매자 전용, 오버송금된 경우에만 진행)
async def refund_request(update: Update, context) -> int:
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("사용법: /refund 거래ID\n예: /refund 123456789012" + command_guide())
        return ConversationHandler.END
    t_id = args[1].strip()
    session = get_db_session()
    try:
        # /refund은 상태가 "deposit_confirmed_over"인 거래에서만 진행합니다.
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="deposit_confirmed_over").first()
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

        refund_amount = expected_amount * (1 - (NORMAL_COMMISSION_RATE / 2))
        context.user_data["refund_txid"] = t_id
        context.user_data["refund_amount"] = refund_amount
        await update.message.reply_text(
            f"환불을 진행합니다. 구매자 지갑 주소를 입력해주세요.\n(환불 금액: {refund_amount} USDT, 수수료 2.5% 적용)"
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
        await update.message.reply_text("환불 송금 중 오류가 발생했습니다. 다시 시도해주세요." + command_guide())
        return WAITING_FOR_REFUND_WALLET

# -------------------------------------------------------------------
# /warexit (관리자 전용)
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

# -------------------------------------------------------------------
# /exit
async def exit_to_start(update: Update, context) -> int:
    if "current_chat_tx" in context.user_data:
        await update.message.reply_text("채팅 중에는 /exit 명령어를 사용할 수 없습니다." + command_guide())
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("초기 화면으로 돌아갑니다. /start 명령어로 다시 시작해주세요." + command_guide())
    return ConversationHandler.END

# -------------------------------------------------------------------
# 에러 핸들러
async def error_handler(update: object, context) -> None:
    logging.error("오류 발생", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("오류가 발생했습니다. 다시 시도해주세요." + command_guide())

# -------------------------------------------------------------------
# 대화형 핸들러 설정
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

# -------------------------------------------------------------------
# 메인 실행부
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    # 주요 명령어 등록
    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(CommandHandler("warexit", warexit_command))
    app.add_handler(CommandHandler("chat", start_chat))
    app.add_handler(CommandHandler("exit", exit_to_start))
    app.add_handler(refund_handler)

    # ConversationHandlers
    app.add_handler(sell_handler)
    app.add_handler(cancel_handler)
    app.add_handler(rate_handler)

    # 채팅 메시지 리레이 (파일/사진/텍스트)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay_message))

    app.add_error_handler(error_handler)

    # 자동 입금 확인 작업 (60초마다 실행, 첫 실행은 10초 후)
    app.job_queue.run_repeating(auto_verify_deposits, interval=60, first=10)

    app.run_polling()