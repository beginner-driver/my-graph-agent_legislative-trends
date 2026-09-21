"""
seed_bills.json에 있는 의안번호 목록으로 열린국회정보 API(BPMBILLSUMMARY)에서
제안이유/주요내용 원문을 받아 data/docs/bills/{bill_no}.md 로 저장한다.

확인된 사실 (Claude Code 세션에서 검증):
  - BPMBILLSUMMARY는 BILL_NO 하나만 있으면 SUMMARY(제안이유+주요내용 전문)를 그대로 준다.
    likms.assembly.go.kr의 billDetailPage.do는 JS 렌더링이라 Playwright가 필요했지만,
    이 API를 쓰면 브라우저 자동화 없이 훨씬 간단하고 안정적으로 같은 내용을 얻을 수 있다.
  - data.go.kr 키는 불필요.

사용법:
    export ASSEMBLY_API_KEY=발급받은_키
    python scripts/fetch_bills.py
"""
import os
import json
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ.get("ASSEMBLY_API_KEY")

BASE_DIR = Path(__file__).resolve().parent.parent
SEED_PATH = BASE_DIR / "data" / "seed_bills.json"
OUT_DIR = BASE_DIR / "data" / "docs" / "bills"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SUMMARY_URL = "https://open.assembly.go.kr/portal/openapi/BPMBILLSUMMARY"


def load_seed_bills():
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    bills = []
    for round_info in data["rounds"]:
        for b in round_info["bills"]:
            b = dict(b)
            b["round"] = round_info["round"]
            bills.append(b)
        for b in round_info.get("not_merged_optional", []):
            b = dict(b)
            b["round"] = round_info["round"] + "_optional"
            bills.append(b)
    return bills


def fetch_summary(bill_no: str) -> str:
    resp = requests.get(
        SUMMARY_URL,
        params={"KEY": API_KEY, "Type": "json", "pIndex": 1, "pSize": 10, "BILL_NO": bill_no},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    for block in data.get("BPMBILLSUMMARY", []):
        if "row" in block and block["row"]:
            return block["row"][0].get("SUMMARY", "")
    return ""


def save_markdown(bill: dict, body: str):
    bill_no = bill["bill_no"]
    title = bill["title"]
    lines = [f"# {title}", "", f"의안번호: {bill_no}"]
    if bill.get("propose_date"):
        lines.append(f"제안일자: {bill['propose_date']}")
    if bill.get("decision_date"):
        lines.append(f"의결일자: {bill['decision_date']}")
    if bill.get("decision_result"):
        lines.append(f"의결결과: {bill['decision_result']}")
    if bill.get("committee"):
        lines.append(f"소관위원회: {bill['committee']}")
    lines.append(f"라운드: {bill['round']}")
    lines.append("")
    lines.append(body if body else "(본문 없음 - 수동 확인 필요)")
    out_path = OUT_DIR / f"{bill_no}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  저장: {out_path}")


def main():
    if not API_KEY:
        raise SystemExit("ASSEMBLY_API_KEY 없음 (.env 확인)")
    bills = load_seed_bills()
    print(f"총 {len(bills)}건 처리 시작")
    for bill in bills:
        bill_no = bill["bill_no"]
        print(f"- {bill_no} {bill['title']}")
        try:
            body = fetch_summary(bill_no)
        except Exception as e:
            print(f"  [오류] API 요청 실패: {e}")
            continue
        save_markdown(bill, body)
        time.sleep(0.3)  # 과도한 연속 요청 방지


if __name__ == "__main__":
    main()
