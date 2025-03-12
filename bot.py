import logging
import random
import os
import time
import requests
import asyncio
from requests.adapters import HTTPAdapter, Retry

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)
from telegram.constants import ParseMode

# SQLAlchemy 임포트 (String 포함)
from sqlalchemy import create_engine, Column, Integer, String, DECIMAL, BigInteger, Text, TIMESTAMP, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session

from tronpy import Tron
from tronpy.providers import HTTPProvider

# 로깅 설정
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# 환경 변수 설정 (Fly.io 시크릿 등)
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")  # 예: "postgres://user:pass@host:5432/dbname"
TRON_API = os.getenv("TRON_API")            # 예: "https://api.trongrid.io"
TRON_API_KEY = os.getenv("TRON_API_KEY")      # 실제 API Key
TRON_WALLET = os.getenv("TRON_WALLET", "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
TRON_PASSWORD = os.getenv("TRON_PASSWORD")
if not TRON_PASSWORD:
    logging.error("TRON_PASSWORD 환경변수가 설정되어 있지 않습니다. 반드시 설정하세요.")
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "999999999"))

# requests 세션 설정 (재시도/타임아웃)
http_session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
http_adapter = HTTPAdapter(max_retries=retries)
http_session.mount("https://", http_adapter)
http_session.mount("http://", http_adapter)

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

# 데이터베이스 모델
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
    status = Column(String, default='pending')  # pending, accepted, completed, cancelled, rejected
    session_id = Column(Text)  # 판매자 지갑 주소 (또는 환불용 구매자 지갑)
    transaction_id = Column(Text, unique=True)  # 12자리 랜덤 거래 id
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

# Tron 클라이언트 설정 (TRON_API의 끝에 '/' 제거)
TRON_API_CLEAN = TRON_API.rstrip("/")
client = Tron(provider=HTTPProvider(TRON_API_CLEAN, api_key=TRON_API_KEY))

# 중개 수수료
NORMAL_COMMISSION_RATE = 0.05   # 5%
OVERSEND_COMMISSION_RATE = 0.075  # 7.5% (초과 송금 시)

# TronGrid API 유틸리티 함수
def fetch_recent_transactions(address: str, limit: int = 30) -> list:
    url = f"{TRON_API_CLEAN}/v1/accounts/{address}/transactions"
    headers = {"Accept": "application/json"}
    if TRON_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRON_API_KEY
    params = {"limit": limit, "only_confirmed": "true"}
    resp = http_session.get(url, headers=headers, params=params)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", [])

def fetch_transaction_detail(txid: str) -> dict:
    url = f"{TRON_API_CLEAN}/v1/transactions/{txid}"
    headers = {"Accept": "application/json"}
    if TRON_API_KEY:
        headers["TRON-PRO-API-KEY"] = TRON_API_KEY
    resp = http_session.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    arr = data.get("data", [])
    return arr[0] if arr else {}

def parse_trc20_transfer_amount_and_memo(tx_detail: dict) -> (float, str):
    try:
        contracts = tx_detail.get("raw_data", {}).get("contract", [])
        if not contracts:
            return 0, ""
        first_contract = contracts[0].get("parameter", {}).get("value", {})
        transferred_amount = first_contract.get("amount", 0)
        data_hex = first_contract.get("data", "")
        memo = bytes.fromhex(data_hex).decode("utf-8") if data_hex else ""
        actual_amount = transferred_amount / 1e6
        return actual_amount, memo
    except Exception as e:
        logging.error(f"Transaction parsing error: {e}")
        return 0, ""

# 송금 및 검증 로직
def verify_deposit(expected_amount: float, txid: str, internal_txid: str) -> (bool, float):
    try:
        tx_detail = fetch_transaction_detail(txid)
        actual_amount, memo = parse_trc20_transfer_amount_and_memo(tx_detail)
        if abs(actual_amount - expected_amount) > 1e-6:
            return (False, actual_amount)
        if internal_txid.lower() not in memo.lower():
            return (False, actual_amount)
        return (True, actual_amount)
    except Exception as e:
        logging.error(f"Blockchain verification error: {e}")
        return (False, 0)

