"""
data/goldenset.json 문항 13개를 (1) agent.py의 그래프 기반 멀티홉 에이전트와 (2) basic RAG
(그래프 없이 키워드 겹침 상위 문서만 주고 답하게 한 것)로 각각 풀게 하고 채점한다.

측정 항목:
  - path_recall: 기대 경로(reference_contexts)가 agent.py가 실제로 탄 경로에 얼마나 들어왔는지
    (0~1). 전역 문항처럼 reference_contexts가 없는 경우는 None(해당없음)으로 둔다.
  - correct: LLM 판정관(gpt-4o-mini)이 기대 정답과 답변을 대조해 true/false로 채점. 표현이 달라도
    핵심 사실이 맞으면 정답으로 본다.
  - 실패 층 분류(오답일 때만): 아래 순서로 가장 먼저 맞는 것을 고른다.
      색인 — 질문에서 그래프의 시작 개체를 아예 못 찾음 (find_entities가 빈 결과)
      탐색 — 시작 개체는 찾았지만 기대 경로 재현율이 낮음(<0.5) → 홉 확장/허브 회피 단계가 문제
      생성 — 경로는 웬만큼 탔는데(재현율 0.5 이상 또는 전역이라 해당없음) 최종 답변이 틀림 → LLM
             답변 생성 단계가 문제

basic RAG 대조: 문서 전체(51건)에서 질문과 키워드가 많이 겹치는 상위 5개만 컨텍스트로 주고 그래프
순회 없이 답하게 한 것. 멀티홉 문항에서 그래프 기반과 얼마나 차이 나는지 보려는 목적.

출력: output/eval.json (문항별 상세), 콘솔에 홉 수별/유형별 요약표.

사용법:
    python evaluate.py
"""
import json
import re
from collections import defaultdict
from pathlib import Path

import networkx as nx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from agent import GRAPH_PATH, answer_question

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DOCS_DIR = BASE_DIR / "data" / "docs"
OUT_DIR = BASE_DIR / "output"

# 위원장 대안의 제안자 "환경노동위원장"(직함)은 그래프에서 실제 재임자 "안호영"으로 정규화돼 있다
# (build_graph.py PROPOSER_ALIASES). 골든셋 reference_contexts는 정규화 전 표현을 쓰므로 여기서도
# 같은 별칭을 적용해야 경로 재현율이 정확히 잡힌다.
ALIASES = {"환경노동위원장": "안호영"}


def load_all_docs() -> list[dict]:
    return [
        {"path": str(p.relative_to(BASE_DIR)), "text": p.read_text(encoding="utf-8")}
        for p in DOCS_DIR.rglob("*.md")
    ]


def _keyword_score(question: str, text: str) -> int:
    q_tokens = set(re.findall(r"[가-힣]{2,}|\d{4,}", question))
    return sum(1 for t in q_tokens if t in text)


def basic_rag_answer(question: str, docs: list[dict], k: int = 5) -> str:
    top = sorted(docs, key=lambda d: _keyword_score(question, d["text"]), reverse=True)[:k]
    context = "\n\n".join(f"[{d['path']}]\n{d['text'][:1500]}" for d in top)
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = f"""질문: {question}

아래 문서 조각만 근거로 답하라. 근거에 없으면 "확인되지 않습니다"라고 답하라. 지어내지 마라.

{context}
"""
    return llm.invoke(prompt).content


class JudgeResult(BaseModel):
    correct: bool
    reason: str


def judge_answer(question: str, reference: str, candidate: str) -> JudgeResult:
    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0).with_structured_output(JudgeResult)
    prompt = f"""질문: {question}
기대 정답(기준): {reference}
채점할 답변: {candidate}

채점할 답변이 기대 정답의 핵심 사실을 정확히 담고 있는지 판정하라.
- 표현이 다르거나 정답보다 더 자세하거나 추가 정보(다른 위원회, 다른 절차 단계 등)를 더 담고
  있어도, 기대 정답이 말하는 핵심 사실을 반박하지 않고 포함하고 있으면 correct=true로 판단하라.
- "환경노동위원장"(직함)과 "안호영"(그 시점 실제 재임자)은 이 도메인에서 같은 사람을 가리키는
  것으로 취급하라 — 어느 쪽으로 답해도 correct=true.
- 같은 법안을 의안번호 대신 제목·발의일 등 다른 방식으로 식별해도, 실제로 같은 법안을 가리키고
  건수·처리결과 같은 핵심 사실이 일치하면 correct=true로 판단하라. 식별 방식이 다르다는 이유만으로
  틀렸다고 하지 마라.
- 핵심 사실 자체가 틀렸거나(예: 반대인데 찬성이라고 함) 빠져 있으면(예: 확인되지 않는다고만 함)
  correct=false로 판단하라.
이유를 한 문장으로 적어라.
"""
    return llm.invoke(prompt)


