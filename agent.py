"""
질문 -> 시작 개체 찾기 -> n홉 확장(허브 회피) -> 수집한 근거만으로 답변 + 실제로 탄 경로 제시.

LangGraph 파이프라인: find_entities -> expand_hop -> (조건부: 더 확장할지) -> generate_answer

홉 수·허브 회피 기준 (config.json max_hops=3과 함께 여기서 확정):
  - 최대 3홉까지 확장한다 (config.json hop_limit.max_hops).
  - 차수(degree)가 HUB_DEGREE_THRESHOLD(6)를 넘는 노드는 허브로 본다. 이 그래프에서는
    환경노동위원회(19), 2211924(15), 2202444(14)가 해당한다 — 전부 "위원장 대안"이거나
    모든 법안이 걸리는 위원회라서 그대로 펼치면 무관한 이웃까지 전부 들어와 답이 흐려진다.
  - 허브를 지날 때는 이웃을 전부 펼치지 않고, 관계 힌트(질문에 "발의"/"심사"/"입장" 같은 단어가
    있으면 그 관계를 우선)와 키워드 겹침 점수로 상위 MAX_HUB_FANOUT(10)개만 남긴다. 단, 질문에
    의안번호가 직접 언급된 이웃은 점수와 무관하게 항상 포함한다. 6에서 10으로 올린 이유: "숫자
    2211924가 언급됐다"는 신호가 "2211924에 반영됐다"는 법안과 "2211924와 무관하다"는 법안을
    구분 못 해서(둘 다 decision_result 텍스트에 "2211924"가 등장) 정작 필요한 계류 법안들이
    밀려나는 사례가 있었다 — 완전한 해결책은 아니고, 밀려날 여지를 줄이는 수준의 보완이다.
  - 그래도 근거가 부족하면(경로가 끊기거나 empty) 답을 지어내지 않고 "확인되지 않는다"고 말한다
    (data/goldenset.json 멀티홉-05가 이 동작을 검증하는 문항).

사용법:
    python agent.py "질문 문자열"
"""
import re
import sys
from typing import TypedDict

import networkx as nx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END
from pydantic import BaseModel

load_dotenv()

GRAPH_PATH = "output/graph.graphml"
MAX_HOPS = 3
HUB_DEGREE_THRESHOLD = 6
MAX_HUB_FANOUT = 10


class Hop(TypedDict):
    frm: str
    relation: str
    to: str
    direction: str  # "forward" | "backward"
    hop: int


class AgentState(TypedDict):
    question: str
    graph: nx.DiGraph
    start_entities: list[str]
    visited: set[str]
    frontier: list[str]
    hop_count: int
    path: list[Hop]
    evidence: list[str]
    answer: str


class _StartEntities(BaseModel):
    entities: list[str]


def _llm_find_entities(question: str, node_ids: list[str]) -> list[str]:
    """문자열 매칭으로 못 찾을 때만 쓰는 폴백. "동일 인물인가?"처럼 질문 자체가 이름을 몰라서
    묻는 경우(이름이 문장에 없음) 이걸로 잡는다. 목록에 없는 이름은 못 쓰게 강제한다."""
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(_StartEntities)
    prompt = f"""다음은 지식그래프에 있는 노드 이름 전체 목록이다:
{node_ids}

질문: {question}

이 질문에 답하려면 그래프의 어느 노드에서 탐색을 시작해야 하는가? 위 목록에 있는 이름만 정확히
그대로 골라라. 목록에 없는 이름은 절대 만들지 마라. 질문이 특정 인물을 몰라서 묻는 경우
(예: "동일 인물인가?", "누구인가?")는 그 사람을 찾을 단서가 되는 위원회·법안 노드를 골라라.
"""
    result = llm.invoke(prompt)
    return [e for e in result.entities if e in node_ids]


def find_start_entities(state: AgentState) -> dict:
    G, q = state["graph"], state["question"]
    matched = [n for n in G.nodes if n in q]
    if not matched:
        # gpt-4o-mini는 temperature=0이어도 완전히 결정적이지 않아서, 실측 결과 이 폴백 호출이
        # 꽤 자주(4번에 1번꼴) 빈 리스트를 반환하는 걸 확인했다. ponytail: 재시도 3회면 실측
        # 실패율을 눈에 띄게 낮추지만 0으로 만들진 못한다 — 완전히 없애려면 더 나은 모델이나
        # 임베딩 기반 매칭으로 바꿔야 하는데, 지금 규모(노드 44개)에서는 과한 투자라 보류.
        for _ in range(3):
            matched = _llm_find_entities(q, list(G.nodes))
            if matched:
                break
    evidence = [f"[시작개체] {n}: {dict(G.nodes[n])}" for n in matched]
    return {
        "start_entities": matched,
        "visited": set(matched),
        "frontier": matched,
        "hop_count": 0,
        "path": [],
        "evidence": evidence,
    }


