"""
노란봉투법 입법 과정 지식그래프 챗봇 — streamlit 데모.

질문을 입력하면 agent.py(그래프 기반 멀티홉 에이전트)가 답변·탄 경로·근거를 함께 보여준다.
비교용으로 evaluate.py의 basic RAG(그래프 없이 키워드 검색만) 답변도 나란히 볼 수 있다.

데모 설계에서 신경 쓴 점:
  - 경로 시각화는 별도 그래프 렌더링 라이브러리(plotly 등) 없이 "A --관계--> B (n홉)" 텍스트
    목록으로 보여준다. 이 프로젝트 규모(노드 44개)에서는 텍스트 목록이 그래프 그림보다 오히려
    한눈에 들어오고, 새 의존성을 안 늘려도 된다 — 나중에 필요해지면 그때 추가하기로 함.
  - 근거는 기본적으로 접어 두고(양이 많아서) 펼쳐서 보고 싶을 때만 보게 한다.
  - 골든셋 질문을 사이드바에서 바로 클릭해서 써볼 수 있게 해서, 뭘 물어봐야 할지 막막하지 않게 함.

실행:
    streamlit run app.py
"""
import json
from pathlib import Path

import networkx as nx
import streamlit as st

from agent import GRAPH_PATH, answer_question
from evaluate import basic_rag_answer, load_all_docs

st.set_page_config(page_title="노란봉투법 그래프 챗봇", page_icon="⚖️")


@st.cache_resource
def get_graph() -> nx.DiGraph:
    return nx.read_graphml(GRAPH_PATH)


@st.cache_resource
def get_docs() -> list[dict]:
    return load_all_docs()


@st.cache_data
def get_goldenset() -> list[dict]:
    path = Path("data/goldenset.json")
    return json.loads(path.read_text(encoding="utf-8"))["items"]


def format_hop(h: dict) -> str:
    arrow = "→" if h["direction"] == "forward" else "←"
    return f"{h['hop']}홉  {h['frm']} {arrow}{h['relation']}{arrow} {h['to']}"


G = get_graph()

st.title("⚖️ 노란봉투법 입법 과정 그래프 챗봇")
st.caption(
    f"22대 국회 노동조합법 개정안(노란봉투법) 입법 과정 지식그래프 · "
    f"노드 {G.number_of_nodes()}개(법안·인물·위원회) · 엣지 {G.number_of_edges()}개"
)

with st.sidebar:
    st.header("예시 질문 (골든셋)")
    st.caption("클릭하면 질문창에 채워집니다.")
    for item in get_goldenset():
        if st.button(item["user_input"], key=item["id"], use_container_width=True):
            st.session_state["question"] = item["user_input"]

question = st.text_input("질문을 입력하세요", key="question", placeholder="예: 위원장 대안(2211924)의 제안자는 누구인가?")
show_rag = st.checkbox("basic RAG(그래프 없이 키워드 검색) 답변도 비교해서 보기")

if st.button("질문하기", type="primary") and question:
    with st.spinner("그래프를 탐색하는 중..."):
        result = answer_question(question, G)

    st.subheader("답변")
    st.write(result["answer"])

    st.subheader("탄 경로")
    if result["path"]:
        for h in result["path"]:
            st.text(format_hop(h))
    else:
        st.caption("(경로 없음 — 그래프에서 시작 개체를 찾지 못했거나 1홉 이내에 종료)")

    with st.expander(f"근거 원문 보기 ({len(result['evidence'])}건)"):
        for e in result["evidence"]:
            st.text(e)

    if show_rag:
        st.divider()
        st.subheader("(비교) basic RAG 답변 — 그래프 없이 키워드 검색만")
        with st.spinner("문서 검색 중..."):
            rag_answer = basic_rag_answer(question, get_docs())
        st.write(rag_answer)

st.divider()
st.caption(
    "출처: data/docs (법안 원문 19건 · 회의록 발언 전문 5건 · 인물 프로필 27건, "
    "국회 열린국회정보 API 및 국회도서관 발언 빅데이터에서 수집). "
    "자세한 설계는 REPORT.md 참고."
)