def path_recall(reference_contexts: list[list[str]], path: list[dict]) -> float | None:
    if not reference_contexts:
        return None
    traversed = set()
    for h in path:
        traversed.add((h["frm"], h["relation"], h["to"]))
        traversed.add((h["to"], h["relation"], h["frm"]))

    def norm(name: str) -> str:
        return ALIASES.get(name, name)

    hit = 0
    for s, r, o in reference_contexts:
        s, o = norm(s), norm(o)
        if (s, r, o) in traversed or (o, r, s) in traversed:
            hit += 1
    return hit / len(reference_contexts)


def classify_failure(start_found: bool, recall: float | None) -> str:
    if not start_found:
        return "색인"
    if recall is not None and recall < 0.5:
        return "탐색"
    return "생성"


def hop_bucket(chain: str) -> str:
    m = re.search(r"(\d)홉", chain)
    return f"{m.group(1)}홉" if m else "해당없음(전역)"


def main():
    goldenset = json.loads((BASE_DIR / "data" / "goldenset.json").read_text(encoding="utf-8"))
    G = nx.read_graphml(GRAPH_PATH)
    docs = load_all_docs()

    results = []
    for item in goldenset["items"]:
        q = item["user_input"]
        print(f"- {item['id']}: {q[:50]}...")

        graph_result = answer_question(q, G)
        start_found = bool(graph_result["start_entities"])
        recall = path_recall(item.get("reference_contexts", []), graph_result["path"])
        graph_judge = judge_answer(q, item["reference"], graph_result["answer"])

        rag_answer = basic_rag_answer(q, docs)
        rag_judge = judge_answer(q, item["reference"], rag_answer)

        failure = None if graph_judge.correct else classify_failure(start_found, recall)

        results.append({
            "id": item["id"], "kind": item["kind"], "hop": hop_bucket(item["chain"]),
            "question": q, "reference": item["reference"],
            "graph_answer": graph_result["answer"], "graph_correct": graph_judge.correct,
            "graph_judge_reason": graph_judge.reason, "path_recall": recall,
            "rag_answer": rag_answer, "rag_correct": rag_judge.correct,
            "failure_stage": failure,
        })

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "eval.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 홉 수별 그래프 에이전트 vs basic RAG 정답률 ===")
    by_hop = defaultdict(lambda: {"graph": [], "rag": [], "recall": []})
    for r in results:
        by_hop[r["hop"]]["graph"].append(r["graph_correct"])
        by_hop[r["hop"]]["rag"].append(r["rag_correct"])
        if r["path_recall"] is not None:
            by_hop[r["hop"]]["recall"].append(r["path_recall"])
    for hop in sorted(by_hop):
        v = by_hop[hop]
        g = f"{sum(v['graph'])}/{len(v['graph'])}"
        rg = f"{sum(v['rag'])}/{len(v['rag'])}"
        rc = f"{sum(v['recall']) / len(v['recall']):.0%}" if v["recall"] else "N/A"
        print(f"{hop:12s} 그래프 {g:5s} | basic RAG {rg:5s} | 경로 재현율 {rc}")

    total_graph = sum(r["graph_correct"] for r in results)
    total_rag = sum(r["rag_correct"] for r in results)
    print(f"\n전체: 그래프 {total_graph}/{len(results)} | basic RAG {total_rag}/{len(results)}")

    print("\n=== 실패 층 분류 (그래프 에이전트 오답만) ===")
    fails = defaultdict(int)
    for r in results:
        if r["failure_stage"]:
            fails[r["failure_stage"]] += 1
    if not fails:
        print("오답 없음")
    for stage, cnt in fails.items():
        print(f"{stage}: {cnt}건")

    print(f"\n저장: {OUT_DIR / 'eval.json'}")


if __name__ == "__main__":
    main()