def check_usdt_payment(expected_amount: float, txid: str = "", internal_txid: str = "") -> (bool, float):
    if txid and internal_txid:
        return verify_deposit(expected_amount, txid, internal_txid)
    try:
        contract = client.get_contract(USDT_CONTRACT)
        balance = contract.functions.balanceOf(TRON_WALLET)
        return (balance / 1e6) >= expected_amount, balance / 1e6
    except Exception as e:
        logging.error(f"USDT balance check error: {e}")
        return (False, 0)

def send_usdt(to_address: str, amount: float, memo: str = "") -> dict:
    if not TRON_PASSWORD:
        raise Exception("TRON_PASSWORD not set.")
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
        logging.error(f"TRC20 transfer error: {e}")
        raise

# 자동 송금 확인 (TronGrid 기반)
def auto_verify_transaction(tx: Transaction) -> (bool, float):
    try:
        recent_txs = fetch_recent_transactions(TRON_WALLET, limit=30)
        for tx_summary in recent_txs:
            txid = tx_summary.get("txID", "")
            if not txid:
                continue
            detail = fetch_transaction_detail(txid)
            actual_amount, memo = parse_trc20_transfer_amount_and_memo(detail)
            if tx.transaction_id.lower() in memo.lower():
                if actual_amount >= float(tx.amount):
                    return True, actual_amount
        return False, 0
    except Exception as e:
        logging.error(f"Auto verification error: {e}")
        return False, 0

async def auto_verify_deposits(context):
    session = get_db_session()
    try:
        pending_txs = session.query(Transaction).filter(Transaction.status == "accepted").all()
        for tx in pending_txs:
            valid, deposited_amount = auto_verify_transaction(tx)
            if valid:
                original_amount = float(tx.amount)
                if deposited_amount > original_amount:
                    net_amount = deposited_amount * (1 - OVERSEND_COMMISSION_RATE)
                    msg_buyer = (f"[Auto] Transaction {tx.transaction_id} deposit confirmed.\n"
                                 f"Deposited: {deposited_amount} USDT (over-sent)\nSeller, please ship goods after receiving {net_amount} USDT.")
                    msg_seller = (f"[Auto] Transaction {tx.transaction_id} deposit confirmed.\n"
                                  f"After 7.5% fee, {net_amount} USDT will be sent to your wallet.\nPlease ship the product.")
                    tx.status = "completed"
                    session.commit()
                    try:
                        await context.bot.send_message(chat_id=tx.buyer_id, text=msg_buyer)
                        await context.bot.send_message(chat_id=tx.seller_id, text=msg_seller)
                    except Exception as e:
                        logging.error(f"Auto verification notification error: {e}")
                else:
                    net_amount = original_amount * (1 - NORMAL_COMMISSION_RATE)
                    msg_buyer = (f"[Auto] Transaction {tx.transaction_id} deposit confirmed.\nExact deposit: {original_amount} USDT received.\nTransaction completed.")
                    msg_seller = (f"[Auto] Transaction {tx.transaction_id} deposit confirmed.\n{net_amount} USDT will be sent to your wallet.\nPlease ship the product.")
                    tx.status = "completed"
                    session.commit()
                    try:
                        await context.bot.send_message(chat_id=tx.buyer_id, text=msg_buyer)
                        await context.bot.send_message(chat_id=tx.seller_id, text=msg_seller)
                    except Exception as e:
                        logging.error(f"Auto verification notification error: {e}")
    except Exception as e:
        logging.error(f"Auto verification job error: {e}")
    finally:
        session.close()

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

def command_guide() -> str:
    return (
        "\n\n[Main Menu]\n"
        "Use the buttons below to select a function.\n"
        "Type /menu to return to the main menu.\n"
        "\n※ Traditional command functions are also supported."
    )

def reset_conversation(context):
    context.user_data.clear()

