"""
열린국회정보 / 공공데이터포털 의안정보 API 스모크 테스트.

목적: 정확한 엔드포인트·응답 필드명을 아직 확정하지 못했다 (이 스크립트를 만든 세션에서는
      국회 쪽 도메인으로 나가는 네트워크 자체가 방화벽에 막혀 실제 호출로 검증하지 못했음).
      Claude Code 환경(정상 인터넷)에서 이 스크립트를 먼저 돌려서, 어떤 후보가 실제로 동작하는지
      확인하고 그 결과에 맞춰 fetch_bills.py의 CANDIDATE 인덱스/필드명을 확정할 것.

사용법:
    export ASSEMBLY_API_KEY=발급받은_키   (또는 .env 파일에 저장 후 python-dotenv로 로드)
    python scripts/test_api.py
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ.get("ASSEMBLY_API_KEY")
if not API_KEY:
    sys.exit("ASSEMBLY_API_KEY 환경변수가 없습니다. .env 파일을 만들거나 export 하세요.")

BILL_NAME = "노동조합 및 노동관계조정법 일부개정법률안"

# 확인이 필요한 후보 엔드포인트들. 열린국회정보(open.assembly.go.kr)와 공공데이터포털(data.go.kr)은
# 요청 파라미터 스펙이 다르므로, 실제로 발급받은 키가 어느 포털 것인지에 따라 둘 중 하나만 동작할 수 있다.
CANDIDATES = [
    {
        "label": "open.assembly.go.kr - ALLBILL (의안 목록, 서비스ID는 포털의 'Open API 상세' 페이지에서 재확인 필요)",
        "url": "https://open.assembly.go.kr/portal/openapi/ALLBILL",
        "params": {
            "KEY": API_KEY,
            "Type": "json",
            "pIndex": 1,
            "pSize": 20,
            "AGE": 22,
            "BILL_NAME": BILL_NAME,
        },
    },
    {
        "label": "data.go.kr - 국회사무처_의안정보 통합 API (오퍼레이션명은 활용신청 상세페이지에서 재확인 필요)",
        "url": "http://apis.data.go.kr/9710000/BillInfoService2/getBillInfoList",
        "params": {
            "serviceKey": API_KEY,
            "numOfRows": 20,
            "pageNo": 1,
            "age": 22,
            "billName": BILL_NAME,
        },
    },
]


def try_candidate(cand):
    print("=" * 80)
    print(cand["label"])
    print("URL:", cand["url"])
    try:
        resp = requests.get(cand["url"], params=cand["params"], timeout=15)
        print("status_code:", resp.status_code)
        print("response (first 2000 chars):")
        print(resp.text[:2000])
    except Exception as e:
        print("요청 실패:", e)
    print()


if __name__ == "__main__":
    for c in CANDIDATES:
        try_candidate(c)
    print("위 응답 중 정상적으로 의안 목록(JSON/XML)이 온 후보를 fetch_bills.py의 API_CONFIG에 반영하세요.")
    print("둘 다 실패하면: open.assembly.go.kr에 로그인 후 '의안정보 통합 API' 상세페이지의")
    print("'명세서 다운로드' 또는 샘플 URL을 직접 확인해서 정확한 서비스ID/파라미터명을 채워 넣으세요.")
