import os
import logging
from decimal import Decimal, InvalidOperation
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
from tronpy import Tron
from tronpy.keys import PrivateKey
from decimal import Decimal, InvalidOperation
from tronpy.exceptions import TransactionError

# 환경 변수 설정
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# TronLink 지갑 설정
TRON_NETWORK = "TRON (TRC20)"
TRONLINK_ADDRESS = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB"
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
ESCROW_FEE_PERCENTAGE = Decimal('0.05')  # 5% 중개 수수료

# 트론(Tron) 클라이언트 초기화
client = Tron()

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
            seller_id BIGINT,
            status TEXT,
            item_type TEXT,
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

        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            item_id INTEGER REFERENCES items(id),
            buyer_id BIGINT,
            seller_id BIGINT,
            amount DECIMAL,
            status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''))

        conn.execute(text('''
        CREATE TABLE IF NOT EXISTS ratings (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            rating INTEGER,
            review TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        '''))

        conn.commit()
        logging.info("데이터베이스 테이블 초기화 완료")
    except SQLAlchemyError as e:
        logging.error(f"Database Initialization Error: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "안녕하세요! 에스크로 거래 봇입니다.\n"
        "판매할 물품은 /sell, 구매할 물품은 /list를 입력해주세요.\n"
        "언제든지 /exit 명령어로 초기 화면으로 돌아올 수 있습니다."
    )

async def exit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()  # 대화 상태 초기화
    await start(update, context)

# /sell 명령어 (판매 물품 등록 시작)
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("판매할 물품의 이름을 입력해주세요.")
    return WAITING_FOR_ITEM_NAME

# 물품 이름 입력
async def set_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['item_name'] = update.message.text
    await update.message.reply_text(f"'{update.message.text}'의 가격을 트론(USDT)으로 입력해주세요.")
    return WAITING_FOR_ITEM_PRICE

# 물품 가격 입력
async def set_item_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = Decimal(update.message.text.strip())
        context.user_data['item_price'] = price
        await update.message.reply_text(
            "물품 유형을 선택해주세요.\n"
            "디지털 물품은 /digital, 현물(실물) 물품은 /physical 을 입력하세요."
        )
        return WAITING_FOR_ITEM_TYPE
    except InvalidOperation:
        await update.message.reply_text("유효한 가격을 입력해주세요. 숫자로만 입력해 주세요.")
        return WAITING_FOR_ITEM_PRICE

# 물품 유형 설정
async def set_item_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item_type = update.message.text.lower()
    if item_type not in ['digital', 'physical']:
        await update.message.reply_text("유효한 물품 유형을 입력해주세요. /digital 또는 /physical 을 입력하세요.")
        return WAITING_FOR_ITEM_TYPE

    context.user_data['item_type'] = item_type
    item_name = context.user_data['item_name']
    price = context.user_data['item_price']
    seller_id = update.message.from_user.id

    conn.execute(text('INSERT INTO items (name, price, seller_id, status, item_type) VALUES (:name, :price, :seller_id, :status, :item_type)'),
                 {'name': item_name, 'price': price, 'seller_id': seller_id, 'status': 'available', 'item_type': item_type})
    conn.commit()

    await update.message.reply_text(f"'{item_name}'을(를) {price} USDT에 판매 등록하였습니다.")
    return ConversationHandler.END
ITEMS_PER_PAGE = 10  # 한 페이지에 표시할 물품 수

# /list 명령어 (판매 물품 목록 표시)
async def list_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    page = context.user_data.get('page', 1)
    offset = (page - 1) * ITEMS_PER_PAGE

    items = conn.execute(
        text('SELECT id, name, price, item_type FROM items WHERE status=:status LIMIT :limit OFFSET :offset'),
        {'status': 'available', 'limit': ITEMS_PER_PAGE, 'offset': offset}
    ).fetchall()

    if not items:
        await update.message.reply_text("판매 중인 물품이 없습니다.")
        return

    message = f"판매 중인 물품 목록 (페이지 {page}):\n"
    for item in items:
        message += f"{item.id}. {item.name} - {item.price} USDT ({item.item_type})\n"

    keyboard = []
    if page > 1:
        keyboard.append(InlineKeyboardButton("이전 페이지", callback_data="prev_page"))
    if len(items) == ITEMS_PER_PAGE:
        keyboard.append(InlineKeyboardButton("다음 페이지", callback_data="next_page"))

    if keyboard:
        reply_markup = InlineKeyboardMarkup([keyboard])
        await update.message.reply_text(message, reply_markup=reply_markup)
    else:
        await update.message.reply_text(message)