def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("Sell", callback_data="menu_sell"),
         InlineKeyboardButton("List Items", callback_data="menu_list")],
        [InlineKeyboardButton("Search", callback_data="menu_search"),
         InlineKeyboardButton("Offer", callback_data="menu_offer")],
        [InlineKeyboardButton("Manage", callback_data="menu_manage"),
         InlineKeyboardButton("Chat", callback_data="menu_chat")],
        [InlineKeyboardButton("Admin", callback_data="menu_admin"),
         InlineKeyboardButton("Exit", callback_data="menu_exit")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context) -> None:
    if update.message:
        await update.message.reply_text("Select an option from the main menu.", reply_markup=get_main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text("Select an option from the main menu.", reply_markup=get_main_menu_keyboard())

async def main_menu_callback(update: Update, context) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    reset_conversation(context)
    if data == "menu_sell":
        await query.edit_message_text("Sell selected. Please enter the product name.")
        context.user_data["next_func"] = sell_step_2
    elif data == "menu_list":
        await list_items_command(update, context)
    elif data == "menu_search":
        await query.edit_message_text("Enter a search term.")
        context.user_data["next_func"] = list_search_results
    elif data == "menu_offer":
        await query.edit_message_text("Enter the product number or name for the offer.")
        context.user_data["next_func"] = offer_item
    elif data == "menu_manage":
        keyboard = [
            [InlineKeyboardButton("Accept", callback_data="manage_accept"),
             InlineKeyboardButton("Refuse", callback_data="manage_refusal")],
            [InlineKeyboardButton("Confirm", callback_data="manage_confirm"),
             InlineKeyboardButton("Refund", callback_data="manage_refund")],
            [InlineKeyboardButton("Rate", callback_data="manage_rate"),
             InlineKeyboardButton("Cancel", callback_data="manage_off")]
        ]
        await query.edit_message_text("Select a management function.", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "menu_chat":
        await query.edit_message_text("Enter the transaction ID for chat.")
        context.user_data["next_func"] = start_chat
    elif data == "menu_admin":
        keyboard = [
            [InlineKeyboardButton("Force Exit", callback_data="admin_warexit")]
        ]
        await query.edit_message_text("Select an admin function.", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "menu_exit":
        await query.edit_message_text("Exiting. Goodbye!")
    elif data == "manage_accept":
        await query.edit_message_text("Accept selected.\nEnter transaction ID and seller wallet address (space separated).")
        context.user_data["next_func"] = accept_transaction
    elif data == "manage_refusal":
        await query.edit_message_text("Refusal selected.\nEnter transaction ID.")
        context.user_data["next_func"] = refusal_transaction
    elif data == "manage_confirm":
        await query.edit_message_text("Confirm selected.\nEnter transaction ID, buyer wallet address, and txID (space separated).")
        context.user_data["next_func"] = confirm_payment
    elif data == "manage_refund":
        await query.edit_message_text("Refund selected.\nEnter transaction ID.")
        context.user_data["next_func"] = refund_request
    elif data == "manage_rate":
        await query.edit_message_text("Rate selected.\nEnter transaction ID.")
        context.user_data["next_func"] = rate_user
    elif data == "manage_off":
        await query.edit_message_text("Cancel selected.\nEnter transaction ID.")
        context.user_data["next_func"] = off_transaction
    elif data == "admin_warexit":
        await query.edit_message_text("Force exit selected.\nEnter transaction ID.")
        context.user_data["next_func"] = warexit_command

# /sell 대화 흐름 (버튼 인터페이스용)
async def sell_step_2(update: Update, context) -> int:
    context.user_data["sell_name"] = update.message.text.strip()
    await update.message.reply_text("Enter product price (USDT).")
    return "sell_price"

async def sell_step_3(update: Update, context) -> int:
    try:
        context.user_data["sell_price"] = float(update.message.text.strip())
        keyboard = [
            [InlineKeyboardButton("Digital", callback_data="sell_type_digital"),
             InlineKeyboardButton("Physical", callback_data="sell_type_physical")]
        ]
        await update.message.reply_text("Select product type.", reply_markup=InlineKeyboardMarkup(keyboard))
    except ValueError:
        await update.message.reply_text("Enter a valid price.")
        return "sell_price"
    return "sell_type"

async def sell_finish(update: Update, context) -> None:
    query = update.callback_query
    item_type = "Digital" if query.data == "sell_type_digital" else "Physical"
    context.user_data["sell_type"] = item_type

    session = get_db_session()
    new_item = Item(
        name=context.user_data["sell_name"],
        price=context.user_data["sell_price"],
        seller_id=query.from_user.id,
        type=item_type
    )
    session.add(new_item)
    session.commit()
    session.close()

    await query.answer()
    await query.edit_message_text(
        f"Product registered.\nName: {context.user_data['sell_name']}\nPrice: {context.user_data['sell_price']} USDT\nType: {item_type}"
    )
    await query.message.reply_text("Returning to main menu.", reply_markup=get_main_menu_keyboard())

# 텍스트 입력 처리 (버튼 메뉴 후 입력)
async def text_input_handler(update: Update, context) -> None:
    if "next_func" in context.user_data:
        next_func = context.user_data["next_func"]
        await next_func(update, context)
    else:
        await update.message.reply_text("Input not processed. Type /menu to return to the main menu.", reply_markup=get_main_menu_keyboard())

# /list, /next, /prev (기존 명령어)
async def list_items_command(update: Update, context) -> None:
    session = get_db_session()
    try:
        page = context.user_data.get("list_page", 1)
        items = session.query(Item).filter(Item.status == "available").all()
        if not items:
            await update.message.reply_text("No items available." + command_guide())
            return
        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        page = total_pages if page < 1 else (1 if page > total_pages else page)
        context.user_data["list_page"] = page
        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        context.user_data["list_mapping"] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}
        msg = f"Available items (Page {page}/{total_pages}):\n"
        for idx, item in enumerate(page_items, start=1):
            msg += f"{idx}. {item.name} - {item.price} USDT ({item.type})\n"
        msg += "\nUse /next, /prev to navigate.\nUse /offer [number or name] to request a transaction."
        await update.message.reply_text(msg + command_guide())
    except Exception as e:
        logging.error(f"/list error: {e}")
        await update.message.reply_text("Error fetching items." + command_guide())
    finally:
        session.close()

async def next_page(update: Update, context) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) + 1
    await list_items_command(update, context)

async def prev_page(update: Update, context) -> None:
    context.user_data["list_page"] = context.user_data.get("list_page", 1) - 1
    await list_items_command(update, context)

# /search
async def search_items_command(update: Update, context) -> None:
    reset_conversation(context)
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Enter a search term. E.g., /search Mouse" + command_guide())
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
            await update.message.reply_text(f"No items found for '{query}'." + command_guide())
            return
        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        page = total_pages if page < 1 else (1 if page > total_pages else page)
        context.user_data["search_page"] = page
        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        context.user_data["search_mapping"] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}
        msg = f"Search results for '{query}' (Page {page}/{total_pages}):\n"
        for idx, item in enumerate(page_items, start=1):
            msg += f"{idx}. {item.name} - {item.price} USDT ({item.type})\n"
        msg += "\nUse /next, /prev to navigate.\nUse /offer [number or name] to request a transaction."
        await update.message.reply_text(msg + command_guide())
    except Exception as e:
        logging.error(f"/search error: {e}")
        await update.message.reply_text("Error during search." + command_guide())
    finally:
        session.close()

