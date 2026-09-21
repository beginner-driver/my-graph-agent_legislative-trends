"""
data/docs 코퍼스에서 config.json 스키마(Bill/Person/Committee, PROPOSED_BY/REVIEWED_BY/STATED_POSITION_ON)에
맞는 지식그래프를 만든다.

추출 전략:
  - PROPOSED_BY, REVIEWED_BY: data/seed_bills.json에서 결정론적으로 뽑는다. 발의자명은 의안명
    문자열에 이미 구조화돼 있고("...(박해철의원 등 14인)"), 심사 위원회도 API로 이미 확인된
    사실이라 LLM이 필요 없다.
  - STATED_POSITION_ON: data/docs/meetings의 회의록에서만 LLM으로 뽑는다. 누가 어떤 법안에
    찬성/반대/조건부/설명 입장을 밝혔는지는 텍스트를 읽고 판단해야 하는 유일한 관계이기 때문.
    회의마다 1회 호출로, 그 회의에 등장한 (등록된) 발언자 전원을 한 번에 분류받는다
    — 발언자별로 개별 호출하지 않아 API 비용을 낮춘다.

정규화 규칙:
  - "위원장 안호영", "소위원장 김주영", "OOO 위원" 같은 직함 접두/접미사를 떼고 순수 이름만
    남긴다 (별칭 병합).
  - 위원장 대안의 제안자로 찍히는 "환경노동위원장"(직함, 사람 이름이 아님)은 실제 재임자인
    안호영으로 정규화한다 (회의록에서 2024·2025 모두 그가 위원장으로 주재했음을 확인함).
  - 전문위원·기타 미등록 인물(예: 관련 없는 인사청문회 대상자)은 data/docs/people에 프로필이
    없으므로 Person 후보에서 자동으로 빠진다 — 이 코퍼스의 Person은 "국회의원 + 정부위원"만
    다루기로 한 config.json 스키마와 일치.
  - 절차적 발언(개의/상정/산회 선언 등 의사진행)은 STATED_POSITION_ON 추출 대상에서 제외한다
    (config.json의 hub_exclusion_note, data/goldenset.json의 note와 동일 기준). LLM 프롬프트에
    "해당없음" 분류로 명시했다.

출력:
  output/graph.graphml — networkx 그래프
  output/build_log.json — 추출 통계 + STATED_POSITION_ON 판단 근거(감사용)

사용법:
    python build_graph.py
"""
import json
import re
from pathlib import Path
from typing import Literal

import networkx as nx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PEOPLE_DIR = DATA_DIR / "docs" / "people"
MEETINGS_DIR = DATA_DIR / "docs" / "meetings"
OUT_DIR = BASE_DIR / "output"
OUT_DIR.mkdir(exist_ok=True)

# 위원장 대안의 제안자 "환경노동위원장"(직함) -> 실제 재임 위원장(사람) 정규화
PROPOSER_ALIASES = {"환경노동위원장": "안호영"}

# 라운드별 위원장 대안 bill_no. seed_bills.json의 status는 "대안반영폐기"라고만 적혀 있고 어느
# 대안으로 흡수됐는지 번호가 없다 — 같은 위원회를 공유한다는 사실만으로는 "병합됐다"를 특정
# Bill과 확정할 수 없다(같은 위원회를 거쳤지만 병합 안 된 계류 법안도 있으므로). decision_result에
# 번호를 명시적으로 채워 넣어야 agent.py가 그래프 순회만으로 답을 찾을 수 있다.
ROUND_ALTERNATIVE = {"2024": "2202444", "2025": "2211924"}

ROLE_PREFIXES = [
    "조정위원장직무대행 ", "조정위원장 ", "소위원장 ", "위원장 ",
    "고용노동부장관 ", "고용노동부차관 ", "환경부장관후보자 ", "전문위원 ",
]

# "누가 회의를 주재했는가"는 관계로 뽑지 않기로 했지만(절차 발언이라 STATED_POSITION_ON에서 뺐음),
# 그렇다고 이 사실 자체가 그래프에서 완전히 사라지면 "위원장이 동일 인물인가?" 같은 질문에 답할
# 방법이 없다. 관계를 늘리는 대신 Committee 노드의 속성으로 한 줄만 남긴다 — 스키마는 그대로 두고
# 정규화 단계에서 값만 채우는 것이라 비용이 거의 없다.
CHAIR_ROLE_LABELS = {"위원장 ": "chair", "소위원장 ": "subcommittee_chair"}

