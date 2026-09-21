"""
국회도서관 "발언 빅데이터"(dataset.nanet.go.kr)에서 노란봉투법 심사 회의의 발언자별 발언 전문을
검색·수집해 data/docs/meetings/{conferNum}.md 로 저장한다.

확인된 사실 (Claude Code 세션에서 검증):
  - 홈페이지 검색창(input[name=srchQ])에 검색어를 입력하고 실제 돋보기 버튼(div.btn_search.ml-auto)을
    클릭해야 검색이 실행된다. form.submit()을 직접 호출하면 실제 submit 이벤트가 발생하지 않아
    (JS의 submit 이벤트 리스너를 우회함) 서버가 "error ocurred!"를 반환한다. Enter 키 입력도 자동완성
    드롭다운에 막혀 검색이 안 될 수 있다.
  - 검색 결과 카드는 onclick="selectMeetingDetail('CONFER_NUM')" 속성을 가진다. 이 값을
    page.evaluate()로 직접 호출하면 역시 신뢰된(trusted) 클릭 이벤트가 아니라서 실패한다 —
    반드시 그 속성을 가진 실제 DOM 요소를 Playwright로 클릭해야 한다.
  - 상세페이지(conferNum)로 직접 URL 이동(예: /meeting/detail?conferNum=XXX)은 검색 상태(세션/폼)가
    없으면 "error ocurred!"가 뜬다. 항상 검색 -> 결과 카드 클릭 순서를 거쳐야 한다.
  - 발언 목록은 화면에 보이는 만큼만 로드된다(스크롤해도 추가 로드 안 됨, 실측 215/249건).
    전수는 아니지만 핵심 발언은 충분히 포함된다.
  - 발언자 구분은 "발언자" 필터 체크박스 목록(회의 참석자 전체 이름)을 기준으로 한다. 전문위원·정부
    위원은 국회의원과 달리 "의원정보" 표시가 없어서 그것만으로는 화자 전환을 못 잡기 때문.
    ponytail 한계: 아주 드물게(예: 053920.md의 고용노동부장관 이정식처럼 긴 다중 안건 회의에서 발언이
    1회뿐인 화자) 그 목록에서 빠져 직전 화자 블록에 병합되는 경우가 있다. 중요 발언은
    `data/RESEARCH_NOTES.md`에 원문을 직접 인용해 뒀으니 그걸로 대조할 것. 완전히 고치려면 짧고
    마침표로 안 끝나는 줄을 화자명 후보로 보는 휴리스틱을 추가하면 되지만, 지금은 과한 작업이라 보류.

TARGET_MEETINGS는 회의록 검색 결과에서 미리 확인한 conferNum이다(수동 확인, 아래 각 항목에 근거 기록).
새 대상을 추가하려면: dataset.nanet.go.kr에서 검색 -> 결과 카드의 onclick="selectMeetingDetail('...')"
값을 확인해서 여기 추가하면 된다.

사용법:
    python scripts/fetch_speeches.py
"""
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "docs" / "meetings"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SEARCH_QUERY = "노동조합 및 노동관계조정법"

# (conferNum, 검색결과에 뜨는 제목, 근거)
TARGET_MEETINGS = [
    ("053897", "제22대 국회 제416회 제1차 환경노동위원회 - 고용노동법안심사소위원회 (2024-07-09)",
     "2024년 라운드 최초 소위 심사. committee_reviews.json의 PRESENT_DT(2024-07-09)와 일치."),
    ("053928", "제22대 국회 제416회 제1차 환경노동위원회 - 안건조정위원회 (2024-07-18)",
     "committee_reviews.json의 안건조정위원회 PRESENT_DT/PROC_DT(2024-07-18)와 정확히 일치."),
    ("053920", "제22대 국회 제416회 제2차 환경노동위원회 (2024-07-22)",
     "소위 처리일(2024-07-16) 직후 전체회의. 이정식 장관 발언 확인 후보."),
    ("N053190", "제22대 국회 제427회 제1차 환경노동위원회 - 고용노동법안심사소위원회 (2025-07-28)",
     "committee_reviews.json의 2025년 라운드 PROC_DT(2025-07-28)와 정확히 일치. 김영훈 장관 발언 확인 후보."),
    ("N053198", "제22대 국회 제427회 제5차 환경노동위원회 (2025-07-28)",
     "같은 날 전체회의. 위원장 대안 의결 회의."),
]