# /offer
def generate_transaction_id() -> str:
    return ''.join(str(random.randint(0, 9)) for _ in range(12))

async def offer_item(update: Update, context) -> None:
    reset_conversation(context)
    session = get_db_session()
    try:
        args = update.message.text.split(maxsplit=1)
        if len(args) < 2:
            await update.message.reply_text("Usage: /offer [number or product name]" + command_guide())
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
            await update.message.reply_text("Invalid product number/name." + command_guide())
            return
        buyer_id = update.message.from_user.id
        seller_id = item.seller_id
        t_id = generate_transaction_id()
        new_tx = Transaction(item_id=item.id, buyer_id=buyer_id, seller_id=seller_id,
                             amount=item.price, transaction_id=t_id)
        session.add(new_tx)
        session.commit()
        await update.message.reply_text(f"Transaction for '{item.name}' created.\nTransaction ID: {t_id}\nInclude this ID in your transfer memo!" + command_guide())
        try:
            await context.bot.send_message(
                chat_id=seller_id,
                text=(f"Transaction request for your product '{item.name}' has arrived.\nTransaction ID: {t_id}\n"
                      "Seller, accept with /accept [transactionID] [your wallet address] or refuse with /refusal [transactionID].\n"
                      "Network: TRC20 USDT")
            )
        except Exception as e:
            logging.error(f"Seller notification error: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/offer error: {e}")
        await update.message.reply_text("Error creating transaction." + command_guide())
    finally:
        session.close()

