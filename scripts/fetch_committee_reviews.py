"""
seed_bills.json의 각 법안에 대해 열린국회정보 API(TVBPMCONFINFO)로
소위원회/안건조정위원회 심사 정보(위원회, 회기, 차수, 회부/상정/처리일, 처리결과)를 조회해
data/committee_reviews.json 으로 저장한다.

확인된 사실 (Claude Code 세션에서 검증):
  - TVBPMCONFINFO는 BILL_NO(+AGE)로 조회하며, 세션/차수/처리일까지 정확히 나온다.
    예: 2200074 -> 고용노동법안심사소위원회 제416회 2차(2024-07-16 처리), 안건조정위원회 제416회 1차(2024-07-18 처리).
  - 이 API는 위원회 "회의 메타정보"만 준다 (CONF_ID나 발언 원문은 없음).
  - 실제 회의록 원문(발언 인용, STATED_POSITION_ON 관계용)은 record.assembly.go.kr의
    "회의록검색"(/assembly/mnts/minutes/search.do)에서 의안번호로 찾아야 하는데, 이 페이지는
    검색 버튼이 JS 아코디언 뒤에 숨어 있고 실제 제출 로직이 불명확해 Playwright 자동화에 실패했다.
    ponytail: 여기서 자동화를 포기함 (안 되는 폼과 씨름하는 것보다 사람이 직접 찾는 게 빠름).
    수동 확인 경로: https://record.assembly.go.kr/assembly/mnts/minutes/search.do 접속 ->
    "검색조건펼치기" 클릭 -> 의안번호 입력란에 bill_no 입력 -> 검색.
    자동화하려면: 브라우저 개발자도구 네트워크 탭에서 실제 검색 시 호출되는 XHR 요청(폼이 아니라
    실제 API 호출)을 캡처해서 그 엔드포인트를 직접 호출하는 방식으로 다시 시도할 것.

사용법:
    python scripts/fetch_committee_reviews.py
"""
import os
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ.get("ASSEMBLY_API_KEY")

BASE_DIR = Path(__file__).resolve().parent.parent
SEED_PATH = BASE_DIR / "data" / "seed_bills.json"
OUT_PATH = BASE_DIR / "data" / "committee_reviews.json"

CONF_INFO_URL = "https://open.assembly.go.kr/portal/openapi/TVBPMCONFINFO"


def load_bill_nos():
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    bill_nos = []
    for round_info in data["rounds"]:
        for b in round_info["bills"]:
            bill_nos.append(b["bill_no"])
        for b in round_info.get("not_merged_optional", []):
            bill_nos.append(b["bill_no"])
    return bill_nos


def fetch_reviews(bill_no: str) -> list[dict]:
    resp = requests.get(
        CONF_INFO_URL,
        params={"KEY": API_KEY, "Type": "json", "pIndex": 1, "pSize": 20, "AGE": 22, "BILL_NO": bill_no},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = []
    for block in data.get("TVBPMCONFINFO", []):
        if "row" in block:
            rows = block["row"]
    return rows


def main():
    if not API_KEY:
        raise SystemExit("ASSEMBLY_API_KEY 없음 (.env 확인)")
    bill_nos = load_bill_nos()
    print(f"총 {len(bill_nos)}건 조회 시작")
    result = {}
    for bill_no in bill_nos:
        rows = fetch_reviews(bill_no)
        result[bill_no] = rows
        print(f"- {bill_no}: {len(rows)}건 심사 기록")
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"저장: {OUT_PATH}")


if __name__ == "__main__":
    main()