def extract_speaker_names(text: str) -> set[str]:
    """'발언자' 필터 체크박스 목록(회의의 전체 참석자 이름, 의원뿐 아니라 전문위원·장관도 포함)을 뽑는다.
    이 목록이 발언 구간을 나누는 기준이 된다 — 전문위원·정부위원은 의원과 달리 '의원정보' 표시가
    없어서 그것만으로는 화자 전환을 못 잡기 때문."""
    idx = text.find("\n발언자\n")
    if idx == -1:
        return set()
    names = set()
    for line in text[idx + len("\n발언자\n"):].split("\n"):
        if line.startswith(" ") and 0 < len(line.strip()) < 30:
            names.add(line.strip())
        else:
            break
    return names


def parse_speech_turns(text: str) -> list[tuple[str, str]]:
    speaker_names = extract_speaker_names(text)
    if not speaker_names:
        return []
    first_marker = text.find("의원정보")
    if first_marker == -1:
        return []
    lines = text[:first_marker].split("\n")
    start_idx = len(lines) - 1  # 첫 화자 이름이 있는 줄부터 시작
    body_lines = text.split("\n")[start_idx:]

    turns = []
    speaker = None
    content_lines: list[str] = []
    for line in body_lines:
        stripped = line.strip()
        if stripped in speaker_names:
            if speaker and content_lines:
                turns.append((speaker, "\n".join(content_lines).strip()))
            speaker = stripped
            content_lines = []
        elif stripped in ("의원정보", "관련저서") or stripped.startswith("관련저서"):
            continue
        else:
            content_lines.append(line)
    if speaker and content_lines:
        turns.append((speaker, "\n".join(content_lines).strip()))
    return [(s, c) for s, c in turns if c]


def extract_meeting_title(text: str) -> str:
    m = re.search(r"(제22대 국회.+?\d{4}년\d{2}월\d{2}일\([A-Za-z]+\))", text)
    return m.group(1) if m else "제목 미확인"


def save_markdown(conf_num: str, title: str, turns: list[tuple[str, str]]):
    lines = [f"# {title}", "", f"회의ID(conferNum): {conf_num}", f"발언 {len(turns)}건 수집", ""]
    for speaker, content in turns:
        lines.append(f"## {speaker}")
        lines.append(content)
        lines.append("")
    out_path = OUT_DIR / f"{conf_num}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  저장: {out_path} ({len(turns)}건)")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto("https://dataset.nanet.go.kr", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)

        for conf_num, label, reason in TARGET_MEETINGS:
            print(f"- {conf_num}: {label}")
            page.goto("https://dataset.nanet.go.kr", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(800)
            box = page.locator("form#searchFrm input[name='srchQ']").first
            box.click()
            box.fill("")
            box.type(SEARCH_QUERY, delay=15)
            page.wait_for_timeout(400)
            page.locator("div.btn_search.ml-auto").first.click()
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(1200)

            card = page.locator(f"[onclick=\"selectMeetingDetail('{conf_num}')\"]").first
            if card.count() == 0:
                print(f"  [건너뜀] 결과에서 conferNum={conf_num} 카드를 찾지 못함 (검색어/페이지 확인 필요)")
                continue
            card.click()
            page.wait_for_load_state("networkidle", timeout=20000)
            page.wait_for_timeout(1200)

            text = page.inner_text("body")
            title = extract_meeting_title(text)
            turns = parse_speech_turns(text)
            if not turns:
                print(f"  [경고] 발언 파싱 결과 0건 (페이지 구조 변경 가능성)")
                continue
            save_markdown(conf_num, title, turns)
            time.sleep(0.5)

        browser.close()


if __name__ == "__main__":
    main()