# /cancel
async def cancel(update: Update, context) -> int:
    reset_conversation(context)
    session = get_db_session()
    try:
        seller_id = update.message.from_user.id
        items = session.query(Item).filter(Item.seller_id == seller_id, Item.status == "available").all()
        if not items:
            await update.message.reply_text("No cancellable items available (only items not yet funded)." + command_guide())
            return ConversationHandler.END
        page = context.user_data.get("cancel_page", 1)
        total_pages = (len(items) - 1) // ITEMS_PER_PAGE + 1
        page = total_pages if page < 1 else (1 if page > total_pages else page)
        context.user_data["cancel_page"] = page
        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        page_items = items[start:end]
        context.user_data["cancel_mapping"] = {str(idx): item.id for idx, item in enumerate(page_items, start=1)}
        msg = f"Cancellable items (Page {page}/{total_pages}):\n"
        for idx, it in enumerate(page_items, start=1):
            msg += f"{idx}. {it.name} - {it.price} USDT ({it.type})\n"
        msg += "\nUse /next, /prev to navigate.\nEnter the number or name to cancel the item."
        await update.message.reply_text(msg + command_guide())
        return WAITING_FOR_CANCEL_ID
    except Exception as e:
        logging.error(f"/cancel error: {e}")
        await update.message.reply_text("Error fetching cancellable items." + command_guide())
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
            await update.message.reply_text("Invalid or non-cancellable item." + command_guide())
            return WAITING_FOR_CANCEL_ID
        session.delete(item)
        session.commit()
        await update.message.reply_text(f"'{item.name}' has been cancelled." + command_guide())
        return ConversationHandler.END
    except Exception as e:
        session.rollback()
        logging.error(f"/cancel processing error: {e}")
        await update.message.reply_text("Error cancelling item." + command_guide())
        return WAITING_FOR_CANCEL_ID
    finally:
        session.close()

# /accept
async def accept_transaction(update: Update, context) -> None:
    reset_conversation(context)
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("Usage: /accept [transactionID] [sellerWalletAddress]" + command_guide())
        return
    t_id = args[1].strip()
    seller_wallet = args[2].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="pending").first()
        if not tx:
            await update.message.reply_text("Invalid transaction ID." + command_guide())
            return
        if update.message.from_user.id != tx.seller_id:
            await update.message.reply_text("Only the seller can accept this transaction." + command_guide())
            return
        tx.session_id = seller_wallet
        tx.status = "accepted"
        session.commit()
        await update.message.reply_text(f"Transaction {t_id} accepted.\nNetwork: TRC20 USDT\nNotifying buyer..." + command_guide())
        try:
            await context.bot.send_message(
                chat_id=tx.buyer_id,
                text=(f"Transaction {t_id} has been accepted.\n"
                      f"Please send the required {tx.amount} USDT to {TRON_WALLET}.\n"
                      "Include the transaction ID in the memo!")
            )
        except Exception as e:
            logging.error(f"Buyer notification error: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/accept error: {e}")
        await update.message.reply_text("Error accepting transaction." + command_guide())
    finally:
        session.close()