def _neighbors(G: nx.DiGraph, node: str) -> list[tuple[str, str, str, dict]]:
    out = [(v, d["relation"], "forward", d) for _, v, d in G.out_edges(node, data=True)]
    inn = [(u, d["relation"], "backward", d) for u, _, d in G.in_edges(node, data=True)]
    return out + inn


# 한국어는 조사가 단어에 그대로 붙는다("계류로", "위원회는") — 정확한 토큰 일치만 보면 "계류로"와
# 노드 속성의 "계류"가 다른 글자로 취급돼 매칭이 안 된다. 흔한 조사만 떼어내는 가벼운 보정.
# (형태소 분석기를 쓸 정도는 아니라서 ponytail: 자주 쓰는 조사 목록으로만 처리, 완벽하진 않음)
_JOSA_SUFFIXES = sorted(["으로", "로", "은", "는", "이", "가", "을", "를", "의", "에서", "에", "도", "만", "까지", "부터", "와", "과"], key=len, reverse=True)


def _strip_josa(token: str) -> str:
    for suf in _JOSA_SUFFIXES:
        if token.endswith(suf) and len(token) > len(suf) + 1:
            return token[: -len(suf)]
    return token


def _relevance_score(question: str, node: str, node_attrs: dict, edge_attrs: dict) -> int:
    text = " ".join([node, str(node_attrs), str(edge_attrs)])
    q_tokens = {_strip_josa(t) for t in re.findall(r"[가-힣]{2,}|\d{4,}", question)}
    return sum(1 for t in q_tokens if t in text)


# 질문에 이 키워드가 있으면 그 관계 종류를 우선한다. 허브를 지날 때 "무슨 관계를 찾는 질문인지"가
# 이름 키워드 겹침보다 훨씬 강한 신호이기 때문 — 예: "제안자가 누구인가"는 PROPOSED_BY 하나면
# 충분한데, 이름 키워드 겹침만 보면 REVIEWED_BY/STATED_POSITION_ON 이웃까지 무작위로 섞여 들어온다.
RELATION_KEYWORDS = {
    "PROPOSED_BY": ["제안", "발의"],
    "REVIEWED_BY": ["심사", "위원회", "소관", "회부"],
    "STATED_POSITION_ON": ["입장", "찬성", "반대", "지지", "조건부", "발언"],
}


def _relevant_relations(question: str) -> set[str]:
    return {rel for rel, kws in RELATION_KEYWORDS.items() if any(kw in question for kw in kws)}


def expand_hop(state: AgentState) -> dict:
    G, q = state["graph"], state["question"]
    visited, path, evidence = set(state["visited"]), list(state["path"]), list(state["evidence"])
    new_frontier: list[str] = []
    rel_hints = _relevant_relations(q)
    # 엣지 단위 중복만 걸러낸다. "이미 visited라서 후보에서 뺀다"를 여기서 하면, 같은 홉 안에서
    # 서로 다른 frontier 노드가 같은 이웃을 서로 다른 관계로 가리킬 때(예: 환경노동위원회는
    # REVIEWED_BY로, 김영훈은 STATED_POSITION_ON으로 같은 법안을 가리킴) 먼저 처리된 쪽이 그
    # 이웃을 visited로 찍어버려서 뒤에 처리되는 진짜 필요한 엣지(김영훈의 입장 근거)가 통째로
    # 사라지는 버그가 있었다. visited는 "더 확장할지"만 결정하고, 엣지 기록은 별개로 취급한다.
    seen_edges = {(h["frm"], h["relation"], h["to"], h["direction"]) for h in path}

    for node in state["frontier"]:
        candidates = _neighbors(G, node)
        if G.degree(node) > HUB_DEGREE_THRESHOLD:
            forced = [c for c in candidates if c[0] in q]
            rest = [c for c in candidates if c[0] not in q]
            # 관계 힌트는 "이것만 남기고 나머지는 버린다"가 아니라 "이걸 먼저 채우고, 자리가
            # 남으면 다른 관계도 채운다"로 처리한다. 힌트로 완전히 걸러버리면, 질문에 두 가지
            # 관계가 동시에 필요한 경우(예: "재발의"→PROPOSED_BY 힌트 + "계류로 남았나"→사실은
            # REVIEWED_BY도 필요) 뒤쪽 관계로 가는 길이 통째로 막혀 버리는 문제가 실제로 있었다.
            scored = sorted(
                rest,
                key=lambda c: (c[1] in rel_hints, _relevance_score(q, c[0], G.nodes[c[0]], c[3])),
                reverse=True,
            )
            candidates = forced + scored[: max(0, MAX_HUB_FANOUT - len(forced))]

        for neighbor, relation, direction, edge_attrs in candidates:
            edge_key = (node, relation, neighbor, direction)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            path.append({
                "frm": node, "relation": relation, "to": neighbor,
                "direction": direction, "hop": state["hop_count"] + 1,
            })
            evidence.append(f"[{node} —{relation}({direction})→ {neighbor}] 노드속성={dict(G.nodes[neighbor])} 엣지속성={edge_attrs}")
            if neighbor not in visited:
                visited.add(neighbor)
                new_frontier.append(neighbor)

    return {
        "visited": visited, "path": path, "evidence": evidence,
        "frontier": new_frontier, "hop_count": state["hop_count"] + 1,
    }