# 페이지네이션 콜백 처리
async def handle_page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    page = context.user_data.get('page', 1)
    if query.data == "next_page":
        page += 1
    elif query.data == "prev_page" and page > 1:
        page -= 1

    context.user_data['page'] = page
    await list_items(update, context)

# 구매자가 물품을 선택했을 때 (오퍼 전송)
async def select_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        item_id = int(update.message.text.strip())
        item = conn.execute(
            text('SELECT id, name, price, seller_id FROM items WHERE id=:id AND status=:status'),
            {'id': item_id, 'status': 'available'}
        ).fetchone()

        if not item:
            await update.message.reply_text("해당 물품을 찾을 수 없습니다.")
            return

        buyer_id = update.message.from_user.id
        conn.execute(
            text('INSERT INTO offers (item_id, buyer_id, status) VALUES (:item_id, :buyer_id, :status)'),
            {'item_id': item.id, 'buyer_id': buyer_id, 'status': 'pending'}
        )
        conn.commit()

        await update.message.reply_text(f"'{item.name}'에 대한 오퍼를 보냈습니다.")

        # 판매자에게 알림 전송
        await context.bot.send_message(
            chat_id=item.seller_id,
            text=f"'{item.name}' 물품에 대해 구매자가 오퍼를 보냈습니다.\n"
                 "오퍼를 수락하려면 /accept, 거절하려면 /reject를 입력해주세요."
        )

    except ValueError:
        await update.message.reply_text("유효한 물품 ID를 입력해주세요.")
# /accept 명령어 (오퍼 수락)
async def accept_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    seller_id = update.message.from_user.id
    offers = conn.execute(
        text('SELECT offers.id, items.name, items.price, items.item_type, offers.buyer_id '
             'FROM offers JOIN items ON offers.item_id = items.id '
             'WHERE items.seller_id=:seller_id AND offers.status=:status'),
        {'seller_id': seller_id, 'status': 'pending'}
    ).fetchall()

    if not offers:
        await update.message.reply_text("수락할 오퍼가 없습니다.")
        return

    offer = offers[0]  # 첫 번째 대기중인 오퍼를 자동으로 선택

    conn.execute(
        text('UPDATE offers SET status=:status WHERE id=:id'),
        {'status': 'accepted', 'id': offer.id}
    )
    conn.commit()

    await update.message.reply_text(f"'{offer.name}'에 대한 오퍼를 수락했습니다.")

    # 구매자에게 결제 정보 전송
    await context.bot.send_message(
        chat_id=offer.buyer_id,
        text=f"판매자가 '{offer.name}' 물품에 대한 오퍼를 수락했습니다.\n"
             f"테더(USDT, TRC20)를 아래 주소로 송금해주세요.\n"
             f"지갑 주소: {TRONLINK_ADDRESS}\n"
             f"결제 금액: {offer.price} USDT\n"
             f"네트워크: {TRON_NETWORK}"
    )

# /reject 명령어 (오퍼 거절)
async def reject_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    seller_id = update.message.from_user.id
    offers = conn.execute(
        text('SELECT offers.id, items.name, offers.buyer_id '
             'FROM offers JOIN items ON offers.item_id = items.id '
             'WHERE items.seller_id=:seller_id AND offers.status=:status'),
        {'seller_id': seller_id, 'status': 'pending'}
    ).fetchall()

    if not offers:
        await update.message.reply_text("거절할 오퍼가 없습니다.")
        return

    offer = offers[0]

    conn.execute(
        text('UPDATE offers SET status=:status WHERE id=:id'),
        {'status': 'rejected', 'id': offer.id}
    )
    conn.commit()

    await update.message.reply_text(f"'{offer.name}'에 대한 오퍼를 거절했습니다.")

    # 구매자에게 오퍼 거절 알림
    await context.bot.send_message(
        chat_id=offer.buyer_id,
        text=f"판매자가 '{offer.name}' 물품에 대한 오퍼를 거절했습니다."
    )