# /refusal
async def refusal_transaction(update: Update, context) -> None:
    reset_conversation(context)
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /refusal [transactionID]" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="pending").first()
        if not tx:
            await update.message.reply_text("Invalid transaction ID." + command_guide())
            return
        if update.message.from_user.id != tx.seller_id:
            await update.message.reply_text("Only the seller can refuse this transaction." + command_guide())
            return
        session.delete(tx)
        session.commit()
        await update.message.reply_text(f"Transaction {t_id} has been refused." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/refusal error: {e}")
        await update.message.reply_text("Error refusing transaction." + command_guide())
    finally:
        session.close()

# /confirm – 구매자 거래 완료 확인 및 송금 실행/검증
async def confirm_payment(update: Update, context) -> None:
    reset_conversation(context)
    args = update.message.text.split()
    if len(args) < 4:
        await update.message.reply_text("Usage: /confirm [transactionID] [buyerWalletAddress] [txID]\nExample: /confirm 123456789012 TYYYYYYYYYYYY abcdef1234567890" + command_guide())
        return
    t_id = args[1].strip()
    buyer_wallet = args[2].strip()
    txid = args[3].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="accepted").first()
        if not tx:
            await update.message.reply_text("Invalid or unaccepted transaction." + command_guide())
            return
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("Only the buyer can confirm the transaction." + command_guide())
            return
        original_amount = float(tx.amount)
        valid, deposited_amount = check_usdt_payment(original_amount, txid, t_id)
        if not valid:
            if deposited_amount == 0:
                await update.message.reply_text("Unable to verify blockchain transaction or memo missing.\nCheck your transfer record." + command_guide())
                return
            if deposited_amount < original_amount:
                refund_result = send_usdt(buyer_wallet, deposited_amount)
                await update.message.reply_text(
                    f"Insufficient deposit: received {deposited_amount} USDT (required: {original_amount} USDT).\nFull refund processed.\nRefund result: {refund_result}\nPlease resend the exact amount." + command_guide()
                )
                return
            else:
                net_amount = deposited_amount * (1 - OVERSEND_COMMISSION_RATE)
                tx.status = "completed"
                session.commit()
                await update.message.reply_text(
                    f"Over-sent deposit detected: received {deposited_amount} USDT.\nAfter 7.5% fee, {net_amount} USDT will be sent to the seller." + command_guide()
                )
                try:
                    seller_wallet = tx.session_id
                    result = send_usdt(seller_wallet, net_amount, memo=t_id)
                    await context.bot.send_message(
                        chat_id=tx.seller_id,
                        text=(f"Transaction {t_id} completed with over-sent amount.\n"
                              f"After 7.5% fee, {net_amount} USDT sent to your wallet ({seller_wallet}).\n"
                              "Notify buyer to resend the excess amount.")
                    )
                except Exception as e:
                    logging.error(f"Seller over-send error: {e}")
                return
        tx.status = "completed"
        session.commit()
        net_amount = original_amount * (1 - NORMAL_COMMISSION_RATE)
        await update.message.reply_text(
            f"Exact deposit confirmed ({original_amount} USDT). Completing transaction.\nSeller will receive {net_amount} USDT." + command_guide()
        )
        try:
            seller_wallet = tx.session_id
            result = send_usdt(seller_wallet, net_amount, memo=t_id)
            await context.bot.send_message(
                chat_id=tx.seller_id,
                text=(f"Transaction {t_id} completed.\n"
                      f"{net_amount} USDT sent to your wallet ({seller_wallet}).\n"
                      "Buyer, please dispatch your product immediately!")
            )
        except Exception as e:
            logging.error(f"Seller normal send error: {e}")
    except Exception as e:
        session.rollback()
        logging.error(f"/confirm error: {e}")
        await update.message.reply_text("Error confirming transaction." + command_guide())
    finally:
        session.close()

# /rate
async def rate_user(update: Update, context) -> int:
    reset_conversation(context)
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /rate [transactionID]" + command_guide())
        return WAITING_FOR_RATING
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("Transaction not completed or invalid." + command_guide())
            return WAITING_FOR_RATING
        context.user_data["rating_txid"] = t_id
        await update.message.reply_text("Enter a rating (1-5):" + command_guide())
        return WAITING_FOR_CONFIRMATION
    except Exception as e:
        logging.error(f"/rate error: {e}")
        return WAITING_FOR_RATING
    finally:
        session.close()

async def save_rating(update: Update, context) -> int:
    session = get_db_session()
    try:
        score = int(update.message.text.strip())
        if not (1 <= score <= 5):
            await update.message.reply_text("Rating must be between 1 and 5." + command_guide())
            return WAITING_FOR_CONFIRMATION
        t_id = context.user_data.get("rating_txid")
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="completed").first()
        if not tx:
            await update.message.reply_text("Invalid transaction." + command_guide())
            return ConversationHandler.END
        target_id = tx.seller_id if update.message.from_user.id == tx.buyer_id else tx.buyer_id
        new_rating = Rating(user_id=target_id, score=score, review="Anonymous")
        session.add(new_rating)
        session.commit()
        await update.message.reply_text(f"Rating {score} recorded." + command_guide())
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Enter a valid number." + command_guide())
        return WAITING_FOR_CONFIRMATION
    except Exception as e:
        session.rollback()
        logging.error(f"/rate processing error: {e}")
        return WAITING_FOR_CONFIRMATION
    finally:
        session.close()

