"""
법안 발의자·회의록 발언자로 이미 코퍼스에 등장한 국회의원들의 프로필을
열린국회정보 API(국회의원 인적사항, 서비스ID nwvrqwxyaytdsfvhu)에서 받아
data/docs/people/{이름}.md 로 저장한다.

확인된 사실 (Claude Code 세션에서 검증):
  - 서비스ID는 문서화가 안 돼 있고 open.assembly.go.kr 사이트에서 "국회의원 인적사항"으로
    검색해서 직접 확인함: https://open.assembly.go.kr/portal/openapi/nwvrqwxyaytdsfvhu
  - HG_NM(이름)으로 검색하며, 정당·선거구·소속위원회·재선여부·당선대수·약력(MEM_TITLE)까지 준다.
  - 장관/차관(정부위원)은 이 API에 없다 (국회의원 DB이므로 당연함). 그리고 동명이인 함정이 있다 —
    예: "김민석" 검색 시 2024년 고용노동부차관 김민석이 아니라 현직 국무총리 김민석(의원 출신)이
    나온다. TARGET_MEMBERS에 없는 이름이면 이 API로 정부위원 프로필을 만들지 말 것.
  - 정부위원(이정식/김영훈/김민석 차관/권창준 차관) 4명은 이 API로 커버 안 되므로 별도로
    data/docs/people/에 수동 작성했다 (간단한 역할·재임기간 정보뿐이라 자동화 불필요).

사용법:
    python scripts/fetch_member_profiles.py
"""
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ.get("ASSEMBLY_API_KEY")

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "docs" / "people"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MEMBER_URL = "https://open.assembly.go.kr/portal/openapi/nwvrqwxyaytdsfvhu"

# 법안 발의자(seed_bills.json) + 회의록 발언자(data/docs/meetings)로 코퍼스에 이미 등장한 의원 23명.
TARGET_MEMBERS = [
    "박해철", "김태선", "이용우", "신장식", "윤종오", "김주영", "김용민", "권영세",
    "이수진", "박정", "정혜경", "윤재옥", "송옥주",
    "김형동", "김위상", "박홍배", "우재준", "이학영", "안호영", "강득구", "임이자", "조지연", "김소희",
]


def fetch_profile(name: str) -> dict | None:
    resp = requests.get(
        MEMBER_URL,
        params={"KEY": API_KEY, "Type": "json", "pIndex": 1, "pSize": 10, "HG_NM": name},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    for block in data.get("nwvrqwxyaytdsfvhu", []):
        if "row" in block and block["row"]:
            return block["row"][0]
    return None


def save_markdown(name: str, row: dict):
    lines = [
        f"# {row['HG_NM']} ({row.get('HJ_NM', '')})",
        "",
        f"정당: {row.get('POLY_NM', '')}",
        f"선거구: {row.get('ORIG_NM', '')} ({row.get('ELECT_GBN_NM', '')})",
        f"소속위원회: {row.get('CMITS', '')}",
        f"당선: {row.get('UNITS', '')} ({row.get('REELE_GBN_NM', '')})",
        f"국회의원코드(MONA_CD): {row.get('MONA_CD', '')}",
        "",
        "## 약력",
        row.get("MEM_TITLE", "").strip(),
    ]
    out_path = OUT_DIR / f"{name}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  저장: {out_path}")


def main():
    if not API_KEY:
        raise SystemExit("ASSEMBLY_API_KEY 없음 (.env 확인)")
    for name in TARGET_MEMBERS:
        print(f"- {name}")
        row = fetch_profile(name)
        if not row:
            print(f"  [경고] {name} 프로필을 찾지 못함")
            continue
        save_markdown(name, row)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