# 입금 확인 함수
async def check_usdt_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE, offer_id: int, expected_amount: Decimal) -> None:
    buyer_id = update.message.from_user.id
    
    # TronLink 지갑 잔액 확인
    contract = client.get_contract("TEb1C6YoLKYMzvAN7Dmx9AhLzY1oG2MAj9")  # TRC20 USDT 계약 주소
    current_balance = Decimal(contract.functions.balanceOf(TRONLINK_ADDRESS) / (10 ** 6))
    
    logging.info(f"현재 TronLink 지갑 잔액: {current_balance} USDT")
    
    if current_balance < expected_amount:
        shortfall = expected_amount - current_balance
        await update.message.reply_text(
            f"입금 금액이 부족합니다. 추가로 {shortfall} USDT를 송금해주세요."
        )
        return
    
    excess_amount = current_balance - expected_amount
    if excess_amount > 0:
        await update.message.reply_text(
            f"입금 금액이 예상보다 {excess_amount} USDT 많습니다. "
            f"추가 금액은 거래 완료 후 반환됩니다."
        )
    
    # 구매자와 판매자에게 입금 확인 알림
    offer = conn.execute(
        text('SELECT item_id, buyer_id FROM offers WHERE id=:id'),
        {'id': offer_id}
    ).fetchone()

    item = conn.execute(
        text('SELECT id, name, price, seller_id, item_type FROM items WHERE id=:id'),
        {'id': offer.item_id}
    ).fetchone()

    await context.bot.send_message(
        chat_id=item.seller_id,
        text=f"구매자가 '{item.name}' 물품에 대한 결제를 완료했습니다. "
             "물품을 구매자에게 전달해주세요."
    )

    await context.bot.send_message(
        chat_id=buyer_id,
        text="테더(USDT) 입금이 확인되었습니다. 거래를 진행해주세요."
    )

    # 물품 유형에 따른 메시지 전송
    if item.item_type == "디지털":
        await context.bot.send_message(
            chat_id=item.seller_id,
            text="디지털 물품의 경우, 구매자에게 파일을 전송하려면 /sendfile 명령어를 사용하세요."
        )
    else:
        await context.bot.send_message(
            chat_id=buyer_id,
            text="현물 물품의 경우, 배송받을 주소를 /address 명령어를 통해 판매자에게 알려주세요."
        )

    # 거래 상태 업데이트
    conn.execute(
        text('UPDATE offers SET status=:status WHERE id=:id'),
        {'status': 'paid', 'id': offer_id}
    )
    conn.commit()

    # 거래 완료 시 초과 입금 금액 반환 및 판매자 정산
async def complete_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, offer_id: int) -> None:
    offer = conn.execute(
        text('SELECT item_id, buyer_id FROM offers WHERE id=:id'),
        {'id': offer_id}
    ).fetchone()

    item = conn.execute(
        text('SELECT id, name, price, seller_id FROM items WHERE id=:id'),
        {'id': offer.item_id}
    ).fetchone()

    # 판매자에게 정산할 금액 계산 (중개 수수료 차감)
    escrow_fee = item.price * ESCROW_FEE_PERCENTAGE
    seller_amount = item.price - escrow_fee

    # 송금 트랜잭션 생성
    seller_address = await get_user_wallet_address(item.seller_id)

    # 판매자에게 정산 (수수료 차감 후)
    txn = (
        client.trx.transfer(TRONLINK_ADDRESS, seller_address, int(seller_amount * (10 ** 6)))
        .memo(f"'{item.name}' 거래 완료")
        .build()
        .sign(PrivateKey(bytes.fromhex(PRIVATE_KEY)))
        .broadcast()
    )

    await update.message.reply_text(
        f"판매자에게 {seller_amount} USDT를 송금했습니다.\n"
        f"중개 수수료: {escrow_fee} USDT"
    )

    # 거래 상태 업데이트
    conn.execute(
        text('UPDATE offers SET status=:status WHERE id=:id'),
        {'status': 'completed', 'id': offer_id}
    )
    conn.commit()

    # 거래 완료 메시지
    await context.bot.send_message(
        chat_id=item.seller_id,
        text=f"거래가 완료되었습니다! {seller_amount} USDT를 수령하셨습니다."
    )

    await context.bot.send_message(
        chat_id=offer.buyer_id,
        text="거래가 완료되었습니다! 판매자와의 거래를 평가해주세요 (/rate 명령어 사용)."
    )

    # 채팅 시작 명령어 (/chat)
async def start_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    offer_id = context.user_data.get('offer_id')

    if not offer_id:
        await update.message.reply_text("현재 활성화된 거래가 없습니다. /list 명령어로 물품을 선택해주세요.")
        return

    offer = conn.execute(
        text('SELECT item_id, buyer_id FROM offers WHERE id=:id'),
        {'id': offer_id}
    ).fetchone()

    item = conn.execute(
        text('SELECT id, name, seller_id FROM items WHERE id=:id'),
        {'id': offer.item_id}
    ).fetchone()

    # 구매자와 판매자 매핑
    if user_id == item.seller_id:
        context.user_data['chat_partner'] = offer.buyer_id
    elif user_id == offer.buyer_id:
        context.user_data['chat_partner'] = item.seller_id
    else:
        await update.message.reply_text("잘못된 요청입니다.")
        return

    await update.message.reply_text("채팅이 시작되었습니다. 메시지를 입력하세요. /exit 명령어로 채팅을 종료할 수 있습니다.")

    # 메시지 중개 처리