# /chat
async def start_chat(update: Update, context) -> None:
    reset_conversation(context)
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /chat [transactionID]" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter(Transaction.transaction_id == t_id, Transaction.status.in_(["accepted", "completed"])).first()
        if not tx:
            await update.message.reply_text("Invalid or unaccepted transaction." + command_guide())
            return
        user_id = update.message.from_user.id
        if user_id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("You are not a party to this transaction." + command_guide())
            return
        active_chats[t_id] = (tx.buyer_id, tx.seller_id)
        context.user_data["current_chat_tx"] = t_id
        await update.message.reply_text(f"Starting anonymous chat for transaction {t_id}.\nText or file (photo/document) will be relayed to the other party." + command_guide())
    except Exception as e:
        logging.error(f"/chat error: {e}")
        await update.message.reply_text("Error starting chat." + command_guide())
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
            await context.bot.send_document(chat_id=partner, document=file_id, caption=f"[File] {file_name}")
        elif update.message.photo:
            photo = update.message.photo[-1]
            await context.bot.send_photo(chat_id=partner, photo=photo.file_id, caption="[Photo]")
        else:
            await context.bot.send_message(chat_id=partner, text=f"[Chat] {update.message.text}")
    except Exception as e:
        logging.error(f"Chat relay error: {e}")

# /off
async def off_transaction(update: Update, context) -> None:
    reset_conversation(context)
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /off [transactionID]" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).first()
        if not tx:
            await update.message.reply_text("Invalid transaction ID." + command_guide())
            return
        if tx.status not in ["pending", "accepted"]:
            await update.message.reply_text("Cannot cancel a transaction that is already in progress or completed." + command_guide())
            return
        if update.message.from_user.id not in [tx.buyer_id, tx.seller_id]:
            await update.message.reply_text("You are not a party to this transaction." + command_guide())
            return
        tx.status = "cancelled"
        session.commit()
        if t_id in active_chats:
            active_chats.pop(t_id)
        await update.message.reply_text(f"Transaction {t_id} has been cancelled." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/off error: {e}")
        await update.message.reply_text("Error cancelling transaction." + command_guide())
    finally:
        session.close()

# /refund
async def refund_request(update: Update, context) -> int:
    reset_conversation(context)
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /refund [transactionID]\nExample: /refund 123456789012" + command_guide())
        return ConversationHandler.END
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id, status="accepted").first()
        if not tx:
            await update.message.reply_text("Invalid transaction ID or refund not possible." + command_guide())
            return ConversationHandler.END
        if update.message.from_user.id != tx.buyer_id:
            await update.message.reply_text("Only the buyer can request a refund." + command_guide())
            return ConversationHandler.END
        expected_amount = float(tx.amount)
        valid, deposited_amount = check_usdt_payment(expected_amount, "", t_id)
        if not valid:
            await update.message.reply_text("Deposit not verified or transaction data is incorrect." + command_guide())
            return ConversationHandler.END
        # 환불 시, 중개 수수료 2.5%만 차감 (즉, 구매자에게 돌려줄 금액)
        refund_amount = expected_amount * (1 - (NORMAL_COMMISSION_RATE / 2))
        context.user_data["refund_txid"] = t_id
        context.user_data["refund_amount"] = refund_amount
        await update.message.reply_text(f"Processing refund. Please enter your wallet address.\n(Refund Amount: {refund_amount} USDT, fee: 2.5% applied)" + command_guide())
        return WAITING_FOR_REFUND_WALLET
    except Exception as e:
        logging.error(f"/refund error: {e}")
        await update.message.reply_text("Error during refund request." + command_guide())
        return ConversationHandler.END
    finally:
        session.close()

async def process_refund(update: Update, context) -> int:
    buyer_wallet = update.message.text.strip()
    t_id = context.user_data.get("refund_txid")
    refund_amount = context.user_data.get("refund_amount")
    try:
        result = send_usdt(buyer_wallet, refund_amount, memo=t_id)
        await update.message.reply_text(f"Refund processed: {refund_amount} USDT sent to {buyer_wallet}.\nTransaction ID: {t_id}\nResult: {result}" + command_guide())
        return ConversationHandler.END
    except Exception as e:
        logging.error(f"Refund transfer error: {e}")
        await update.message.reply_text("Error processing refund. Please re-enter your wallet address." + command_guide())
        return WAITING_FOR_REFUND_WALLET