MEETING_CONFIG = [
    {"file": "053897.md", "target_bill": "2202444", "date": "2024-07-09"},
    {"file": "053928.md", "target_bill": "2202444", "date": "2024-07-18"},
    # 053920.md는 앞의 96%가 환경부장관 후보자 인사청문회이고 노동조합법 상정은 맨 끝부분이다.
    # start_marker 이전을 잘라내지 않으면 위원들의 청문회 발언이 노동조합법 입장으로 오분류된다
    # (실제로 겪은 버그: 박홍배·박해철 등의 후보자 인사검증 발언이 '반대'로 잘못 뽑혔었음).
    {"file": "053920.md", "target_bill": "2202444", "date": "2024-07-22",
     "start_marker": "그러면 의사일정 제3항부터 7항까지 이상 5건의 법률안을 일괄하여 상정합니다"},
    {"file": "N053190.md", "target_bill": "2211924", "date": "2025-07-28"},
    {"file": "N053198.md", "target_bill": "2211924", "date": "2025-07-28"},
]


def normalize_person_name(raw: str) -> str:
    name = raw.strip()
    for prefix in ROLE_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return re.sub(r"\s*위원$", "", name).strip()


def load_people_registry() -> dict:
    registry = {}
    for path in PEOPLE_DIR.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        name = path.stem.split("(")[0]
        party_m = re.search(r"^정당: (.+)$", text, re.MULTILINE)
        org_m = re.search(r"^소속: (.+)$", text, re.MULTILINE)
        if party_m:
            registry[name] = {"role": "국회의원", "party_or_org": party_m.group(1).strip()}
        elif org_m:
            registry[name] = {"role": "정부위원", "party_or_org": org_m.group(1).strip()}
    return registry


def parse_proposers(title: str) -> list[str]:
    m = re.search(r"\(([^()]*위원장)\)\s*$", title)
    if m:
        raw = m.group(1)
        return [PROPOSER_ALIASES.get(raw, raw)]
    m = re.search(r"\(([^()]+)\)\s*$", title)
    if not m:
        return []
    inner = re.sub(r"\s*등\s*\d+인\s*$", "", m.group(1))
    names = [re.sub(r"의원$", "", p).strip() for p in inner.split("ㆍ")]
    return [n for n in names if n]


def build_bills_and_committees(G: nx.DiGraph, registry: dict) -> dict:
    seed = json.loads((DATA_DIR / "seed_bills.json").read_text(encoding="utf-8"))
    for committee in ["환경노동위원회", "법제사법위원회", "본회의"]:
        G.add_node(committee, type="Committee")

    bill_titles = {}
    for round_info in seed["rounds"]:
        for b in round_info["bills"] + round_info.get("not_merged_optional", []):
            bill_no = b["bill_no"]
            bill_titles[bill_no] = b["title"]
            decision_result = b.get("decision_result", b.get("status", ""))
            alt = ROUND_ALTERNATIVE.get(round_info["round"])
            if "대안반영폐기" in decision_result and alt and bill_no != alt and "(" not in decision_result:
                decision_result = f"{decision_result}({alt})"
            G.add_node(
                bill_no, type="Bill", title=b["title"],
                propose_date=b.get("propose_date", ""),
                decision_date=b.get("decision_date", ""),
                decision_result=decision_result,
                round=round_info["round"],
            )
            G.add_edge(bill_no, "환경노동위원회", relation="REVIEWED_BY")

            for i, name in enumerate(parse_proposers(b["title"])):
                if name not in registry:
                    registry[name] = {"role": "국회의원", "party_or_org": ""}
                if name not in G:
                    G.add_node(name, type="Person", **registry[name])
                G.add_edge(bill_no, name, relation="PROPOSED_BY", kind="대표" if i == 0 else "공동")

    # 위원장 대안만 법사위·본회의 단계까지 감 (data/RESEARCH_NOTES.md 타임라인 근거)
    G.add_edge("2202444", "본회의", relation="REVIEWED_BY")
    G.add_edge("2211924", "법제사법위원회", relation="REVIEWED_BY")
    G.add_edge("2211924", "본회의", relation="REVIEWED_BY")
    return bill_titles


def extract_chairs(path: Path) -> dict[str, str]:
    """회의록 헤더에서 '위원장 OOO'/'소위원장 OOO'의 첫 등장을 뽑는다 (발언 내용은 안 봄, 누가
    그 직함으로 회의를 주재했는지만 확인). 발언 자체는 여전히 STATED_POSITION_ON 추출 대상이 아니다."""
    headers = re.findall(r"^## (.+)$", path.read_text(encoding="utf-8"), re.MULTILINE)
    chairs = {}
    for header in headers:
        for prefix, label in CHAIR_ROLE_LABELS.items():
            if header.startswith(prefix) and label not in chairs:
                chairs[label] = header[len(prefix):].strip()
    return chairs