def should_continue(state: AgentState) -> str:
    if state["hop_count"] >= MAX_HOPS or not state["frontier"]:
        return "generate_answer"
    return "expand_hop"


ANSWER_PROMPT = """질문: {question}

아래는 지식그래프에서 수집한 근거뿐이다. 이 근거만 사용해서 답하라. 근거에 없는 내용은
절대 지어내지 마라. 근거가 부족하거나 질문에 맞는 사실이 없으면 "코퍼스에서 확인되지 않습니다"라고
답하라.

근거 각 줄은 "[A -관계-> B] 노드속성=... 엣지속성=..." 형태다. 노드속성의 decision_result(법안
처리 결과)나 엣지속성의 stance/evidence(입장 표명)에 질문의 답이 직접 들어있는 경우가 많으니
빠짐없이 확인하라. 예를 들어 "같은 위원회를 거친 법안 중 실제로 반영된 것"을 묻는 질문이면,
후보 법안들의 decision_result를 하나씩 대조해서 "대안반영폐기"인 것과 "계류"인 것을 구분해야 한다.

[수집된 근거]
{evidence}
"""


def generate_answer(state: AgentState) -> dict:
    if not state["start_entities"]:
        return {"answer": "질문에서 그래프의 시작 개체(법안번호·인물명·위원회명)를 찾지 못해 답할 수 없습니다."}
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = ANSWER_PROMPT.format(question=state["question"], evidence="\n".join(state["evidence"]))
    answer = llm.invoke(prompt).content
    return {"answer": answer}


def build_agent():
    graph = StateGraph(AgentState)
    graph.add_node("find_entities", find_start_entities)
    graph.add_node("expand_hop", expand_hop)
    graph.add_node("generate_answer", generate_answer)
    graph.set_entry_point("find_entities")
    graph.add_conditional_edges("find_entities", should_continue, {"expand_hop": "expand_hop", "generate_answer": "generate_answer"})
    graph.add_conditional_edges("expand_hop", should_continue, {"expand_hop": "expand_hop", "generate_answer": "generate_answer"})
    graph.add_edge("generate_answer", END)
    return graph.compile()


def answer_question(question: str, G: nx.DiGraph | None = None) -> dict:
    if G is None:
        G = nx.read_graphml(GRAPH_PATH)
    app = build_agent()
    result = app.invoke({"question": question, "graph": G})
    return {
        "answer": result["answer"], "path": result["path"], "evidence": result["evidence"],
        "start_entities": result["start_entities"],
    }


def format_path(path: list[Hop]) -> str:
    if not path:
        return "(경로 없음)"
    arrows = {"forward": "→", "backward": "←"}
    return " / ".join(f"{h['frm']} {arrows[h['direction']]}{h['relation']}{arrows[h['direction']]} {h['to']} ({h['hop']}홉)" for h in path)


if __name__ == "__main__":
    question = sys.argv[1] if len(sys.argv) > 1 else "위원장 대안(2211924)의 제안자는 누구인가?"
    result = answer_question(question)
    print("질문:", question)
    print("답변:", result["answer"])
    print("경로:", format_path(result["path"]))
