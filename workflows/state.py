from typing import List, Optional, TypedDict


class TestWorkflowState(TypedDict):
    """测试工作流的图状态。

    领域事实（Workflow / Artifact）以自建表为唯一可信来源；本 State 仅承载图运行期数据，
    由 LangGraph 的 checkpointer 持久化用于断点恢复与 interrupt 续跑。
    """

    # 图入口注入
    workflow_id: str
    user_id: int

    # load_requirement 节点填充
    requirement_text: str
    knowledge_base_id: Optional[str]

    # retrieve_knowledge 节点产出：[{source, content}]
    retrieved_documents: List[dict]

    # requirement_analysis_agent 产出；human_review 确认/编辑后覆盖
    requirement_analysis: dict
    analysis_artifact_id: Optional[str]

    # test_case_generation_agent 产出
    test_cases: List[dict]

    # coverage_check 产出
    coverage_report: dict

    # 任一节点失败时写入，条件边据此转 persist_failure
    error: Optional[str]
