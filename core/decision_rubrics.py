"""Chinese anchored rubrics shared by teacher and student in learning mode."""
from copy import deepcopy

RUBRICS = {
    "topic_relevance": (
        "判断与给出的当前话题内容的关联，不以机器人是否该插话代替关联度；没有话题上下文则证据不足。",
        ("明确无关或已换话题", "仅共享词语或背景", "同主题但联系间接", "直接延续当前主题", "明确回答或推进当前问题")),
    "question_value": (
        "判断请求本身的回答价值，不判断收件人是否为机器人；闲聊中的有效求助也有价值。",
        ("无问题或无需回应的口头语", "简单招呼或低信息确认", "明确普通问题或日常求助", "具体问题且回答能解决困难", "关键复杂请求且回答价值很高")),
    "professionalism": (
        "判断妥善回答所需的严谨程度；不是判断说话者职业或机器人是否有权插话。",
        ("纯寒暄表情或玩笑", "简单事实无需展开", "需要基本解释或核实", "需要专业分析或多个步骤", "高风险或复杂问题需严谨验证")),
    "silence_bias": (
        "判断此刻保持沉默的理由强度；不与参与强度机械取反。结合点名、边界和对话关系。",
        ("明确请求机器人回应且无冲突", "适合回应且少有打扰风险", "回应与旁听都有合理理由", "他人交流或回应价值低宜旁听", "明确拒绝打扰或不应介入")),
    "force_scale": (
        "判断机器人参与强度；不是回复长度。可以值得回答同时需要克制措辞。",
        ("无需参与", "仅轻量确认或简短补充", "正常回应而不主导", "主动提供有用帮助并跟进", "明确要求机器人主导解决问题")),
}


def rubric_questions(questions):
    result = deepcopy(questions)
    for key, question in result.items():
        if key in RUBRICS and question.get("type") == "score":
            instruction, levels = RUBRICS[key]
            question.update(instructions=instruction, criteria=list(levels))
    return result
