from tronpy import Tron
from tronpy.keys import PrivateKey
import os

# Fly.io 환경 변수에서 TronLink 개인 키 가져오기
PRIVATE_KEY = os.getenv('PRIVATE_KEY')

# USDT (TRC20) 스마트 계약 주소 (고정)
USDT_CONTRACT = 'TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj'

# 자동 송금을 위한 Tron 네트워크 클라이언트 초기화
client = Tron()

def send_usdt(amount: float, to_address: str = "TT8AZ3dCpgWJQSw9EXhhyR3uKj81jXxbRB") -> str:
    """USDT (TRC20)를 자동으로 송금하는 함수.

    Args:
        amount (float): 송금할 USDT 금액.
        to_address (str): 송금 받을 TronLink 지갑 주소 (기본값으로 설정).

    Returns:
        str: 트랜잭션 ID (송금 내역 확인용).
    """
    priv_key = PrivateKey(bytes.fromhex(PRIVATE_KEY))

    # USDT (TRC20) 송금 트랜잭션 생성
    txn = (
        client.trx.asset_transfer(USDT_CONTRACT, client.address.to_base58(), to_address, int(amount * 1e6))
        .fee_limit(1000000)
        .build()
        .sign(priv_key)
    )
    
    # 송금 트랜잭션 브로드캐스트 및 결과 반환
    txn_hash = txn.broadcast().wait()
    return txn_hash['id']