def add_chair_attributes(G: nx.DiGraph):
    by_round: dict[str, dict[str, str]] = {}
    for cfg in MEETING_CONFIG:
        round_name = cfg["date"][:4]
        chairs = extract_chairs(MEETINGS_DIR / cfg["file"])
        by_round.setdefault(round_name, {}).update(chairs)
    for round_name, chairs in by_round.items():
        for label, name in chairs.items():
            G.nodes["환경노동위원회"][f"{label}_{round_name}"] = name


def parse_meeting_turns(path: Path, start_marker: str | None = None) -> dict[str, list[str]]:
    text = path.read_text(encoding="utf-8")
    if start_marker:
        idx = text.find(start_marker)
        if idx != -1:
            text = text[idx:]
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    turns: dict[str, list[str]] = {}
    for i in range(1, len(parts), 2):
        speaker = normalize_person_name(parts[i])
        turns.setdefault(speaker, []).append(parts[i + 1].strip())
    return turns


class StanceItem(BaseModel):
    speaker: str
    stance: Literal["찬성", "반대", "조건부", "설명", "해당없음"]
    evidence: str


class MeetingStances(BaseModel):
    items: list[StanceItem]


STANCE_PROMPT = """다음은 "{meeting_title}" 회의록에서 발언자별 발언을 모은 것이다.
이 회의에서 심사 중인 법안은 "{bill_title}"({bill_no})이다.

각 발언자에 대해 이 법안에 대한 입장을 분류하라:
- 찬성: 법안 내용에 명확히 찬성하는 실질적 발언
- 반대: 법안 내용에 명확히 반대하거나 우려를 표하는 실질적 발언
- 조건부: 일부 조건을 달아 찬성 또는 반대하는 발언
- 설명: 입장 표명 없이 법안 내용을 설명·보고하는 발언 (예: 조문 설명)
- 해당없음: 개의 선언, 의사진행, 표결 절차 안내 등 순수 절차적 발언뿐이라 실질적 입장이 없는 경우

evidence는 판단 근거가 된 실제 발언 원문을 그대로 짧게 인용하라. 지어내지 말고, 원문에 없는
내용은 절대 만들지 마라.

{speakers_block}
"""


def extract_stances(meeting_title: str, bill_no: str, bill_title: str, speaker_texts: dict[str, str]) -> list[StanceItem]:
    if not speaker_texts:
        return []
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(MeetingStances)
    speakers_block = "\n\n".join(f"### {name}\n{text[:3000]}" for name, text in speaker_texts.items())
    prompt = STANCE_PROMPT.format(
        meeting_title=meeting_title, bill_no=bill_no, bill_title=bill_title, speakers_block=speakers_block,
    )
    result: MeetingStances = llm.invoke(prompt)
    return result.items


def build_stated_positions(G: nx.DiGraph, registry: dict, bill_titles: dict) -> list[dict]:
    log = []
    for cfg in MEETING_CONFIG:
        path = MEETINGS_DIR / cfg["file"]
        turns = parse_meeting_turns(path, cfg.get("start_marker"))
        speaker_texts = {
            name: "\n".join(chunks) for name, chunks in turns.items() if name in registry
        }
        print(f"- {cfg['file']}: LLM 호출 ({len(speaker_texts)}명 등록된 발언자)")
        items = extract_stances(cfg["file"], cfg["target_bill"], bill_titles[cfg["target_bill"]], speaker_texts)
        for item in items:
            speaker = normalize_person_name(item.speaker)
            record = {
                "meeting": cfg["file"], "date": cfg["date"], "speaker": speaker,
                "bill": cfg["target_bill"], "stance": item.stance, "evidence": item.evidence,
            }
            log.append(record)
            if item.stance == "해당없음" or speaker not in registry:
                continue
            if speaker not in G:
                G.add_node(speaker, type="Person", **registry[speaker])
            G.add_edge(
                speaker, cfg["target_bill"], relation="STATED_POSITION_ON",
                stance=item.stance, evidence=item.evidence, date=cfg["date"], source=cfg["file"],
            )
    return log


def main():
    G = nx.DiGraph()
    registry = load_people_registry()
    bill_titles = build_bills_and_committees(G, registry)
    add_chair_attributes(G)
    print(f"법안/위원회/발의자 그래프 구축 완료: 노드 {G.number_of_nodes()}개, 엣지 {G.number_of_edges()}개")

    log = build_stated_positions(G, registry, bill_titles)
    print(f"회의록 입장 추출 완료: 노드 {G.number_of_nodes()}개, 엣지 {G.number_of_edges()}개")

    nx.write_graphml(G, OUT_DIR / "graph.graphml")
    (OUT_DIR / "build_log.json").write_text(
        json.dumps({"stated_position_extractions": log}, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"저장: {OUT_DIR / 'graph.graphml'}, {OUT_DIR / 'build_log.json'}")


if __name__ == "__main__":
    main()