# /warexit (Admin only)
async def warexit_command(update: Update, context) -> None:
    if update.message.from_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("Admin only." + command_guide())
        return
    reset_conversation(context)
    args = update.message.text.split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: /warexit [transactionID]" + command_guide())
        return
    t_id = args[1].strip()
    session = get_db_session()
    try:
        tx = session.query(Transaction).filter_by(transaction_id=t_id).first()
        if not tx:
            await update.message.reply_text("Invalid transaction ID." + command_guide())
            return
        tx.status = "cancelled"
        session.commit()
        if t_id in active_chats:
            active_chats.pop(t_id)
        await update.message.reply_text(f"[Admin] Transaction {t_id} forcefully terminated." + command_guide())
    except Exception as e:
        session.rollback()
        logging.error(f"/warexit error: {e}")
        await update.message.reply_text("Error during force termination." + command_guide())
    finally:
        session.close()

# /exit
async def exit_to_start(update: Update, context) -> int:
    if "current_chat_tx" in context.user_data:
        await update.message.reply_text("Cannot exit while in chat." + command_guide())
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Returning to start. Use /start to begin." + command_guide())
    return ConversationHandler.END

# 에러 핸들러
async def error_handler(update: object, context) -> None:
    logging.error("Error occurred", exc_info=context.error)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text("Error occurred. Please try again.", reply_markup=get_main_menu_keyboard())

# 대화형 핸들러 설정 (버튼 기반 + 명령어 기반)
sell_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(sell_step_2, pattern="^menu_sell$")],
    states={
        "sell_name": [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_step_2)],
        "sell_price": [MessageHandler(filters.TEXT & ~filters.COMMAND, sell_step_3)],
        "sell_type": [CallbackQueryHandler(sell_finish, pattern="^sell_type_")]
    },
    fallbacks=[CommandHandler("exit", lambda update, context: update.message.reply_text("Cancelled.", reply_markup=get_main_menu_keyboard()))],
)

cancel_handler = ConversationHandler(
    entry_points=[CommandHandler("cancel", cancel)],
    states={
        WAITING_FOR_CANCEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, cancel_item)]
    },
    fallbacks=[CommandHandler("exit", lambda update, context: update.message.reply_text("Cancelled.", reply_markup=get_main_menu_keyboard()))],
)

rate_handler = ConversationHandler(
    entry_points=[CommandHandler("rate", rate_user)],
    states={
        WAITING_FOR_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_rating)]
    },
    fallbacks=[CommandHandler("exit", lambda update, context: update.message.reply_text("Cancelled.", reply_markup=get_main_menu_keyboard()))],
)

refund_handler = ConversationHandler(
    entry_points=[CommandHandler("refund", refund_request)],
    states={
        WAITING_FOR_REFUND_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_refund)]
    },
    fallbacks=[CommandHandler("exit", lambda update, context: update.message.reply_text("Cancelled.", reply_markup=get_main_menu_keyboard()))],
)

# 앱 초기화 및 핸들러 등록
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    # 메인 메뉴 관련 핸들러
    app.add_handler(CommandHandler("start", show_main_menu))
    app.add_handler(CommandHandler("menu", show_main_menu))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^menu_"))

    # 버튼 기반 대화 흐름 핸들러
    app.add_handler(sell_handler)

    # 기존 명령어 기반 핸들러 (버튼 메뉴와 함께 사용)
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

    # 대화형 핸들러 등록 (버튼 메뉴 후 텍스트 입력 처리)
    app.add_handler(cancel_handler)
    app.add_handler(rate_handler)

    # 텍스트 입력 핸들러 (버튼 메뉴 후 필요한 입력 처리)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    # 파일 및 텍스트 메시지 중계 (채팅)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, relay_message))

    app.add_error_handler(error_handler)

    # 자동 송금 확인 작업 (60초마다 실행, 첫 실행은 10초 후)
    app.job_queue.run_repeating(auto_verify_deposits, interval=60, first=10)

    app.run_polling()