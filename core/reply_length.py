"""Canonical length policy used to generate decision and reply instructions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyLengthPolicy:
    normal_sentences: int = 2
    normal_list_items: int = 2
    detailed_chars: int = 120

    @staticmethod
    def _word(count: int) -> str:
        return {1: "one", 2: "two", 3: "three"}.get(count, str(count))

    @property
    def criteria(self) -> dict[str, str]:
        return {
            "brief": "One sentence or less suffices: greeting, confirmation or simple answer",
            "normal": (
                f"One or {self._word(self.normal_sentences)} concise sentences or at most "
                f"{self._word(self.normal_list_items)} short list items; simple explanations and steps stay compact"
            ),
            "detailed": (
                "The current user explicitly requests depth, or a brief/normal answer would omit essential "
                "information or necessary steps; condense ordinary group-chat prose to at most "
                f"{self.detailed_chars} characters. {self.exceptions_en}"
            ),
        }

    @property
    def exceptions_en(self) -> str:
        return (
            "A request for detail alone does not waive the "
            f"{self.detailed_chars}-character limit. Explicit requests for complete code, long-form writing "
            "or a longer specified length may exceed this default; preserve the required content and "
            "structure for those requests."
        )

    @property
    def exceptions_zh(self) -> str:
        return (
            f"要求详解本身不放宽 {self.detailed_chars} 字限制；用户明确要求完整代码、长文或指定更长篇幅时，"
            "按该请求保留必需的信息和结构。"
        )

    @property
    def decision_focus(self) -> str:
        return (
            "Default to brief or normal. Judge the minimum useful answer to the current message, "
            "not the length or complexity of background. A short list, a few steps or a technical "
            "question alone does not require detailed. Choose detailed only for explicitly "
            "requested depth or essential content that cannot fit a compact answer. Follow "
            "operator decision_prompt length preferences while preserving explicit user requirements. "
            f"Normal uses at most {self._word(self.normal_sentences)} sentences; detailed ordinary prose "
            f"must fit {self.detailed_chars} characters in total. {self.exceptions_en}"
        )

    @property
    def reply_guidance_zh(self) -> str:
        items = {1: "一", 2: "两", 3: "三"}.get(self.normal_list_items, str(self.normal_list_items))
        return (
            f"normal 默认用 1～{self.normal_sentences} 句回答，短列表最多{items}项，先给结论或可用结果。"
            f"普通群聊即使判为 detailed，整轮正文也不超过 {self.detailed_chars} 字，只保留必要解释或步骤，"
            "省略重复背景、复述和总结；发送前检查长度，超出时重新压缩表达，短答不凑字数。"
            f"{self.exceptions_zh}\n"
        )

    @property
    def reply_instructions_en(self) -> str:
        sentences = self._word(self.normal_sentences)
        items = self._word(self.normal_list_items)
        return (
            "Write the actual reply only. Within response_plan and delivery_constraints, honor the current user's explicit\n"
            "length and format requirements, including complete code and long-form writing.\n"
            "Otherwise follow specific reply-length\n"
            "guidance in response_goal and reply_guidance before the generic length label:\n"
            f"tiny means only a few characters, short means one sentence, and medium means one or {sentences} sentences.\n"
            f"Without specific guidance, brief means one short sentence and normal means one or {sentences} concise sentences\n"
            f"or at most {items} short list items. For detailed, give only essential explanation or steps and keep ordinary\n"
            f"group-chat prose to at most {self.detailed_chars} characters in total across all paragraphs. Lead with the answer or result;\n"
            "omit redundant background, restatements and summaries. Check length before responding and rewrite an\n"
            f"overlong draft more compactly. Do not pad short answers. {self.exceptions_en}\n"
        )


REPLY_LENGTH_POLICY = ReplyLengthPolicy()