async def forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_partner = context.user_data.get('chat_partner')
    if not chat_partner:
        await update.message.reply_text("현재 활성화된 채팅이 없습니다. /chat 명령어를 사용해 채팅을 시작하세요.")
        return

    message = update.message.text
    await context.bot.send_message(chat_id=chat_partner, text=message)

# 파일 전송 처리
async def forward_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_partner = context.user_data.get('chat_partner')
    if not chat_partner:
        await update.message.reply_text("현재 활성화된 채팅이 없습니다. /chat 명령어를 사용해 채팅을 시작하세요.")
        return

    # 파일 전송
    if update.message.document:
        file = update.message.document
        await file.forward(chat_partner)
    elif update.message.photo:
        photo = update.message.photo[-1]
        await photo.forward(chat_partner)

# 평가 시스템 (구매자가 판매자를 평가)
async def rate_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    offer_id = context.user_data.get('offer_id')

    if not offer_id:
        await update.message.reply_text("현재 평가할 거래가 없습니다.")
        return

    await update.message.reply_text("거래 평가를 1점에서 5점 사이의 숫자로 입력해주세요.")
    return WAITING_FOR_RATING

# 평가 점수 입력 처리
async def set_rating(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        rating = int(update.message.text)
        if rating < 1 or rating > 5:
            raise ValueError("평가 점수는 1에서 5 사이여야 합니다.")

        offer_id = context.user_data.get('offer_id')
        offer = conn.execute(
            text('SELECT item_id FROM offers WHERE id=:id'),
            {'id': offer_id}
        ).fetchone()

        item = conn.execute(
            text('SELECT seller_id FROM items WHERE id=:id'),
            {'id': offer.item_id}
        ).fetchone()

        # 판매자 평가 저장
        conn.execute(
            text('INSERT INTO ratings (user_id, rating) VALUES (:user_id, :rating)'),
            {'user_id': item.seller_id, 'rating': rating}
        )
        conn.commit()

        await update.message.reply_text(f"거래 평가가 완료되었습니다! {rating}점을 판매자에게 부여하였습니다.")
        return ConversationHandler.END

    except ValueError as e:
        await update.message.reply_text("유효한 숫자를 입력해주세요. 1점에서 5점 사이의 숫자를 입력해야 합니다.")
        return WAITING_FOR_RATING
    
# 초기 화면으로 돌아가는 /exit 명령어
async def exit_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()  # 모든 상태 초기화
    await update.message.reply_text(
        "초기 화면으로 돌아왔습니다. 판매할 물품은 /sell, 구매할 물품은 /list를 입력해주세요."
    )

def main():
    application = ApplicationBuilder().token(TELEGRAM_API_KEY).build()

    # 판매 물품 등록 대화 흐름
    sell_handler = ConversationHandler(
        entry_points=[CommandHandler('sell', sell)],
        states={
            WAITING_FOR_ITEM_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_type)],
            WAITING_FOR_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_name)],
            WAITING_FOR_ITEM_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_item_price)],
        },
        fallbacks=[CommandHandler('exit', exit_to_main)]
    )

    # 평가 시스템 대화 흐름
    rate_handler = ConversationHandler(
        entry_points=[CommandHandler('rate', rate_transaction)],
        states={
            WAITING_FOR_RATING: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_rating)],
        },
        fallbacks=[CommandHandler('exit', exit_to_main)]
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_items))
    application.add_handler(CommandHandler("next", list_next_page))
    application.add_handler(CommandHandler("previous", list_previous_page))
    application.add_handler(CommandHandler("offer", make_offer))
    application.add_handler(CommandHandler("accept", accept_offer))
    application.add_handler(CommandHandler("reject", reject_offer))
    application.add_handler(CommandHandler("chat", start_chat))
    application.add_handler(CommandHandler("exit", exit_to_main))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, forward_message))
    application.add_handler(MessageHandler(filters.Document.ALL, forward_file))
    application.add_handler(rate_handler)
    application.add_handler(sell_handler)

    application.run_polling()

if __name__ == '__main__':
    main()

